# Getting Started

This guide walks you through installing ForgeRAG, running it locally, and ingesting your first document.

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
git clone https://github.com/deeplethe/ForgeRAG.git
cd ForgeRAG
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

The base `requirements.txt` includes only the core dependencies. Optional backends need extra packages:

| Backend | Extra packages | Install command |
|---------|----------------|-----------------|
| PostgreSQL | `psycopg[binary]` | `pip install "psycopg[binary]>=3.1"` |
| pgvector | `psycopg[binary]` | (same as above) |
| MySQL | `pymysql` | `pip install "pymysql>=1.1"` |
| Neo4j | `neo4j` | `pip install "neo4j>=5.0"` |
| S3 storage | `boto3` | `pip install "boto3>=1.34"` |
| Alibaba OSS | `oss2` | `pip install "oss2>=2.18"` |
| Local embeddings | `sentence-transformers` | `pip install "sentence-transformers>=3.0"` |
| MinerU parser | `mineru` | `pip install mineru` |

### 4. Build the frontend

```bash
cd web
npm install
npm run build
cd ..
```

The built files are placed in `web/dist/` and served automatically by the backend.

### 5. Set your API key

```bash
# Linux / macOS
export OPENAI_API_KEY=sk-...

# Windows PowerShell
$env:OPENAI_API_KEY = "sk-..."
```

Or copy `.env.example` to `.env` and fill in your key:

```bash
cp .env.example .env
```

### 6. Start the server

```bash
python main.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

On first boot, if no `forgerag.yaml` exists, ForgeRAG writes a skeleton you must finish filling in:

- **Infrastructure defaults**: PostgreSQL, ChromaDB, local blob storage (paths under `./storage/`)
- **LLM providers**: **empty** — add at least one `chat` and one `embedding` entry under `llm_providers:` and point `embedder.provider_id` / `answering.generator.provider_id` at them. Put real credentials in environment variables referenced by `api_key_env` (never in yaml). See the [minimal config example](configuration.md#example-minimal-config).

The server will boot without providers, but any retrieval call (embedding, LLM answer) will fail explicitly until you configure them.

## CLI Options

```bash
python main.py [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--config PATH` | auto-detect | Path to `forgerag.yaml` |
| `--host HOST` | `0.0.0.0` | Bind address (or `$FORGERAG_HOST`) |
| `--port PORT` | `8000` | Bind port (or `$FORGERAG_PORT`) |
| `--reload` | off | Hot-reload on code changes (development) |
| `--workers N` | `4` | Uvicorn worker processes (each runs its own ingestion queue; stuck jobs auto-recover on restart) |
| `--log-level LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `--init-only` | off | Write default config and exit |

## Your First Document

1. Navigate to the **Repository** tab in the web UI
2. Drag and drop a PDF (or DOCX, PPTX, XLSX, HTML, Markdown) onto the page, or click the **+** icon to select files
3. The document is automatically queued for ingestion. Watch the toast notification for upload progress
4. Once status shows **ready**, switch to the **Chat** tab
5. Ask a question about the document. ForgeRAG returns an answer with highlighted source citations

## What's Next

- [Configuration Reference](configuration.md) — customize backends, models, and retrieval parameters
- [Architecture Overview](architecture.md) — understand how ingestion, retrieval, and answering work
- [Deployment Guide](deployment.md) — run ForgeRAG with Docker or in production
- [API Reference](api-reference.md) — integrate ForgeRAG into your own applications
