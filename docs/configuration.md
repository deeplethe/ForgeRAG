# Configuration Reference

ForgeRAG is configured via a single YAML file. **YAML is the single source of truth** — there is no runtime config editing via UI. Edit the file and restart to apply.

## Config Resolution Order

1. `--config <path>` CLI argument
2. `$FORGERAG_CONFIG` environment variable
3. `./forgerag.yaml` in the working directory
4. Auto-generated skeleton (written on first boot if no yaml exists; needs at minimum your LLM + embedding provider credentials filled in before queries will succeed)

A fully commented example is available at [`examples/forgerag.dev.yaml`](../examples/forgerag.dev.yaml).

## Per-request overrides (not the same as config editing)

A subset of retrieval knobs can be overridden **per query** via `QueryOverrides` in the `/api/v1/query` request body — handy for A/B testing and debug without mutating the global config. See [api-reference.md](api-reference.md#post-apiv1query) for the field list. These overrides never mutate YAML or the database; they apply only to the single request.

## DB as a one-way backup mirror

On startup, ForgeRAG writes the resolved cfg into the `settings` table as a read-only snapshot. `GET /api/v1/settings` returns this snapshot for admin tooling. The runtime **never reads back** — components always consult the in-memory cfg loaded from YAML. Any drift between DB and YAML is resolved in YAML's favour on the next boot. (A legacy `llm_providers` table also exists for migration compatibility but is unused since v0.2.0 dropped the `provider_id` indirection.)

## Changing configuration — the only way

Edit `forgerag.yaml` (or `myconfig.yaml` via `--config`) and restart the backend. This applies to every setting: infrastructure (persistence/storage/graph backends), LLM providers, retrieval parameters, prompts, everything.

---

## Sections

### `parser`

Controls document parsing, chunking, and tree building.

#### `parser.backend`

Single explicit choice — no fallback chain. Pick one of:

| Value | Description |
|-------|-------------|
| `pymupdf` (default) | Fast, no extra dependencies. |
| `mineru` | Layout-aware (tables / formulas / multi-column). Pulls GBs of model weights on first run. |
| `mineru-vlm` | Vision-language MinerU. Best for scanned / handwritten / very complex layouts. Heaviest. |

#### `parser.mineru`

Sub-config for MinerU; only used when `parser.backend` is `mineru` or `mineru-vlm`. The pipeline auto-derives `mineru.backend` from the top-level choice.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `device` | string | `"cuda"` | Compute device: `cuda` or `cpu` |
| `lang` | string | `"ch"` | Primary OCR language |
| `formula_enable` | bool | `true` | Enable formula detection |
| `table_enable` | bool | `true` | Enable table detection |
| `parse_method` | string | `"auto"` | Parse method: `auto`, `txt`, `ocr` |
| `server_url` | string | null | Remote VLM server URL (leave blank for local inference, only meaningful with `mineru-vlm`) |

#### `parser.chunker`

Controls how blocks are packed into chunks.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `target_tokens` | int | `600` | Target token count per chunk (greedy packing) |
| `max_tokens` | int | `1000` | Hard ceiling; chunks exceeding this are split |
| `min_tokens` | int | `100` | Trailing chunks below this merge into previous |
| `tokenizer` | string | `"char_approx"` | Token counting method (CJK-aware character approximation) |
| `isolate_tables` | bool | `true` | Tables become single-block chunks |
| `isolate_figures` | bool | `true` | Figures become single-block chunks |
| `isolate_formulas` | bool | `false` | Formulas become single-block chunks |
| `overlap_blocks` | int | `0` | Number of blocks to overlap between adjacent chunks |

#### `parser.tree_builder`

Controls how document hierarchy is built. When `llm_enabled` is true, an LLM groups pages into logical sections, generates titles and per-node summaries in a single call. TOC and heading signals are passed as hints but the LLM makes all structural decisions. When disabled, a flat fallback is used and tree navigation is not available during retrieval.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `llm_enabled` | bool | `true` | Use LLM to build document tree with summaries (page-group strategy). Falls back to flat tree when `model` is unset, so this stays safe to keep on by default. |
| `llm_model` | string | null | Model for tree building (defaults to generator model) |
| `page_group_size` | int | `5` | Pages per group before LLM merge |
| `max_tokens_per_node` | int | `8000` | Subdivide leaf nodes exceeding this token count |
| `group_llm_max_chars` | int | `40000` | Max chars per LLM batch call |
| `min_coverage` | float | `0.80` | Minimum page coverage for quality scoring |
| `min_nodes` | int | `3` | Minimum node count for non-trivial tree |
| `max_reasonable_depth` | int | `6` | Maximum tree depth |
| `target_leaf_pages` | float | `7.0` | Target pages per leaf node |
| `summary_max_workers` | int | `4` | Parallel workers for batch summary generation |

#### `parser.normalizer`

Post-processing rules applied after parsing.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `strip_header_footer` | bool | `true` | Remove repeated page headers/footers |
| `merge_cross_page_paragraphs` | bool | `true` | Merge paragraphs split across page breaks |
| `bind_captions` | bool | `true` | Associate captions with their figures/tables |
| `resolve_references` | bool | `true` | Resolve "see Figure N" / "see Table N" cross-references |

#### `parser` (top-level)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `ingest_max_workers` | int | `10` | Maximum concurrent document ingestion workers |

---

### `storage`

Blob storage for uploaded files and generated assets (converted PDFs, figure images).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mode` | string | `"local"` | Storage backend: `local`, `s3`, `oss` |

#### `storage.local`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `root` | string | `"./storage/blobs"` | Directory for blob storage |

#### `storage.s3`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bucket` | string | — | S3 bucket name |
| `prefix` | string | `""` | Key prefix |
| `region` | string | null | AWS region |
| `endpoint_url` | string | null | Custom endpoint (for MinIO, etc.) |
| `access_key_env` | string | `"AWS_ACCESS_KEY_ID"` | Env var for access key |
| `secret_key_env` | string | `"AWS_SECRET_ACCESS_KEY"` | Env var for secret key |

#### `storage.oss`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bucket` | string | — | OSS bucket name |
| `endpoint` | string | — | OSS endpoint |
| `prefix` | string | `""` | Key prefix |
| `access_key_env` | string | `"OSS_ACCESS_KEY_ID"` | Env var for access key |
| `secret_key_env` | string | `"OSS_ACCESS_KEY_SECRET"` | Env var for secret key |

---

### `files`

File upload constraints.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_bytes` | int | `209715200` | Maximum upload size (200 MiB) |

---

### `persistence`

Database backends for relational data and vector embeddings.

#### `persistence.relational`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `"sqlite"` | Backend: `sqlite`, `postgres`, `mysql` |

**SQLite:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `persistence.relational.sqlite.path` | string | `"./storage/forgerag.db"` | Database file path |
| `persistence.relational.sqlite.journal_mode` | string | `"wal"` | Journal mode (WAL recommended) |

**PostgreSQL:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `persistence.relational.postgres.host` | string | `"localhost"` | Hostname |
| `persistence.relational.postgres.port` | int | `5432` | Port |
| `persistence.relational.postgres.database` | string | `"forgerag"` | Database name |
| `persistence.relational.postgres.user` | string | `"forgerag"` | Username |
| `persistence.relational.postgres.password_env` | string | `"POSTGRES_PASSWORD"` | Env var for password |

**MySQL:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `persistence.relational.mysql.host` | string | `"localhost"` | Hostname |
| `persistence.relational.mysql.port` | int | `3306` | Port |
| `persistence.relational.mysql.database` | string | `"forgerag"` | Database name |
| `persistence.relational.mysql.user` | string | `"forgerag"` | Username |
| `persistence.relational.mysql.password_env` | string | `"MYSQL_PASSWORD"` | Env var for password |

#### `persistence.vector`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `"chromadb"` | Backend: `chromadb`, `pgvector`, `qdrant`, `milvus`, `weaviate` |

**ChromaDB:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `persistence.vector.chromadb.mode` | string | `"persistent"` | Mode: `persistent`, `in_memory` |
| `persistence.vector.chromadb.persist_directory` | string | `"./storage/chroma"` | Persistence directory |
| `persistence.vector.chromadb.collection_name` | string | `"forgerag"` | Collection name |
| `persistence.vector.chromadb.dimension` | int | `1536` | Embedding dimension |
| `persistence.vector.chromadb.distance` | string | `"cosine"` | Distance metric |

**pgvector:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `persistence.vector.pgvector.dimension` | int | `1536` | Embedding dimension |
| `persistence.vector.pgvector.distance` | string | `"cosine"` | Distance metric: `cosine`, `l2`, `inner_product` |
| `persistence.vector.pgvector.index_type` | string | `"hnsw"` | Index type: `hnsw`, `ivfflat` |

**Qdrant:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `persistence.vector.qdrant.url` | string | `"http://localhost:6333"` | Qdrant server URL |
| `persistence.vector.qdrant.api_key` | string | null | API key (for Qdrant Cloud) |
| `persistence.vector.qdrant.collection_name` | string | `"forgerag_chunks"` | Collection name |
| `persistence.vector.qdrant.dimension` | int | `1536` | Embedding dimension |
| `persistence.vector.qdrant.distance` | string | `"cosine"` | Distance metric: `cosine`, `l2`, `ip` |
| `persistence.vector.qdrant.prefer_grpc` | bool | `false` | Use gRPC instead of HTTP |

**Milvus:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `persistence.vector.milvus.uri` | string | `"http://localhost:19530"` | Milvus server URI |
| `persistence.vector.milvus.token` | string | null | Authentication token |
| `persistence.vector.milvus.collection_name` | string | `"forgerag_chunks"` | Collection name |
| `persistence.vector.milvus.dimension` | int | `1536` | Embedding dimension |
| `persistence.vector.milvus.distance` | string | `"cosine"` | Distance metric: `cosine`, `l2`, `ip` |
| `persistence.vector.milvus.index_type` | string | `"HNSW"` | Index type: `HNSW`, `IVF_FLAT`, `FLAT` |

**Weaviate:**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `persistence.vector.weaviate.url` | string | `"http://localhost:8080"` | Weaviate server URL |
| `persistence.vector.weaviate.api_key` | string | null | API key (for Weaviate Cloud) |
| `persistence.vector.weaviate.collection_name` | string | `"ForgeragChunks"` | Collection name |
| `persistence.vector.weaviate.dimension` | int | `1536` | Embedding dimension |
| `persistence.vector.weaviate.distance` | string | `"cosine"` | Distance metric: `cosine`, `l2`, `dot` |

---

### `embedder`

Embedding model configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `"litellm"` | Backend: `litellm`, `sentence_transformers` |
| `dimension` | int | `1536` | Embedding dimension (must match vector store) |
| `batch_size` | int | `32` | Batch size for embedding requests |

#### `embedder.litellm`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | string | `"openai/text-embedding-3-small"` | Model in LiteLLM format (provider/model) |
| `api_key_env` | string | `"OPENAI_API_KEY"` | Env var for API key |
| `api_base` | string | null | Custom API base URL |

#### `embedder.sentence_transformers`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | string | `"all-MiniLM-L6-v2"` | HuggingFace model name |
| `device` | string | `"cpu"` | Compute device: `cpu`, `cuda` |

---

### `retrieval`

Controls all retrieval paths and merge strategy.

> **Always-on subsystems.** `query_understanding`, `rerank`, `kg_extraction`, and `kg_path` no longer have `enabled` toggles in v0.2.0 — they always run when the relevant infrastructure is configured (e.g. `kg_*` runs whenever a `graph` store is set). To opt out per query, pass `QueryOverrides` in the API request body (e.g. `{"overrides": {"rerank": false, "kg_path": false}}`). To opt out the whole KG layer, just omit the `graph:` config block.

#### `retrieval.query_understanding`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | string | `"openai/gpt-4o-mini"` | LLM model |
| `api_key` / `api_key_env` / `api_base` | string | null | Inline credentials (skip to inherit answer-LLM creds) |
| `max_expansions` | int | `3` | Maximum query expansions |
| `timeout` | float | `10.0` | Timeout in seconds |
| `system_prompt` | string | null | Custom system prompt |
| `user_prompt_template` | string | null | Custom user prompt template |

#### `retrieval.bm25`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable BM25 path |
| `k1` | float | `1.5` | Term frequency saturation |
| `b` | float | `0.75` | Document length normalization |
| `top_k` | int | `30` | Number of results |
| `doc_prefilter_top_k` | int | `10` | Document pre-filter for tree path |

#### `retrieval.vector`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable vector similarity path |
| `top_k` | int | `30` | Number of results |
| `default_filter` | dict | null | Metadata filter (e.g., `{"content_type": "text"}`) |

#### `retrieval.tree_path`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable tree navigation path |
| `llm_nav_enabled` | bool | `true` | Use LLM for tree navigation |
| `top_k` | int | `30` | Number of chunks from tree path |

#### `retrieval.tree_path.tree_nav`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | string | `"openai/gpt-4o-mini"` | LLM model for tree navigation |
| `temperature` | float | `0.0` | LLM temperature |
| `max_tokens` | int | `1024` | LLM max tokens |
| `timeout` | float | `30.0` | Timeout in seconds |
| `max_nodes` | int | `8` | Maximum nodes the LLM can select |
| `max_workers` | int | `5` | Parallel LLM calls |
| `target_chunks` | int | `30` | Early-stop threshold |

#### `retrieval.kg_extraction`

Runs whenever a `graph` store is configured. To skip the KG layer entirely, omit the `graph:` block in yaml. Relation descriptions and entity names are always embedded — both downstream paths (`EntityDisambiguation`, `KGPath.relation_weight` semantic search) silently degrade without them, so the toggles were dropped.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | string | `"openai/gpt-4o-mini"` | LLM model |
| `api_key` / `api_key_env` / `api_base` | string | null | Inline credentials |
| `max_workers` | int | `5` | Parallel extraction workers |
| `timeout` | float | `120.0` | Timeout per chunk |
| `merge_description_threshold` | int | `6` | Fragment count that triggers LLM description consolidation |
| `merge_description_max_chars` | int | `2000` | Char length that triggers LLM description consolidation |

#### `retrieval.kg_path`

Participates in retrieval whenever a `graph` store is configured. Per-query opt-out: `QueryOverrides.kg_path = false`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `model` | string | `"openai/gpt-4o-mini"` | LLM model for entity extraction |
| `api_key` / `api_key_env` / `api_base` | string | null | Inline credentials |
| `top_k` | int | `30` | Number of chunks from KG path |
| `max_hops` | int | `1` | Hop depth in graph traversal (1 is the safe default; 2-hop on hub entities can explode) |
| `local_weight` | float | `0.5` | Weight for local (entity-direct) chunks |
| `global_weight` | float | `0.2` | Weight for global (keyword-search) chunks |
| `relation_weight` | float | `0.1` | Weight for relation description semantic search |
| `relation_top_k` | int | `10` | Max relations matched per query |

#### `retrieval.merge`

RRF fusion and expansion strategies.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `rrf_k` | int | `60` | RRF parameter (higher = more uniform weighting) |
| `sibling_expansion_enabled` | bool | `true` | Pull in adjacent chunks |
| `sibling_max_node_size` | int | `5` | Max siblings to consider |
| `sibling_max_per_hit` | int | `3` | Max siblings added per hit |
| `sibling_score_discount` | float | `0.5` | Score multiplier for siblings |
| `crossref_expansion_enabled` | bool | `true` | Follow cross-references |
| `crossref_max_per_hit` | int | `5` | Max cross-refs per hit |
| `crossref_score_discount` | float | `0.4` | Score multiplier for cross-refs |
| `descendant_expansion_enabled` | bool | `true` | Pull child chunks for headings |
| `descendant_max_chunks` | int | `8` | Max descendants per heading |
| `descendant_score_discount` | float | `0.7` | Score multiplier for descendants |
| `descendant_min_token_threshold` | int | `80` | Chunks below this are "thin" headings |
| `global_budget_multiplier` | float | `2.0` | Expansion budget = top_k * multiplier |
| `candidate_limit` | int | `60` | Hard cap on merged candidates |

#### `retrieval.rerank`

Always runs. Per-query opt-out: `QueryOverrides.rerank = false`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `"llm_as_reranker"` | Backend: `passthrough` (no-op) / `rerank_api` (dedicated cross-encoder via `litellm.rerank()`) / `llm_as_reranker` (chat LLM as judge) |
| `on_failure` | string | `"strict"` | `"strict"` raises `RerankerError` on failure; `"passthrough"` falls back to RRF order silently |
| `model` | string | `"openai/gpt-4o-mini"` | For `llm_as_reranker` use a chat model. For `rerank_api` use a litellm-rerank-compatible prefix: `infinity/`, `cohere/`, `jina_ai/`, `voyage/`, `together_ai/`. SiliconFlow's BGE rerank works as `infinity/BAAI/bge-reranker-v2-m3` + `api_base=https://api.siliconflow.cn/v1`. |
| `api_key` / `api_key_env` / `api_base` | string | null | Inline credentials |
| `top_k` | int | `10` | Results after reranking |
| `timeout` | float | `30.0` | Timeout in seconds |
| `snippet_chars` | int | `500` | Per-candidate snippet budget (only for `llm_as_reranker`) |

#### `retrieval.citations`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_snippet_chars` | int | `200` | Maximum characters per citation snippet |
| `open_url_template` | string | `"/viewer/{doc_id}?page={page_no}&hl={citation_id}"` | URL template for citation links |

---

### `answering`

Controls answer generation.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_chunks` | int | `10` | Maximum context chunks sent to LLM |
| `include_expanded_chunks` | bool | `true` | Include sibling/crossref expansion chunks |

#### `answering.generator`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `"litellm"` | Generator backend |
| `model` | string | `"openai/gpt-4o-mini"` | LLM model |
| `temperature` | float | `0.1` | Generation temperature |
| `max_tokens` | int | `2048` | Maximum generation tokens |
| `timeout` | float | `60.0` | Timeout in seconds |
| `api_key_env` | string | null | Env var for API key |
| `api_base` | string | null | Custom API base URL |
| `chunk_chars` | int | `1500` | Per-chunk character budget in context |
| `max_context_chars` | int | `20000` | Hard ceiling on total context |
| `refuse_when_unknown` | bool | `true` | Refuse to answer if no relevant context |
| `refuse_message` | string | `"I don't know based on the provided documents."` | Refusal message |
| `system_prompt` | string | null | Custom system prompt (overrides default) |
| `user_prompt_template` | string | null | Custom user prompt template |

---

### `graph`

Knowledge graph backend.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `"networkx"` | Backend: `networkx`, `neo4j` |

#### `graph.neo4j`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `uri` | string | `"bolt://localhost:7687"` | Neo4j Bolt URI |
| `user` | string | `"neo4j"` | Username |
| `password_env` | string | `"NEO4J_PASSWORD"` | Env var for password |

---

### `image_enrichment`

Optional figure captioning and OCR.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Enable image enrichment |
| `caption_model` | string | null | Model for figure captioning |
| `ocr_model` | string | null | Model for OCR on images |

---

### `cors`

Cross-Origin Resource Sharing settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `allow_origins` | list | `["*"]` | Allowed origins |
| `allow_methods` | list | `["*"]` | Allowed HTTP methods |
| `allow_headers` | list | `["*"]` | Allowed headers |
| `allow_credentials` | bool | `true` | Allow credentials |

> **Production:** Restrict `allow_origins` to your domain.

---

### `cache`

Cache paths for BM25 index and embedding cache.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `bm25_persistence` | string | `"./storage/bm25_index.pkl"` | BM25 index file path |
| `embedding_cache` | string | `""` | Embedding cache path (empty = disabled) |

---

## Environment Variables

Credentials should **never** be stored in `forgerag.yaml`. Use environment variables instead:

| Variable | Used by | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | Embedder, Generator, Tree Nav | OpenAI API key |
| `FORGERAG_CONFIG` | main.py | Config file path |
| `FORGERAG_HOST` | main.py | Server bind address |
| `FORGERAG_PORT` | main.py | Server bind port |
| `POSTGRES_PASSWORD` | Relational store | PostgreSQL password |
| `MYSQL_PASSWORD` | Relational store | MySQL password |
| `NEO4J_PASSWORD` | Graph store | Neo4j password |
| `AWS_ACCESS_KEY_ID` | S3 blob store | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | S3 blob store | AWS secret key |
| `OSS_ACCESS_KEY_ID` | OSS blob store | Alibaba OSS access key |
| `OSS_ACCESS_KEY_SECRET` | OSS blob store | Alibaba OSS secret key |

The `api_key_env` / `password_env` pattern throughout the config refers to environment variable names, not literal values. For example, `api_key_env: OPENAI_API_KEY` tells ForgeRAG to read the key from `$OPENAI_API_KEY`.

---

## Example: Minimal Config

Yaml is the single source of truth: model + api_key + api_base are inlined directly under each subsystem (no `provider_id` indirection). The retrieval subsystems inherit from `answering.generator` if you don't override them.

Set `$OPENAI_API_KEY`, then:

```yaml
# forgerag.yaml — minimum viable
embedder:
  backend: litellm
  dimension: 1536
  litellm:
    model: openai/text-embedding-3-small
    api_key_env: OPENAI_API_KEY

answering:
  generator:
    backend: litellm
    model: openai/gpt-4o-mini
    api_key_env: OPENAI_API_KEY
```

Run `python scripts/setup.py` to generate this interactively — the wizard also walks through every retrieval subsystem (query_understanding / rerank / kg_extraction / kg_path / tree_path.nav) so you can override the model per-subsystem (e.g. cheap-and-fast for kg_extraction, strong for tree_path.nav). Subsystems left without overrides reuse the answer-LLM credentials.

## Example: Production Config (PostgreSQL + pgvector + S3 + dedicated reranker)

```yaml
persistence:
  relational:
    backend: postgres
    postgres:
      host: db.example.com
      port: 5432
      database: forgerag
      user: forgerag
      password_env: POSTGRES_PASSWORD
  vector:
    backend: pgvector
    pgvector:
      dimension: 1536
      distance: cosine
      index_type: hnsw

storage:
  mode: s3
  s3:
    bucket: my-forgerag-bucket
    region: us-east-1
    access_key_env: AWS_ACCESS_KEY_ID
    secret_key_env: AWS_SECRET_ACCESS_KEY

embedder:
  backend: litellm
  dimension: 1536
  litellm:
    model: openai/text-embedding-3-large
    api_key_env: OPENAI_API_KEY

answering:
  generator:
    backend: litellm
    model: openai/gpt-4o
    api_key_env: OPENAI_API_KEY

retrieval:
  rerank:
    backend: rerank_api
    model: cohere/rerank-english-v3.0
    api_key_env: COHERE_API_KEY
    top_k: 10

cors:
  allow_origins:
    - "https://app.example.com"
```
