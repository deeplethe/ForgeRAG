<p align="center">
  <img src="web/public/text_logo_padding.png" alt="ForgeRAG" height="64">
</p>

<h2 align="center">Production-Ready RAG with Structure-Aware Reasoning</h2>

<p align="center">
  <strong>LLM Tree Reasoning</strong> ◦ <strong>Knowledge Graph Multi-Hop</strong> ◦ <strong>Pixel-Precise Citations</strong> ◦ <strong>Unmatched Performance</strong>
</p>

<p align="center">
  <a href="https://github.com/deeplethe/ForgeRAG/releases"><img src="https://img.shields.io/badge/version-0.2.1-brightgreen?style=for-the-badge" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://github.com/deeplethe/ForgeRAG"><img src="https://img.shields.io/github/stars/deeplethe/ForgeRAG?style=for-the-badge" alt="Stars"></a>
  <a href="https://github.com/deeplethe/ForgeRAG/issues"><img src="https://img.shields.io/github/issues/deeplethe/ForgeRAG?style=for-the-badge" alt="Issues"></a>
  <a href="docs/"><img src="https://img.shields.io/badge/Docs-docs%2F-blue?style=for-the-badge" alt="Docs"></a>
  <a href="https://discord.gg/XJadJHvxdQ"><img src="https://img.shields.io/badge/Discord-Join-7289da?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> •
  <a href="#features">Features</a> •
  <a href="#technical-approach">Technical Approach</a> •
  <a href="docs/">Docs</a> •
  <a href="./README_CN.md">中文</a>
</p>

---

<p align="center"><img src="docs/images/architecture.png" alt="ForgeRAG Architecture" width="700"></p>

### The Problem with Existing Approaches

Many approaches have been proposed to go beyond naive chunk-and-embed RAG, but each has fundamental limitations:

| Approach | Strength | Limitation |
|----------|----------|------------|
| **Embedding-based** (e.g. naive RAG) | Fast semantic search | Similarity ≠ relevance; misses exact-match and structural context |
| **Graph-based** (e.g. GraphRAG) | Cross-document entity linking | Concept skeleton without source-text evidence; extraction loses details |
| **Hybrid graph** (e.g. LightRAG) | Dual-level retrieval (local + global) | Answers synthesized from KG summaries, not grounded in original text; higher hallucination risk |
| **Reasoning-based** (e.g. PageIndex) | High single-doc accuracy | Query latency scales linearly with document count; not production-ready |

### Our Approach: Think Like a Domain Expert

When a domain expert encounters a question, they don't scan every page — they instantly recall where relevant information lives, draw on their mental map of how concepts connect, then synthesize a grounded answer from multiple sources. ForgeRAG mirrors this workflow: **BM25 + vector search** surfaces candidate regions in milliseconds, a **knowledge graph** provides the conceptual connections across documents, and **LLM tree navigation** reasons over document structure to pinpoint the exact sections that matter — all fused into a single answer with traceable citations.

To handle **multi-hop questions** (e.g. *"Which suppliers of Apple also supply Samsung?"*), we introduce a **knowledge graph** path that extracts entities and relations at ingestion time, then runs dual-level retrieval at query time: **local** (query entities → neighborhood traversal) and **global** (keywords → fuzzy / cross-lingual entity match via name embeddings), plus **relation-semantic** search over relation-description embeddings. Inspired by LightRAG's context assembly, the KG path injects **synthesized entity and relation descriptions** directly into the generation prompt — giving the LLM a "distilled knowledge layer" on top of raw text chunks.

### Benchmark: ForgeRAG vs LightRAG

We evaluate against [LightRAG](https://github.com/HKUDS/LightRAG) using the **UltraDomain** benchmark methodology (LLM-as-judge pairwise comparison). Win rates shown as **ForgeRAG% / LightRAG%**.

> 🚧 **More comprehensive benchmarks against additional RAG systems, domains, and metrics are in progress.**

| Domain | Comprehensiveness | Diversity | Empowerment | Overall |
|--------|:-----------------:|:---------:|:-----------:|:-------:|
| **Agriculture** | **58.6** / 41.4 | 47.1 / **52.9** | **52.9** / 47.1 | **56.4** / 43.6 |
| **CS** | **55.6** / 44.4 | 48.4 / **51.6** | **54.0** / 46.0 | **54.8** / 45.2 |
| **Legal** | **57.0** / 43.0 | 46.5 / **53.5** | **53.5** / 46.5 | **55.6** / 44.4 |
| **Mix** | **56.3** / 43.7 | 47.8 / **52.2** | **54.3** / 45.7 | **55.1** / 44.9 |

<sub>Judge: qwen3-max · [Reproduce](scripts/compare_bench.py)</sub>

> **Note on Faithfulness:** The UltraDomain benchmark evaluates Comprehensiveness, Diversity, and Empowerment — but not factual accuracy. ForgeRAG provides pixel-precise `[c_N]` citations for every claim, enabling verification against source text. LightRAG synthesizes answers from knowledge graph summaries without traceable citations, which scores well on breadth but carries higher hallucination risk.

## Features

<p align="center"><img src="docs/images/chat_demo.gif" alt="ForgeRAG Demo" width="700"></p>

Compared to heavier platforms like RAGFlow, ForgeRAG focuses on **core pipeline design** — a lean retrieval-answering chain with composable building blocks.

🔍 **Dual-reasoning retrieval** · BM25 + vector pre-filter → LLM tree nav + KG, fused via RRF

📌 **Pixel-precise citations** · Every claim links to exact page + bounding box, click to highlight

🔗 **Full retrieval tracing** · Inspect path scores, expansion decisions, and merge logic per query

💬 **Multi-turn conversations** · Context-aware follow-ups with conversation history

📄 **Multi-format ingestion** · PDF, DOCX, PPTX, XLSX, HTML, Markdown, TXT

⚙️ **YAML-first config** · One file, one restart — no hidden runtime state

🎛️ **Per-request overrides** · Toggle retrieval paths / top-ks / rerank per query via `QueryOverrides` (great for SDK + A/B)

🏆 **Outperforms LightRAG** · 55.48% overall win rate on UltraDomain benchmark

<details>
<summary><strong><font size="4">📸 Screenshots</font></strong></summary>
<br/>

**Chat** · Structured answers with pixel-precise citations

<img src="docs/screenshots/chat_sample.png" alt="Chat" width="700">

**Ingestion** · Document processing pipeline with tree building

<img src="docs/screenshots/ingest_demo.png" alt="Ingestion" width="700">

**Knowledge Graph** · Entity-relation visualization

<img src="docs/screenshots/kg_demo.png" alt="Knowledge Graph" width="700">

</details>

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+ (for building the frontend)
- An LLM API key (OpenAI, DeepSeek, or any LiteLLM-compatible provider)
- Recommended: 4+ CPU cores, 8GB+ RAM (16GB+ for large documents with KG extraction)

### Option A: Local Development

```bash
git clone https://github.com/deeplethe/ForgeRAG.git
cd ForgeRAG

# 1. Core Python dependencies (small — the heavy backend packages are
#    installed lazily in step 3 based on what your config actually picks).
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Frontend
cd web && npm install && npm run build && cd ..

# 3. Configure: interactive wizard generates forgerag.yaml AND auto-pip-installs
#    the backend-specific deps your choices need (e.g. chromadb, neo4j, mineru).
#    To re-sync deps after a manual yaml edit: python scripts/setup.py --sync-deps forgerag.yaml
python scripts/setup.py

# 4. Run — defaults to a single worker (safe with the wizard's default
#    SQLite + ChromaDB-persistent + NetworkX backends).
python main.py
```

Open [http://localhost:8000](http://localhost:8000) — the web UI is served automatically.

> **Note:** Document ingestion involves heavy LLM calls (tree building, KG extraction, embedding). For a responsive UI under concurrent ingestion, scale to multiple workers — but `--workers >1` requires multi-process-safe backends (PostgreSQL + Neo4j + a non-persistent ChromaDB / Qdrant / Milvus / Weaviate / pgvector). Starting with `--workers >1` against single-process backends (SQLite, NetworkX, persistent ChromaDB) exits with code 2 to avoid silent data corruption.

### Option B: Docker Deployment

```bash
git clone https://github.com/deeplethe/ForgeRAG.git
cd ForgeRAG

python scripts/docker_setup.py   # Interactive wizard: pick provider, set keys, done
docker compose up -d             # PostgreSQL + pgvector + ForgeRAG, ready to go
```

Open [http://localhost:8000](http://localhost:8000). See [Deployment Guide](docs/deployment.md) for details.

> **Tip:** We strongly recommend enabling **MinerU** — it significantly improves document structure parsing accuracy, especially for PDFs with complex layouts, tables, and formulas. Enable it in the web UI settings after startup.

### Supported Backends

| Component | Options |
|-----------|---------|
| **PDF Parser** | One explicit choice: `pymupdf` (fast, default) / `mineru` (layout-aware, tables/formulas) / `mineru-vlm` (vision-language for scanned & complex layouts) |
| **Relational DB** | SQLite (default), PostgreSQL, MySQL |
| **Vector Store** | ChromaDB (default), pgvector (PostgreSQL), Qdrant, Milvus, Weaviate |
| **Blob Storage** | Local filesystem (default), Amazon S3, Alibaba OSS |
| **Graph Store** | NetworkX in-memory (default), Neo4j |
| **LLM / Embeddings** | Any [LiteLLM](https://docs.litellm.ai/docs/providers)-supported provider: OpenAI, Azure, Anthropic, Ollama, DeepSeek, Cohere, etc. |

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | auto-detect | Path to `forgerag.yaml` |
| `--host` | `0.0.0.0` | Bind address (or `$FORGERAG_HOST`) |
| `--port` | `8000` | Bind port (or `$FORGERAG_PORT`) |
| `--reload` | off | Hot-reload for development |
| `--workers` | `1` | Uvicorn workers. Values > 1 require multi-process-safe backends (PostgreSQL + Neo4j + non-persistent vector store); startup exits 2 otherwise. |

## Architecture

The diagram above shows the complete data flow. For detailed pipeline documentation with per-node annotations, see [Architecture Overview](docs/architecture.md).

## API

The REST API is available at `/api/v1/`. Interactive docs:

- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)

Key endpoints:

| Endpoint | Description |
|----------|-------------|
| `POST /api/v1/query` | Ask a question (streaming SSE or sync) — accepts `path_filter` + `overrides` for per-request tuning |
| `POST /api/v1/documents/upload-and-ingest` | Upload into a folder (multipart; `folder_path` form field) |
| `GET  /api/v1/documents?path_filter=…&recursive=…` | List docs under a folder |
| `GET  /api/v1/documents/{id}/tree` | Document hierarchical structure |
| `GET  /api/v1/graph` | Knowledge graph visualization |
| `GET  /api/v1/settings` | Read-only snapshot of effective cfg (yaml is authoritative) |

## Documentation

- **[Getting Started](docs/getting-started.md)** — Installation, first document, step-by-step guide
- **[Architecture Overview](docs/architecture.md)** — How ingestion, retrieval, and answering pipelines work
- **[Configuration Reference](docs/configuration.md)** — Every config option with defaults and examples
- **[API Reference](docs/api-reference.md)** — REST API endpoints, request/response formats, SSE streaming
- **[Deployment Guide](docs/deployment.md)** — Docker deploy, production checklist, Nginx, Ollama
- **[Development Guide](docs/development.md)** — Dev setup, testing, adding new backends
- **[Auth & Sessions](docs/auth.md)** — Single-admin password + SK tokens, web management UI, CLI playbook

## Project Structure

```
ForgeRAG/
├── api/              # FastAPI routes and schemas
├── answering/        # Answer generation pipeline
├── config/           # Pydantic configuration models
├── embedder/         # Embedding backends (LiteLLM, sentence-transformers)
├── graph/            # Knowledge graph stores (NetworkX, Neo4j)
├── ingestion/        # Document ingestion pipeline + format conversion
├── parser/           # PDF parsing, chunking, tree building
├── persistence/      # Database layer (relational, vector, blob)
├── retrieval/        # Retrieval pipeline (BM25, vector, tree, KG, merge)
├── scripts/          # CLI utilities (setup wizard, Docker setup, batch ingest)
├── web/              # Vue 3 frontend
├── docs/             # Detailed documentation
├── main.py           # Application entry point
└── forgerag.yaml     # Your local config (git-ignored)
```

## Roadmap

- [ ] 🧪 More benchmarks against additional RAG systems and domains
- [ ] 🔄 Scale to 1M+ documents · incremental indexing, async KG
- [ ] 🌐 Multi-language retrieval · cross-lingual query and document support
- [ ] 📦 Python SDK · `pip install forgerag-sdk`
- [ ] 🛠️ Config panel hints & diagnostics · Missing provider warnings, validation feedback
- [ ] ⚡ Performance optimization · Faster ingestion, query caching, async embedding

## Contributing

We welcome contributions of all kinds — bug fixes, new features, documentation improvements, and more.

Please read our [Contributing Guide](CONTRIBUTING.md) before submitting a pull request.

## Related Projects

- [LightRAG](https://github.com/HKUDS/LightRAG) — Graph-based RAG with dual-level (local + global) retrieval
- [GraphRAG](https://github.com/microsoft/graphrag) — Microsoft's graph-powered RAG with community summaries
- [PageIndex](https://github.com/VectifyAI/PageIndex) — Reasoning-based vectorless retrieval

## License

[MIT License](LICENSE)
