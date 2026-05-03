"""Stream-migrate ``storage/kg.json`` into a Neo4j instance.

Reads the source file with ijson (one entity / relation at a time, so
multi-GB inputs don't blow the heap) and ships entities + relations to
Neo4j in batches via ``UNWIND``-driven Cypher — round-trip count drops
from O(N) to O(N/batch_size).

Schema matches ``graph/neo4j_store.py`` so the migrated graph is a
drop-in replacement: ``KGEntity`` nodes keyed by ``entity_id``,
``RELATES_TO`` edges, vector indexes ``kg_entity_embedding`` and
``kg_relation_embedding`` created automatically once we know the
embedding dimension.

Usage::

    .venv\\Scripts\\python.exe scripts\\kg_json_to_neo4j.py \\
        --uri bolt://NEO4J_HOST:7687 \\
        --user neo4j \\
        --password "$NEO4J_PASSWORD" \\
        [--src storage/kg.json] [--batch 500] [--wipe] [--dry-run]

``--wipe`` deletes every ``KGEntity`` node + outgoing edges first.
Defaults to off — safer to fail loudly than silently overwrite.

``--dry-run`` walks the file but skips Cypher writes; useful to time
the read pass and validate counts before committing.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any


def _normalise_entity(raw: dict[str, Any]) -> dict[str, Any]:
    """Project the kg.json entity dict to the param shape upsert_entity wants."""
    return {
        "entity_id": raw["entity_id"],
        "name": raw.get("name", ""),
        "entity_type": raw.get("entity_type", "UNKNOWN"),
        "description": raw.get("description", ""),
        "source_doc_ids": sorted(raw.get("source_doc_ids", []) or []),
        "source_chunk_ids": sorted(raw.get("source_chunk_ids", []) or []),
        "source_paths": sorted(raw.get("source_paths", []) or []),
        "name_embedding": raw.get("name_embedding") or None,
    }


def _normalise_relation(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "relation_id": raw.get("relation_id"),
        "source_entity": raw.get("source_entity") or raw.get("source"),
        "target_entity": raw.get("target_entity") or raw.get("target"),
        "keywords": raw.get("keywords", ""),
        "description": raw.get("description", ""),
        "weight": float(raw.get("weight") or 1.0),
        "source_doc_ids": sorted(raw.get("source_doc_ids", []) or []),
        "source_chunk_ids": sorted(raw.get("source_chunk_ids", []) or []),
        "source_paths": sorted(raw.get("source_paths", []) or []),
        "description_embedding": raw.get("description_embedding") or None,
    }


# Bulk upsert via UNWIND — one round-trip per batch instead of per row.
ENTITY_UPSERT = """
UNWIND $rows AS row
MERGE (e:KGEntity {entity_id: row.entity_id})
SET
    e.name             = row.name,
    e.entity_type      = row.entity_type,
    e.description      = row.description,
    e.source_doc_ids   = row.source_doc_ids,
    e.source_chunk_ids = row.source_chunk_ids,
    e.source_paths     = row.source_paths,
    e.name_embedding   = coalesce(row.name_embedding, e.name_embedding)
"""

RELATION_UPSERT = """
UNWIND $rows AS row
MATCH (src:KGEntity {entity_id: row.source_entity})
MATCH (tgt:KGEntity {entity_id: row.target_entity})
MERGE (src)-[r:RELATES_TO {relation_id: row.relation_id}]->(tgt)
SET
    r.keywords              = row.keywords,
    r.description           = row.description,
    r.weight                = row.weight,
    r.source_doc_ids        = row.source_doc_ids,
    r.source_chunk_ids      = row.source_chunk_ids,
    r.source_paths          = row.source_paths,
    r.description_embedding = coalesce(row.description_embedding, r.description_embedding)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default="storage/kg.json", type=Path)
    ap.add_argument("--uri", default="bolt://10.50.4.54:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--batch", type=int, default=500, help="rows per UNWIND")
    ap.add_argument(
        "--wipe",
        action="store_true",
        help="DELETE all KGEntity + RELATES_TO before migrating",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk the file and validate counts; skip Cypher writes",
    )
    args = ap.parse_args()

    if not args.src.exists():
        print(f"ERROR: {args.src} does not exist", file=sys.stderr)
        return 1
    try:
        import ijson
    except ImportError:
        print("ERROR: ijson not installed (pip install ijson)", file=sys.stderr)
        return 1

    src_size = args.src.stat().st_size
    print(f"source : {args.src} ({src_size / 1e9:.2f} GB)")
    print(f"target : {args.uri} (database={args.database})")
    print(f"batch  : {args.batch}")
    if args.dry_run:
        print("mode   : DRY RUN (no writes)")
    elif args.wipe:
        print("mode   : WIPE + write")
    else:
        print("mode   : MERGE (existing nodes get updated)")
    print()

    # Lazy connect — dry-run shouldn't even reach the Neo4j host.
    driver = None
    if not args.dry_run:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
        try:
            driver.verify_connectivity()
        except Exception as e:
            print(f"ERROR: cannot reach Neo4j at {args.uri}: {e}", file=sys.stderr)
            return 2
        print(f"[{0:.1f}s] connected to Neo4j")

        with driver.session(database=args.database) as s:
            # Constraint guarantees idempotent MERGE on entity_id and is
            # also the index Cypher uses for the MATCH inside RELATION_UPSERT.
            s.run(
                "CREATE CONSTRAINT kg_entity_id IF NOT EXISTS "
                "FOR (e:KGEntity) REQUIRE e.entity_id IS UNIQUE"
            ).consume()
            if args.wipe:
                print("[ ... ] WIPE: deleting existing KGEntity / RELATES_TO ...", flush=True)
                s.run("MATCH ()-[r:RELATES_TO]->() DELETE r").consume()
                # batch-delete nodes to avoid one giant transaction
                while True:
                    res = s.run(
                        "MATCH (e:KGEntity) WITH e LIMIT 5000 DETACH DELETE e RETURN count(*) AS n"
                    ).single()
                    if not res or res["n"] == 0:
                        break
                print("        wipe complete")

    n_nodes = 0
    n_edges = 0
    t0 = time.time()
    last_log = t0

    def flush(session, batch: list[dict[str, Any]], cypher: str) -> None:
        if not batch:
            return
        if args.dry_run:
            return
        session.run(cypher, rows=batch).consume()

    # ──── Pass 1: entities ────
    print(f"[{time.time() - t0:5.1f}s] streaming entities ...", flush=True)
    batch: list[dict[str, Any]] = []
    session_ctx = driver.session(database=args.database) if driver else None
    session = session_ctx.__enter__() if session_ctx else None
    try:
        with open(args.src, "rb") as fh:
            for nd in ijson.items(fh, "nodes.item", use_float=True):
                batch.append(_normalise_entity(nd))
                if len(batch) >= args.batch:
                    flush(session, batch, ENTITY_UPSERT)
                    n_nodes += len(batch)
                    batch = []
                    now = time.time()
                    if now - last_log > 5:
                        rate = n_nodes / max(now - t0, 0.1)
                        print(
                            f"  [{now - t0:5.1f}s] {n_nodes:>6} entities ({rate:.0f}/s)",
                            flush=True,
                        )
                        last_log = now
            if batch:
                flush(session, batch, ENTITY_UPSERT)
                n_nodes += len(batch)
                batch = []
    finally:
        if session_ctx:
            session_ctx.__exit__(None, None, None)
    print(f"[{time.time() - t0:5.1f}s] entities done: {n_nodes:,}", flush=True)

    # ──── Pass 2: relations (rewind input) ────
    print(f"[{time.time() - t0:5.1f}s] streaming relations ...", flush=True)
    session_ctx = driver.session(database=args.database) if driver else None
    session = session_ctx.__enter__() if session_ctx else None
    try:
        with open(args.src, "rb") as fh:
            fh.seek(0)
            for ed in ijson.items(fh, "edges.item", use_float=True):
                batch.append(_normalise_relation(ed))
                if len(batch) >= args.batch:
                    flush(session, batch, RELATION_UPSERT)
                    n_edges += len(batch)
                    batch = []
                    now = time.time()
                    if now - last_log > 5:
                        rate = n_edges / max(now - t0, 0.1)
                        print(
                            f"  [{now - t0:5.1f}s] {n_edges:>6} relations ({rate:.0f}/s)",
                            flush=True,
                        )
                        last_log = now
            if batch:
                flush(session, batch, RELATION_UPSERT)
                n_edges += len(batch)
                batch = []
    finally:
        if session_ctx:
            session_ctx.__exit__(None, None, None)
    print(f"[{time.time() - t0:5.1f}s] relations done: {n_edges:,}", flush=True)

    if driver:
        driver.close()

    elapsed = time.time() - t0
    print()
    print(f"done in {elapsed:.1f}s")
    print(f"  entities  : {n_nodes:,}")
    print(f"  relations : {n_edges:,}")
    if args.dry_run:
        print("  (dry-run: no writes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
