"""
Nightly maintenance script.

Drains the ``pending_folder_ops`` queue populated by FolderService
when a folder rename / move exceeds the sync threshold
(``_CROSS_STORE_SYNC_THRESHOLD`` = 2000 affected chunks).

Each pending op is a `(old_path, new_path)` pair. The script:

  1. Atomically claims a small batch of ``status='pending'`` rows
     and flips them to ``'running'`` (SKIP LOCKED so multiple
     workers don't clobber each other).
  2. For each claimed op, calls ``update_paths`` on the configured
     Chroma + Neo4j stores — exactly the same API FolderService
     uses for the <2000-chunk sync path.
  3. Marks the op ``done`` on success, or ``failed`` with the
     exception message on any error. Failed ops stay in the table
     so the next run can retry after human inspection.

Typical invocation from cron (2–3 AM local):
    FORGERAG_READONLY=1  # set on the server before starting
    python -m scripts.nightly_maintenance
    # unset / restart server

Running without read-only mode is supported but not recommended —
writes that happen concurrently can accumulate under a path that's
mid-rewrite, which the OR-fallback retrieval logic handles correctly
but widens the reconciliation window.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time


log = logging.getLogger("forgerag.nightly_maintenance")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(message)s",
    )
    parser = argparse.ArgumentParser(description="Drain pending_folder_ops")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Max ops to claim and process per inner loop iteration.",
    )
    parser.add_argument(
        "--max-ops",
        type=int,
        default=1000,
        help="Upper bound on the total ops processed in this run.",
    )
    args = parser.parse_args()

    # Lazy imports so --help works without the full dependency tree.
    from api.state import AppState
    from config.loader import load_config
    from persistence.pending_ops import (
        claim_next_batch,
        mark_done,
        mark_failed,
    )

    cfg = load_config()
    state = AppState(cfg)
    try:
        store = state.store
        graph_store = state.graph_store
        vector_store = state.vector_store

        processed = 0
        t_start = time.monotonic()
        while processed < args.max_ops:
            # Claim a batch inside its own short transaction so the
            # running-state transition is durable before any downstream
            # work starts.
            with store.transaction() as sess:
                claimed = claim_next_batch(sess, limit=args.batch_size)
                # Capture the fields we need as plain dicts BEFORE the
                # session closes — ORM instances become detached after.
                batch = [
                    {
                        "op_id": op.op_id,
                        "old_path": op.old_path,
                        "new_path": op.new_path,
                        "affected_chunks": op.affected_chunks,
                    }
                    for op in claimed
                ]
            if not batch:
                break

            for op in batch:
                op_id = op["op_id"]
                old_p = op["old_path"]
                new_p = op["new_path"]
                log.info(
                    "processing op %s: %s -> %s  (chunks~%s)",
                    op_id,
                    old_p,
                    new_p,
                    op["affected_chunks"],
                )
                err: str | None = None
                try:
                    if graph_store is not None and hasattr(graph_store, "update_paths"):
                        touched = graph_store.update_paths(old_p, new_p)
                        log.info("  graph update_paths touched=%s", touched)
                    if vector_store is not None and hasattr(vector_store, "update_paths"):
                        touched = vector_store.update_paths(old_p, new_p)
                        log.info("  vector update_paths touched=%s", touched)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    log.exception("op %s FAILED", op_id)

                with store.transaction() as sess:
                    if err is None:
                        mark_done(sess, op_id)
                    else:
                        mark_failed(sess, op_id, err)
                processed += 1

        log.info(
            "nightly maintenance done: %d op(s) in %.1fs",
            processed,
            time.monotonic() - t_start,
        )
    finally:
        try:
            state.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
