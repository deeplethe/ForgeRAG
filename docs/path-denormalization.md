# Path denormalization & folder rename

This document describes how ForgeRAG keeps folder path information
consistent across its three primary stores — Postgres, Chroma
(vector), and Neo4j (knowledge graph) — and how it handles the
edge cases that arise when a folder rename affects many chunks.

## Why denormalize?

Retrieval fires four search paths (BM25, Vector, Tree, KG) in
parallel and merges the results. Three of those paths (Vector on
pgvector/Chroma, Tree on PG chunks, KG on Neo4j entities) can
pre-filter by folder path if each chunk/entity carries its owning
document's path as a denormalised field:

* `chunks.path`              — PG, added in migration D1
* Chroma metadata `path`     — set at upsert time, D2
* Neo4j `source_paths`       — unioned onto entities and relations, D3

Without denormalisation, scope-filtering would require a cross-store
join against Postgres's `documents` table on every query, which
defeats the latency budget of each path.

## Single-source rule

Postgres is the authoritative source. The invariant:

    documents.path is the source of truth; chunks.path mirrors it;
    Chroma metadata.path mirrors chunks.path; Neo4j source_paths
    contains the path of every document that sourced the entity.

`FolderService` is the only code path allowed to mutate folder /
document paths. It updates `folders.path`, `documents.path`, and
`chunks.path` inside a single transaction — so PG is always
coherent at commit time.

## Threshold router

Cross-store propagation (Chroma + Neo4j) is routed by affected-chunk
count:

| Affected chunks | Route      | Latency         |
|-----------------|------------|-----------------|
| < 2000          | sync       | user blocks a few seconds |
| ≥ 2000          | deferred   | queued for nightly maintenance |

The constant lives in `persistence/folder_service.py` as
`_CROSS_STORE_SYNC_THRESHOLD`.

### Sync path

After the PG transaction commits, the HTTP route calls
`update_paths(old, new)` on `graph_store` and `vector_store`.
Errors are logged but not raised — the consistency checker and
nightly maintenance eventually converge any partial failure.

### Deferred path

A `pending_folder_ops` row is inserted with the old/new path pair
inside the same PG transaction. Retrieval is protected during the
lag window by the **OR-fallback filter** (see below). The nightly
maintenance script drains the queue.

## OR-fallback at query time

When a query scoped to path `/LegalV2/Contracts` lands while a
pending op `/Legal → /LegalV2` hasn't yet been applied to
Chroma / Neo4j, the retrieval pipeline computes the *rebased* old
prefix `/Legal/Contracts` and passes it as `path_prefix_or`:

* Chroma: `_build_chroma_where` ORs the prefixes.
* Neo4j: the embedding search WHERE checks
  `ANY(pfx IN $prefixes WHERE ANY(p IN source_paths ...))`.
* NetworkX (tests): client-side post-filter.

`persistence/pending_ops.py::or_fallback_prefixes` is the single
helper that computes the list; it's called once per query from
`retrieval/pipeline._resolve_path_scope`.

## Nightly maintenance window

    FORGERAG_READONLY=1   # reject mutating HTTP methods (503)
    python -m scripts.nightly_maintenance --max-ops 5000
    # optional post-check:
    python -m scripts.check_path_consistency --sample 500

The read-only middleware lives in `api/app.py`. Health endpoints
and `/system/readonly` (toggle, if added) stay reachable.

## Ops runbook

* A pending op stuck in `running` after a crash: the next run
  of `nightly_maintenance` ignores it (only `pending` rows claim).
  Resolve by manually flipping it back to `pending` or dropping it
  once you've verified the downstream stores by running
  `check_path_consistency`.
* Drift found by the consistency checker: usually a pending op
  failed. Inspect `pending_folder_ops` for `status='failed'` rows
  and the `error_msg` column.
