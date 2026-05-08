# API Reference

OpenCraig exposes three categories of HTTP surface:

* **Agent layer** — `/api/v1/agent/hermes-chat` for SSE-streamed
  agentic chat, `/api/v1/llm/v1/chat/completions` for the
  OpenAI-compatible LLM proxy any in-container or external client
  hits.
* **MCP server** — `/api/v1/mcp` exposes domain tools (search /
  KG / library / artifacts) over the Model Context Protocol.
  Any MCP-compatible agent runtime connects here.
* **REST layer** — `/api/v1/{documents,files,chunks,conversations,
  graph,settings,...}` for the file/library/admin surfaces the
  web UI and SDK clients use directly.

Interactive documentation:

- **Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

All REST request/response bodies use JSON. File uploads use
`multipart/form-data`. SSE streams use `text/event-stream` with
`data: <json>\n\n` blocks.

---

## Agent Chat

### Stream a turn

```
POST /api/v1/agent/hermes-chat
```

Run one chat turn through the in-process Hermes Agent runtime.
Returns an SSE stream of events as the agent thinks, calls tools
(via MCP), and produces a final answer with citations.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | yes | The user's message |
| `conversation_id` | string | no | Continue an existing conversation; prior turns are loaded as history |
| `model` | string | no | Override the default model from `cfg.answering.generator.model` |
| `system_prompt_override` | string | no | Per-turn ephemeral system prompt |

**Response:** `text/event-stream` with `data: {type, ...}\n\n`
blocks. Event vocabulary:

```
{ "type": "agent.turn_start", "turn": 1, "run_id": "..." }
{ "type": "agent.thought",    "text": "..." }
{ "type": "tool.call_start",  "id": "...", "tool": "search_vector",
                              "params": {...} }
{ "type": "tool.call_end",    "id": "...", "tool": "search_vector",
                              "latency_ms": 42, "result_summary": {...} }
{ "type": "answer.delta",     "text": "..." }            // token stream
{ "type": "agent.turn_end",   "turn": 1, "run_id": "..." }
{ "type": "done",             "stop_reason": "end_turn",
                              "total_latency_ms": 1234,
                              "final_text": "...",
                              "run_id": "...",
                              "error": null }
```

`done` is always the last event — clients close the stream on it.

**Authz:** standard cookie / bearer auth via the principal
middleware. Path-filter scoping (folder permissions) resolves
server-side from the principal — no body field needed.

**Persistence:** when `conversation_id` is provided, the user
message lands in the `messages` table BEFORE the SSE stream opens
(so a mid-stream refresh always recovers the question), and the
final assistant message lands after `done`. An `agent_runs` row
records the turn for forward-compat lineage queries (Enterprise
edition only).

---

## LLM Proxy

### OpenAI-compatible chat completions

```
POST /api/v1/llm/v1/chat/completions
```

OpenAI-compatible endpoint backed by [litellm](https://github.com/BerriAI/litellm)
router. Provider keys (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` /
`GEMINI_API_KEY` / etc.) live in the backend env; clients (the
in-container Hermes runtime, external SDK callers, anything that
speaks OpenAI's wire format) only know the proxy URL + a session
bearer to *our* backend. Useful for:

* In-container agent runtimes pointing at the proxy via
  `OPENAI_BASE_URL=http://backend:8000/api/v1/llm/v1`
* Multi-provider routing — caller picks model name (`gpt-4o`,
  `claude-3-5-sonnet-...`, `gemini-pro`, local OpenAI-compat
  endpoints), litellm dispatches
* Centralised usage attribution — every call carries the
  authenticated `user_id`

**Request body:** standard OpenAI Chat Completions shape — `model`,
`messages` are required; `temperature`, `tools`, `tool_choice`,
`max_tokens`, `response_format`, `stream`, etc. all pass through.

**Response:** standard OpenAI Chat Completions response. With
`stream: true`, returns SSE in OpenAI's format
(`data: {chunk}\n\n` plus `data: [DONE]\n\n`).

**Errors:**
* `502` on upstream provider failure (the type name surfaces;
  raw provider message does not — avoids leaking server-side
  hints)
* `503` if litellm isn't installed
* `422` on schema-invalid body

---

## MCP Server

### Streamable HTTP endpoint

```
POST /api/v1/mcp
```

Model Context Protocol server exposing OpenCraig's domain tools
to any compatible agent runtime. Uses MCP's streamable HTTP
transport (single endpoint, JSON-RPC 2.0 wire format with optional
SSE for streaming responses).

**Tools exposed:**

| Name | Purpose |
|------|---------|
| `ping` | Diagnostic — confirms reachability + reports authenticated user_id |
| `search_vector` | Semantic / dense-embedding search over the corpus, scoped to the user's accessible folders |
| `read_chunk` | Fetch a single chunk's full content by chunk_id |
| `read_tree` | Navigate a document's section tree |
| `graph_explore` | Look up an entity / topic in the knowledge graph (with source-doc-coverage postfilter) |
| `web_search` | Search the public web; results are flagged untrusted (no instruction-following from titles/snippets) |
| `rerank` | Cross-encoder rerank a candidate chunk set |
| `import_from_library` | Copy a Library document into a project workdir (project-scoped) |

**Authz:** the principal-bridge ASGI middleware reads
`request.state.principal` (set by the standard `AuthMiddleware`)
off the scope and binds it to a per-request `ContextVar`. Each
tool wrapper builds a fresh `ToolContext` from that — same
multi-user authz path the SSE chat route uses. Path filters
resolve to the user's accessible-folder set automatically.

**Connecting from a client:** standard MCP HTTP client config —
point at `http://<your-host>:8000/api/v1/mcp` with a Bearer token
in the `Authorization` header. Any tool the user can't access is
silently filtered before the agent sees the catalogue.

For Hermes Agent (the in-process default), no client config is
needed — the runtime wrapper at `api/agent/hermes_runtime.py`
talks to the MCP server in-process via the same registered tool
table.

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
