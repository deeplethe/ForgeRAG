# Development Guide

This guide covers setting up a development environment, project conventions, testing, and contributing.

## Development Setup

### Prerequisites

- Python 3.10+ (3.11 recommended)
- Node.js 18+
- Git

### Install

```bash
git clone https://github.com/deeplethe/OpenCraig.git
cd OpenCraig

# Python
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Frontend
cd web
npm install
cd ..
```

### Run in Development Mode

**Backend** (with hot-reload):

```bash
export OPENAI_API_KEY=sk-...
python main.py --reload
```

**Frontend** (Vite dev server with HMR):

```bash
cd web
npm run dev
```

The Vite dev server proxies API requests to `localhost:8000`. Open [http://localhost:5173](http://localhost:5173) for frontend development with instant hot-module-replacement.

For production-like testing, build the frontend and let the backend serve it:

```bash
cd web && npm run build && cd ..
python main.py
# Open http://localhost:8000
```

---

## Project Conventions

### Python

- **Style:** Enforced by [ruff](https://docs.astral.sh/ruff/) — `ruff check .` and `ruff format .`
- **Type hints:** Use type annotations for function signatures
- **Config:** Pydantic models (strict validation, JSON-serializable)
- **Database:** SQLAlchemy 2.0 ORM with explicit session management
- **Async:** The API is synchronous (uvicorn handles concurrency); background work uses threading

### Ruff Configuration

```toml
# ruff.toml
target-version = "py310"
line-length = 120

[lint]
select = ["E", "W", "F", "I", "UP", "B", "SIM", "RUF"]
```

### Frontend

- **Framework:** Vue 3 with Composition API (`<script setup>`)
- **Styling:** Tailwind CSS with custom design tokens (`text-t1`, `bg-bg2`, `border-line`, `text-brand`, etc.)
- **State:** Component-local `ref()` / `reactive()`, no global store
- **API calls:** Thin wrappers in `web/src/api/` around `fetch()`

### Design Tokens

The frontend uses semantic color tokens defined in Tailwind config:

| Token | Purpose |
|-------|---------|
| `text-t1` | Primary text |
| `text-t2` | Secondary text |
| `text-t3` | Tertiary / muted text |
| `bg-bg` | Page background |
| `bg-bg2` | Card / elevated background |
| `bg-bg3` | Hover / active background |
| `border-line` | Borders and dividers |
| `text-brand` | Accent / brand color |

---

## Testing

### Run All Tests

```bash
python -m pytest tests/ -v
```

### Run Specific Test File

```bash
python -m pytest tests/test_chunker.py -v
python -m pytest tests/test_retrieval_pipeline.py -v
```

### Test Structure

Tests use **pytest** with fixtures defined in `tests/conftest.py`. Most tests use **fake/stub implementations** instead of real backends:

| Test file | Coverage |
|-----------|----------|
| `test_answering.py` | Answering pipeline (prompt building, citation parsing) |
| `test_api.py` | Full API integration (upload, ingest, query, delete) |
| `test_blob_store.py` | File storage key generation, content addressing |
| `test_chunker.py` | Chunk generation (greedy packing, table isolation, cross-refs) |
| `test_config.py` | Config loading, validation, defaults |
| `test_embedder.py` | Embedder interface, batch processing |
| `test_embedder_backfill.py` | Re-embedding on model change |
| `test_file_store.py` | File upload, dedup, download |
| `test_ingestion_pipeline.py` | End-to-end ingestion (parse → tree → chunk → embed) |
| `test_normalizer.py` | Header/footer removal, caption binding |
| `test_persistence_config.py` | Database configuration resolution |
| `test_persistence_serde.py` | Row ↔ dataclass serialization |
| `test_probe.py` | Document profiling (density, scanned ratio) |
| `test_pymupdf_backend.py` | PyMuPDF parsing (real PDFs in `tests/pdfs/`) |
| `test_retrieval_bm25.py` | BM25 indexing and search |
| `test_retrieval_merge.py` | RRF fusion, sibling/crossref/descendant expansion |
| `test_retrieval_pipeline.py` | End-to-end retrieval (BM25 + vector + tree) |
| `test_router.py` | Parser backend routing and fallback chains |
| `test_sqlite_store.py` | SQLite persistence (schema, CRUD, versioning) |
| `test_tree_builder.py` | Tree building (TOC, headings, fallback, quality scoring) |

### Writing Tests

- Place test files in `tests/` with `test_` prefix
- Use fake stores/embedders for unit tests (see existing fakes in test files)
- Place test PDFs in `tests/pdfs/`
- Use fixtures from `conftest.py` for common setup

---

## Linting

```bash
# Check
ruff check .

# Auto-fix
ruff check . --fix

# Format
ruff format .
```

Ruff is configured in `ruff.toml` at the project root.

---

## Database Migrations

OpenCraig uses [Alembic](https://alembic.sqlalchemy.org/) for schema migrations.

### Create a Migration

After modifying ORM models in `persistence/models.py`:

```bash
alembic revision --autogenerate -m "Add new column to documents"
```

### Apply Migrations

```bash
alembic upgrade head
```

### Check Status

```bash
alembic current
alembic history
```

### Alembic Configuration

- `alembic.ini` — connection settings
- `alembic/env.py` — reads DB URL from OpenCraig config (respects `$FORGERAG_CONFIG`)
- `alembic/script.py.mako` — migration template

---

## CI/CD

GitHub Actions CI runs on every push and PR (`.github/workflows/ci.yml`):

| Job | What it does |
|-----|-------------|
| **lint** | `ruff check .` + `ruff format --check .` |
| **test** | `pytest tests/ -v` |
| **frontend** | `npm ci` + `npm run build` |

All three jobs run in parallel.

---

## Adding a New Backend

OpenCraig uses abstract base classes for pluggable backends. To add a new backend:

### Vector Store

1. Create `persistence/vector/my_store.py`
2. Implement the `VectorStore` abstract class from `persistence/vector/base.py`:
   - `embed_and_upsert(chunks, embedder)`
   - `search(query, embedder, top_k)`
   - `delete_doc(doc_id)`
3. Register in `api/state.py` factory logic
4. Add config model in `config/persistence.py`

### Parser Backend

1. Create `parser/backends/my_parser.py`
2. Implement `ParserBackend` from `parser/backends/base.py`:
   - `parse(path, doc_id, parse_version)` → list of `Block`
   - `quality_score()` → float
3. Register in `parser/router.py`
4. Add config in `config/parser.py`

### Graph Store

1. Create `graph/my_store.py`
2. Implement `GraphStore` from `graph/base.py`:
   - `upsert_entity()`, `upsert_relation()`
   - `get_entity()`, `get_neighbors()`, `search_entities()`
   - `get_subgraph()`, `delete_by_doc()`
3. Register in `api/state.py`
4. Add config in `config/app.py`

### Blob Storage

1. Create a new storage module
2. Implement the blob store interface (store, retrieve, delete)
3. Register in `api/state.py`
4. Add config in `config/app.py` storage section

---

## Key Design Decisions

### Why Tree-Aware Chunking?

Traditional RAG systems split documents into fixed-size chunks, often breaking mid-sentence or mid-section. OpenCraig builds a hierarchical tree first, then chunks within tree nodes. This means:

- Chunks respect section boundaries
- Each chunk carries `section_path` and `ancestor_node_ids` for context
- The tree enables structural retrieval (navigating to relevant sections)

### Why Multi-Path Retrieval?

No single retrieval method works for all queries:
- **BM25** excels at exact keyword matches
- **Vector search** captures semantic similarity
- **Tree navigation** finds structurally relevant sections
- **KG path** discovers entity relationships

RRF fusion combines these diverse signals for robust recall.

### Why Content-Addressed File Storage?

Files are stored by SHA256 hash. Uploading the same file twice creates one blob, two references. This saves storage and enables efficient dedup.

### Why YAML-only Configuration?

Earlier versions kept "runtime" settings editable in the DB via the web UI. We removed that because:

* **Two sources of truth drift.** YAML said one thing, DB said another, users forgot which was current. Every restart became a reconciliation ritual.
* **Per-request overrides are the real need.** When a caller wants "skip QU for this query" or "bump top-k just this once", mutating a shared DB knob is the wrong tool — another concurrent request has to suffer the side-effect. `QueryOverrides` on `/api/v1/query` solves this cleanly; no global mutation.
* **Secrets don't belong in a checkbox.** API keys live in `api_key_env` referencing an environment variable, not in a settings row an HTTP PUT can overwrite.

The DB still has a `settings` table that gets a **write-once snapshot** of the running cfg — surfaced by `GET /api/v1/settings` for admin tools, never read back by the runtime.
