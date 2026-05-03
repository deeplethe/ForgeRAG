"""Backfill: compact accumulated entity / relation descriptions in the live graph.

The post-upsert summarise phase added in PR3 only handles entities /
relations TOUCHED by future ingests. Entities that already accumulated
20+ description fragments before the feature shipped never trigger
unless something happens to re-touch them — meaning the existing
high-cardinality entities (e.g. ``Sustainability`` with hundreds of
mentions) keep their bloated descriptions indefinitely.

This script walks the entire graph store, finds anything that crosses
the ``KGSummaryConfig`` threshold (token total ≥ ``trigger_tokens`` or
fragment count ≥ ``force_on_count``), runs the same
``graph.summarize.summarize_descriptions`` pass against it, and writes
the canonical summary back.

Run modes:

    # Default: read storage/kg.json (NetworkX backend)
    .venv\\Scripts\\python.exe scripts\\summarize_existing_graph.py

    # Neo4j backend
    .venv\\Scripts\\python.exe scripts\\summarize_existing_graph.py \\
        --backend neo4j --uri bolt://NEO4J_HOST:7687 --user neo4j \\
        --password "$NEO4J_PASSWORD"

    # Dry run — print what WOULD be summarised, no LLM calls, no writes
    .venv\\Scripts\\python.exe scripts\\summarize_existing_graph.py --dry-run

    # Re-embed relation descriptions after compaction (recommended)
    .venv\\Scripts\\python.exe scripts\\summarize_existing_graph.py \\
        --reembed-relations

    # Override thresholds for a more aggressive backfill pass
    .venv\\Scripts\\python.exe scripts\\summarize_existing_graph.py \\
        --trigger-tokens 800 --force-on-count 5

Safe to re-run: a description that's already a single LLM-summarised
paragraph won't have enough fragments to re-trigger the threshold. To
force re-summarise (e.g. after a prompt change), pass ``--force``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make the repo root importable when run as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from concurrent.futures import ThreadPoolExecutor, as_completed

from graph.base import GraphStore
from graph.summarize import (
    SummarizeConfig,
    needs_summary,
    split_fragments,
    summarize_descriptions,
)

log = logging.getLogger("summarize_backfill")


def _build_store(args) -> GraphStore:
    """Open the requested graph store backend."""
    if args.backend == "neo4j":
        from graph.neo4j_store import Neo4jGraphStore

        password = args.password or os.environ.get("NEO4J_PASSWORD") or ""
        return Neo4jGraphStore(
            uri=args.uri,
            user=args.user,
            password=password,
            database=args.database,
        )
    # NetworkX
    from graph.networkx_store import NetworkXGraphStore

    return NetworkXGraphStore(path=str(args.src))


def _build_summarize_cfg(args) -> SummarizeConfig:
    return SummarizeConfig(
        enabled=True,
        trigger_tokens=args.trigger_tokens,
        force_on_count=args.force_on_count,
        max_output_tokens=args.max_output_tokens,
        context_size=args.context_size,
        max_iterations=args.max_iterations,
        model=args.model,
        api_key=args.api_key or os.environ.get("CHAT_API_KEY"),
        api_base=args.api_base,
        timeout=args.timeout,
        language=args.language,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    # Backend selection
    ap.add_argument("--backend", choices=["networkx", "neo4j"], default="networkx")
    ap.add_argument("--src", type=Path, default=Path("storage/kg.json"), help="(networkx) kg.json path")
    ap.add_argument("--uri", default="bolt://localhost:7687", help="(neo4j) Bolt URI")
    ap.add_argument("--user", default="neo4j", help="(neo4j) username")
    ap.add_argument("--password", default=None, help="(neo4j) password (or set NEO4J_PASSWORD)")
    ap.add_argument("--database", default="neo4j", help="(neo4j) database name")

    # Summarise thresholds
    ap.add_argument("--trigger-tokens", type=int, default=1200)
    ap.add_argument("--force-on-count", type=int, default=8)
    ap.add_argument("--max-output-tokens", type=int, default=600)
    ap.add_argument("--context-size", type=int, default=12000)
    ap.add_argument("--max-iterations", type=int, default=5)

    # LLM
    ap.add_argument("--model", default="openai/gpt-4o-mini")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--api-base", default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument(
        "--language",
        default="Write the entire output in the original language of the input descriptions",
    )

    # Behaviour
    ap.add_argument("--dry-run", action="store_true", help="Report only — no LLM, no writes")
    ap.add_argument("--force", action="store_true", help="Re-summarise even single-paragraph descriptions")
    ap.add_argument("--reembed-relations", action="store_true", help="Re-embed relation.description after summary")
    ap.add_argument("--max-workers", type=int, default=5, help="Concurrent LLM calls")
    ap.add_argument("--limit", type=int, default=0, help="Stop after this many compactions (0 = unlimited)")
    ap.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = _build_summarize_cfg(args)
    gs = _build_store(args)
    log.info("backend=%s, dry_run=%s, force=%s", args.backend, args.dry_run, args.force)

    # ---------------- Entities ----------------
    ent_targets: list = []
    entities = gs.get_all_entities()
    log.info("scanning %d entities", len(entities))
    for ent in entities:
        frags = split_fragments(ent.description)
        if args.force or needs_summary(frags, cfg):
            ent_targets.append((ent, frags))
    log.info("entity targets: %d / %d", len(ent_targets), len(entities))

    # ---------------- Relations ----------------
    rel_targets: list = []
    relations = gs.get_all_relations()
    log.info("scanning %d relations", len(relations))
    for rel in relations:
        frags = split_fragments(rel.description)
        if args.force or needs_summary(frags, cfg):
            rel_targets.append((rel, frags))
    log.info("relation targets: %d / %d", len(rel_targets), len(relations))

    if args.dry_run:
        # Print the top offenders so the user can spot-check the cull
        ent_targets.sort(key=lambda t: len(t[1]), reverse=True)
        rel_targets.sort(key=lambda t: len(t[1]), reverse=True)
        print("\nTop 10 entities by fragment count:")
        for ent, frags in ent_targets[:10]:
            chars = len(ent.description)
            print(f"  {ent.name!r:40} frags={len(frags):4d} chars={chars}")
        print("\nTop 10 relations by fragment count:")
        for rel, frags in rel_targets[:10]:
            label = rel.keywords or f"{rel.source_entity[:8]}-{rel.target_entity[:8]}"
            chars = len(rel.description)
            print(f"  {label!r:40} frags={len(frags):4d} chars={chars}")
        return 0

    # ---------------- Embedder (optional, for relation re-embed) ----------------
    embedder = None
    if args.reembed_relations:
        try:
            embedder = _build_embedder()
        except Exception as exc:
            log.warning("embedder build failed (%s); relation re-embed skipped", exc)

    # ---------------- Compact ----------------
    targets = []
    for ent, frags in ent_targets:
        targets.append(("entity", ent, frags))
    for rel, frags in rel_targets:
        targets.append(("relation", rel, frags))
    if args.limit > 0:
        targets = targets[: args.limit]
    log.info("compacting %d targets with max_workers=%d", len(targets), args.max_workers)

    done = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {}
        for kind, obj, frags in targets:
            if kind == "entity":
                name = obj.name
            else:
                name = obj.keywords or f"{obj.source_entity[:8]}→{obj.target_entity[:8]}"
            fut = pool.submit(
                summarize_descriptions,
                name=name,
                kind=kind,
                fragments=frags,
                cfg=cfg,
            )
            futures[fut] = (kind, obj)

        for fut in as_completed(futures):
            kind, obj = futures[fut]
            try:
                summary = fut.result()
            except Exception as exc:
                log.warning("summarise failed for %s: %s", kind, exc)
                failed += 1
                continue
            if not summary or summary == obj.description:
                continue
            try:
                if kind == "entity":
                    gs.update_entity_description(obj.entity_id, summary)
                else:
                    new_emb = None
                    if embedder is not None:
                        try:
                            new_emb = embedder.embed_texts([summary])[0]
                        except Exception:
                            log.warning("relation re-embed failed for %s", obj.relation_id)
                    gs.update_relation_description(obj.relation_id, summary, new_emb)
                done += 1
                if done % 50 == 0:
                    log.info("compacted %d / %d", done, len(targets))
            except Exception as exc:
                log.warning("write-back failed for %s: %s", kind, exc)
                failed += 1

    log.info("done: %d compacted, %d failed", done, failed)
    return 0 if failed == 0 else 2


def _build_embedder():
    """Build an embedder from the project config — best-effort."""
    from config import load_config
    from embedder.factory import build_embedder

    cfg = load_config(None)
    return build_embedder(cfg.embedder)


if __name__ == "__main__":
    raise SystemExit(main())
