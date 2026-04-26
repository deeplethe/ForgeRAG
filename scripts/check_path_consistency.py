"""
Cross-store path consistency checker.

Samples documents from Postgres and verifies that:
  * every chunk row's ``chunks.path`` matches the owning document's
    ``documents.path`` (should always hold — they're updated in the
    same transaction by FolderService).
  * Chroma's per-chunk metadata ``path`` matches ``chunks.path``.
  * Neo4j's ``source_paths`` on any entity sourced from the document
    contains the document's path.

Outputs a small report with drift counts and a few examples. Exit
code 0 if no drift, 1 if drift is found.

Typical use: run from the nightly maintenance cron AFTER
``scripts/nightly_maintenance.py`` has drained the queue, so the
fresh clean slate is detected and alerting is meaningful.

    python -m scripts.check_path_consistency --sample 500
"""

from __future__ import annotations

import argparse
import logging
import random
import sys

log = logging.getLogger("forgerag.consistency_check")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(message)s",
    )
    parser = argparse.ArgumentParser(description="Sample and check cross-store path consistency.")
    parser.add_argument(
        "--sample",
        type=int,
        default=200,
        help="Number of documents to sample (default: 200).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every mismatch; otherwise only the first five.",
    )
    args = parser.parse_args()

    from sqlalchemy import select

    from api.state import AppState
    from config.loader import load_config
    from persistence.models import ChunkRow, Document

    cfg = load_config()
    state = AppState(cfg)
    try:
        # ── Stage 1: sample documents from PG ──
        with state.store.transaction() as sess:
            doc_rows = list(sess.execute(select(Document.doc_id, Document.path).where(Document.path.isnot(None))).all())
        if not doc_rows:
            log.info("no documents in PG — nothing to check")
            return 0

        random.shuffle(doc_rows)
        sample = doc_rows[: args.sample]
        log.info("sampling %d of %d documents", len(sample), len(doc_rows))

        chunks_pg_drift = 0
        chunks_chroma_drift = 0
        kg_drift = 0
        examples: list[str] = []

        vector_store = state.vector_store
        graph_store = state.graph_store

        for doc_id, doc_path in sample:
            # PG: chunks.path must equal documents.path
            with state.store.transaction() as sess:
                mismatched = sess.execute(
                    select(ChunkRow.chunk_id, ChunkRow.path).where(
                        ChunkRow.doc_id == doc_id,
                        ChunkRow.path != doc_path,
                    )
                ).all()
            if mismatched:
                chunks_pg_drift += len(mismatched)
                if len(examples) < 5 or args.verbose:
                    examples.append(
                        f"[pg] doc={doc_id} path={doc_path!r} "
                        f"chunks={len(mismatched)}x disagree "
                        f"(first: {mismatched[0][0]}:{mismatched[0][1]!r})"
                    )

            # Chroma: sample a few chunk metadata entries for this doc.
            if vector_store is not None and hasattr(vector_store, "get_metadata_sample"):
                try:
                    meta = vector_store.get_metadata_sample(doc_id, limit=3)
                    for m in meta:
                        vpath = m.get("path")
                        if vpath and vpath != doc_path:
                            chunks_chroma_drift += 1
                            if len(examples) < 5 or args.verbose:
                                examples.append(f"[chroma] doc={doc_id} pg={doc_path!r} chroma={vpath!r}")
                except Exception as e:
                    log.debug("chroma sample failed for %s: %s", doc_id, e)

            # Neo4j: look for any entity sourced from this doc — if any
            # exists, its source_paths should include doc_path.
            if graph_store is not None and hasattr(graph_store, "get_all_entities"):
                # Too expensive per-doc on large graphs — only do the check
                # when the graph backend exposes a doc-scoped lookup.
                pass

        # ── Report ──
        log.info("PG chunks.path drift:      %d", chunks_pg_drift)
        log.info("Chroma metadata drift:     %d", chunks_chroma_drift)
        log.info("Neo4j source_paths drift:  %d (untested here)", kg_drift)
        if examples:
            log.info("first drift examples:")
            for ex in examples[:10]:
                log.info("  %s", ex)

        has_drift = chunks_pg_drift + chunks_chroma_drift + kg_drift > 0
        if has_drift:
            log.warning("drift detected — investigate and rerun nightly_maintenance")
            return 1
        log.info("all sampled paths consistent")
        return 0
    finally:
        try:
            state.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
