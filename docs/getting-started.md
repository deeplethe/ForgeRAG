# Getting Started

This guide walks you through installing OpenCraig, running it locally, and ingesting your first document.

## Two ways to use OpenCraig

Once installed, OpenCraig serves two distinct usage modes — pick
either or both depending on how your team wants to interact with
the corpus.

| Mode | What you do | Who hits what |
|------|-------------|--------------|
| **Web UI + chat** | Open `http://localhost:8000` in a browser, log in, drop documents in the Workspace, ask questions in the Chat tab | Browser → `/api/v1/agent/chat` (the in-process Claude Agent SDK + MCP server) |
| **External agent + MCP** | Configure your own agent runtime (Claude Code, Cursor, Cline, custom MCP client) to point at OpenCraig as a knowledge backend | Agent → `/api/v1/mcp` for retrieval tools (per-user authz applied automatically); optionally → `/api/v1/llm/v1/chat/completions` for OpenAI-compatible LLM calls routed through your provider keys |

Both modes share the same multi-user permission topology: an agent
running on behalf of `alice` only ever sees docs in folders alice
has been granted, regardless of which mode is in use. See
[`auth.md`](auth.md) for the path-as-authz model.

## Prerequisites

| Requirement | Version | Notes |
|------------|---------|-------|
| Python | 3.10+ | 3.11 recommended |
| Node.js | 18+ | Only needed for building the frontend |
| pip | latest | `pip install --upgrade pip` |

You also need an API key for at least one LLM provider (OpenAI, DeepSeek, Cohere, etc.) or a local Ollama instance.

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/opencraig/opencraig.git
cd opencraig
```

### 2. Create a virtual environment

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` ships only **core** dependencies (FastAPI, SQLAlchemy, LiteLLM, PyMuPDF, etc.) — every optional backend (PostgreSQL, Neo4j, ChromaDB, Qdrant, Milvus, Weaviate, S3, OSS, MinerU, FAISS, …) is auto-installed by the setup wizard in step 5 based on the choices you make.

If you skip the wizard and edit `opencraig.yaml` directly, re-sync deps with:

```bash
python scripts/setup.py --sync-deps opencraig.yaml
```

This reads the yaml and pip-installs only the optional packages your config actually uses. No need to memorise the pip names per backend.

### 4. Build the frontend

```bash
cd web
npm install
npm run build
cd ..
```

The built files are placed in `web/dist/` and served automatically by the backend.

### 5. Run the setup wizard

```bash
python scripts/setup.py
```

The wizard is bilingual (EN / 中文, switchable from the first menu) and walks through 13 steps with arrow-key navigation:

1. Relational database (SQLite / PostgreSQL)
2. Vector database (ChromaDB / pgvector / Qdrant / Milvus / Weaviate)
3. Blob storage (local / S3 / OSS)
4. Knowledge graph database (NetworkX / Neo4j)
5. PDF parser (`pymupdf` / `mineru` / `mineru-vlm`)
6. Embedding model — wizard runs a live API call and **auto-detects the embedding dimension** from the response
7. Answer-generation LLM — wizard runs a live completion test
8–12. Per-subsystem LLM routing for query_understanding / rerank / kg_extraction / kg_path / tree_path.nav. Each step opens with the cost / latency / quality rationale for overriding; reuse the answer-LLM by default.
13. Image enrichment (optional VLM-based figure OCR + description)

State is checkpointed after every step, so a crash mid-wizard (Ctrl-C, pip install failure, network blip) re-asks "resume or restart?" on the next run. After completing the wizard, optional pip installs run automatically based on yaml choices.

Put real credentials directly when prompted (saved plaintext into yaml — keep `opencraig.yaml` out of git, it's already gitignored), or leave the API-key prompt blank to use an environment variable. The wizard suggests a sensible env-var name based on the model's provider prefix (e.g. `deepseek/...` → `DEEPSEEK_API_KEY`).

### 6. Start the server

```bash
python main.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser. The first request runs schema migrations and warms the BM25 index.

## CLI Options

```bash
python main.py [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--config PATH` | auto-detect | Path to `opencraig.yaml` |
| `--host HOST` | `0.0.0.0` | Bind address (or `$FORGERAG_HOST`) |
| `--port PORT` | `8000` | Bind port (or `$FORGERAG_PORT`) |
| `--reload` | off | Hot-reload on code changes (development) |
| `--workers N` | `1` | Uvicorn worker processes. Values > 1 require multi-process-safe backends (PostgreSQL + Neo4j + non-persistent vector store); startup exits with code 2 if SQLite, NetworkX, or persistent ChromaDB are configured. |
| `--log-level LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `--init-only` | off | Write default config and exit |

## Your First Document

1. Navigate to the **Workspace** tab in the web UI
2. Drag and drop a PDF (or DOCX, PPTX, HTML, Markdown, an image — PNG/JPG/WEBP/GIF/BMP/TIFF — or a spreadsheet — XLSX/CSV/TSV) onto the page, or click the **+** icon to select files. Drop into a specific folder by dragging onto its tile

   > Image uploads need `image_enrichment.enabled = true` in `opencraig.yaml` — without a VLM the image is stored but never described, so retrieval can't find it. The wizard configures this if you pick a vision model.

   > Spreadsheet uploads need `table_enrichment.enabled = true` in `opencraig.yaml` — same shape as image uploads: each sheet becomes one TABLE block whose embedded text is an LLM-generated description. The full data is preserved on the side for the inline viewer; without an LLM there's no description and retrieval can't find the doc.
3. The document is queued for ingestion. The card shows an amber chip with the current pipeline stage (parsing → embedding → building graph)
4. Once the chip clears to **ready**, switch to the **Chat** tab
5. Ask a question about the document. OpenCraig returns a streaming answer with `[c_N]` citations — click any citation to jump to the source PDF at the exact bounding box

## What's Next

- [Configuration Reference](configuration.md) — customize backends, models, and retrieval parameters
- [Architecture Overview](architecture.md) — understand how ingestion, retrieval, and answering work
- [Deployment Guide](deployment.md) — run OpenCraig with Docker or in production
- [API Reference](api-reference.md) — integrate OpenCraig into your own applications
