# API Reference

OpenCraig exposes a REST API at `/api/v1/`. Interactive documentation is available at:

- **Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

All request/response bodies use JSON. File uploads use `multipart/form-data`.

---

## Query

### Ask a Question

```
POST /api/v1/query
```

Ask a question and get an answer with citations.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | The question to ask |
| `conversation_id` | string | no | Continue an existing conversation (multi-turn) |
| `stream` | bool | no | `true` for Server-Sent Events streaming (default: `false`) |
| `filter` | object | no | Metadata filter (e.g., `{"doc_id": "..."}`) |
| `path_filter` | string | no | Limit retrieval to a folder subtree (e.g. `"/legal/2024"`). Trashed docs are always excluded. |
| `overrides` | object | no | Per-request retrieval tweaks — see below. |

**`overrides` (QueryOverrides)** — any field left unset falls through to the yaml default. Non-mutating: these never touch global config.

| Field | Type | Effect |
|-------|------|--------|
| `query_understanding` | bool | Run / skip the QU LLM (intent + expansion). Skipping saves one LLM call. |
| `kg_path` | bool | Enable / disable the knowledge-graph retrieval path. |
| `tree_path` | bool | Enable / disable tree-navigation retrieval. |
| `tree_llm_nav` | bool | Use LLM vs heuristic tree navigator (`true` requires yaml `tree_path.llm_nav_enabled: true` — the navigator is lazy-built at startup). |
| `rerank` | bool | Run / skip the reranker stage. |
| `bm25_top_k` / `vector_top_k` / `tree_top_k` / `kg_top_k` / `rerank_top_k` | int | Override the path-level top-k. |
| `candidate_limit` | int | Cap on merged candidates passed to rerank. |
| `descendant_expansion` / `sibling_expansion` / `crossref_expansion` | bool | Toggle tree/cross-ref context expansion after RRF merge. |

**Fusion rule.** Tree + KG are the "primary reasoning" layer. When **both** are off or produced zero hits, retrieval falls back to an RRF of BM25 + vector — so `{"overrides": {"tree_path": false, "kg_path": false}}` yields a lexical/semantic hybrid search with no reasoning-LLM cost.

**Normal response** (`stream: false`):

```json
{
  "answer": "The answer text with citations...",
  "citations": [
    {
      "citation_id": "c_1",
      "chunk_id": "chunk_abc123",
      "doc_id": "doc_xyz",
      "page_no": 5,
      "bbox": {"x0": 72, "y0": 200, "x1": 540, "y1": 280},
      "snippet": "Relevant text excerpt...",
      "file_id": "file_001"
    }
  ],
  "conversation_id": "conv_123",
  "stats": {
    "retrieval_ms": 450,
    "generation_ms": 1200,
    "vector_hits": 15,
    "tree_hits": 8,
    "bm25_hits": 12,
    "merged_chunks": 10
  }
}
```

**Streaming response** (`stream: true`):

Returns `text/event-stream` with events:

| Event | Data | Description |
|-------|------|-------------|
| `progress` | `{"phase": "query_understanding"}` | Current retrieval phase |
| `retrieval` | `{"citations": [...], "stats": {...}}` | Retrieval results |
| `delta` | `{"text": "token"}` | Generated text token |
| `done` | `{"answer": "...", "citations": [...]}` | Final complete response |

**Example (streaming with curl):**

```bash
curl -N -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the revenue for Q3?", "stream": true}'
```

---

## Documents

### Ingest (file already uploaded)

```
POST /api/v1/documents
```

Queue an already-uploaded blob for ingestion. Use `POST /api/v1/files` first to get a `file_id`.

**Request body (JSON):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_id` | string | yes | From `/api/v1/files` |
| `doc_id` | string | no | Override the auto-derived id |
| `parse_version` | int | no | Default 1 |
| `folder_path` | string | no | Destination folder, default `/` |
| `force_reparse` | bool | no | Re-parse an existing doc at bumped `parse_version` (preserves original folder) |
| `enrich_summary` | bool | no | Force LLM tree-summary on / off for this job (default = yaml `parser.tree_builder.llm_enabled`) |

**Response** (202 Accepted):

```json
{
  "doc_id": "doc_abc123",
  "file_id": "file_xyz",
  "status": "pending",
  "message": "queued for processing"
}
```

Document processing is asynchronous. Poll `GET /api/v1/documents/{doc_id}` for status.

### Upload and Ingest (one-shot multipart)

```
POST /api/v1/documents/upload-and-ingest
```

Upload the file and queue ingestion in a single request.

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | yes | The document bytes |
| `original_name` | string | no | Overrides the browser-supplied filename |
| `mime_type` | string | no | Overrides content-type |
| `doc_id` | string | no | Override the auto-derived id |
| `folder_path` | string | no | Destination folder, default `/`. Collisions auto-suffix (`foo.pdf` → `foo (1).pdf`). |

A non-existent `folder_path` returns `404` — the blob is not uploaded in that case, so no orphaned files.

### List Documents

```
GET /api/v1/documents?limit=50&offset=0&path_filter=/legal&recursive=true
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | 50 | Page size (≤ 200) |
| `offset` | int | 0 | Offset |
| `search` | string | — | Substring match on filename / doc_id |
| `status` | string | — | Comma-separated statuses: `pending,parsing,ready,error` |
| `path_filter` | string | — | Folder path to restrict to (same semantics as `/query` `path_filter`). `404` if folder doesn't exist. |
| `recursive` | bool | `true` | With `path_filter`: `true` = entire subtree; `false` = only direct children of that folder. |

**Response:**

```json
{
  "items": [
    {
      "doc_id": "doc_abc123",
      "file_name": "annual_report.pdf",
      "format": "pdf",
      "status": "ready",
      "embed_status": "done",
      "enrich_status": "done",
      "num_chunks": 142,
      "num_blocks": 580,
      "file_size_bytes": 2457600,
      "created_at": "2025-01-15T10:30:00Z"
    }
  ],
  "total": 1
}
```

### Get Document Detail

```
GET /api/v1/documents/{doc_id}
```

Returns full document metadata including processing status, timing, and statistics.

### Get Document Blocks

```
GET /api/v1/documents/{doc_id}/blocks?limit=100&offset=0
```

Returns parsed blocks with page numbers, bounding boxes, and types.

### Get Document Chunks

```
GET /api/v1/documents/{doc_id}/chunks?limit=50&offset=0
```

Returns chunks with content, token counts, section paths, and block IDs.

### Get Document Tree

```
GET /api/v1/documents/{doc_id}/tree
```

Returns the full hierarchical tree structure:

```json
{
  "root_id": "node_root",
  "generation_method": "toc",
  "quality_score": 0.92,
  "nodes": {
    "node_root": {
      "node_id": "node_root",
      "title": "Document",
      "level": 0,
      "page_start": 1,
      "page_end": 50,
      "children": ["node_ch1", "node_ch2"],
      "block_ids": [],
      "summary": null
    },
    "node_ch1": {
      "node_id": "node_ch1",
      "title": "Chapter 1: Introduction",
      "level": 1,
      "page_start": 1,
      "page_end": 12,
      "children": ["node_s1_1", "node_s1_2"],
      "block_ids": ["blk_001", "blk_002"],
      "summary": "Overview of the project..."
    }
  }
}
```

### Delete Document

```
DELETE /api/v1/documents/{doc_id}
```

Soft-deletes the document (marks status).

### Reparse Document

```
POST /api/v1/documents/{doc_id}/reparse
```

Force re-ingestion with a new parse version.

---

## Files

### Upload File

```
POST /api/v1/files
```

Upload a file without triggering ingestion. Useful for two-step workflows.

**Request:** `multipart/form-data` with `file` field.

### Upload from URL

```
POST /api/v1/files/from-url
```

Fetch a file from a URL (SSRF-protected).

**Request body:**

```json
{
  "url": "https://example.com/report.pdf",
  "original_name": "report.pdf"
}
```

### Download File

```
GET /api/v1/files/{file_id}/download
```

Returns the file with `Content-Disposition: attachment`.

### Preview File

```
GET /api/v1/files/{file_id}/preview
```

Returns the file with `Content-Disposition: inline` (for PDF viewer embedding).

### List Files

```
GET /api/v1/files?limit=50&offset=0
```

### Delete File

```
DELETE /api/v1/files/{file_id}
```

---

## Chunks

### Get Chunk by ID

```
GET /api/v1/chunks/{chunk_id}
```

Returns a single chunk with full metadata.

### Get Block Image

```
GET /api/v1/blocks/{block_id}/image
```

Returns the extracted image for a figure block.

---

## Conversations

### List Conversations

```
GET /api/v1/conversations?limit=20&offset=0
```

### Get Conversation

```
GET /api/v1/conversations/{conversation_id}
```

Returns all turns (questions + answers + citations) in the conversation.

### Delete Conversation

```
DELETE /api/v1/conversations/{conversation_id}
```

---

## Knowledge Graph

### Get Full Graph

```
GET /api/v1/graph?limit=1000
```

Returns all entities and relations for visualization.

### Get Entity Detail

```
GET /api/v1/graph/entities/{entity_id}
```

Returns entity info + neighboring entities + relations.

### Search Entities

```
GET /api/v1/graph/search?q=revenue&top_k=10
```

Fuzzy/substring search over entity names.

### Get Subgraph

```
GET /api/v1/graph/subgraph?entity_ids=e1,e2,e3
```

Returns the subgraph connecting the specified entities.

---

## Settings

### Get All Settings

```
GET /api/v1/settings
```

Returns all settings grouped by category.

### Get Settings by Group

```
GET /api/v1/settings/{group}
```

### Get Single Setting

```
GET /api/v1/settings/key/{key}
```

Key uses dotted notation: `retrieval.vector.top_k`.

> **Note — settings are read-only.** YAML is the single source of truth
> ([configuration.md](configuration.md)); the `settings` table is a one-way
> mirror written at server boot. Edit `opencraig.yaml` and restart to change
> configuration. Per-query retrieval tweaks go through
> [`QueryOverrides`](#post-apiv1query).

---

## LLM Providers

> The dedicated `/api/v1/llm-providers/*` HTTP surface was removed in v0.2.0 along with the `provider_id` indirection. Models + credentials are now inlined directly under each subsystem in `opencraig.yaml` (see [configuration.md](configuration.md)). To check or change them: edit the yaml and restart, or re-run `python scripts/setup.py` (the wizard live-tests every endpoint before saving).

---

## System

### Health Check

```
GET /api/v1/health
```

Returns `{"status": "ok"}` if the server is running.

### System Info

```
GET /api/v1/system/info
```

Returns backend versions, document count, storage usage, and configuration summary.

---

## Traces

### List Retrieval Traces

```
GET /api/v1/traces?limit=20&offset=0
```

Returns retrieval trace history with timing, phases, and LLM call details.

---

## Benchmark

### Start Benchmark

```
POST /api/v1/benchmark/start
```

**Request body:**

```json
{
  "num_questions": 20
}
```

### Get Benchmark Status

```
GET /api/v1/benchmark/status
```

Returns current phase, progress, elapsed time, and estimated remaining time.

### Cancel Benchmark

```
POST /api/v1/benchmark/cancel
```

### Download Benchmark Report

```
GET /api/v1/benchmark/report
```

Returns a JSON report with scores, per-question details, and config snapshot (credentials redacted).

---

## Error Responses

All errors follow a consistent format:

```json
{
  "detail": "Document not found"
}
```

| Status Code | Meaning |
|-------------|---------|
| 400 | Bad request (invalid parameters) |
| 404 | Resource not found |
| 409 | Conflict (e.g., document already ingesting) |
| 413 | File too large |
| 422 | Validation error |
| 500 | Internal server error |
