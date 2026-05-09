<p align="center">
  <img src="web/public/craig.png" alt="OpenCraig" width="160">
</p>

<h1 align="center">OpenCraig</h1>
<h2 align="center">Free your team from the laptop.</h2>
<h4 align="center">Managed agentic workspaces — per-user sandbox containers, permission-aware retrieval, MCP-native, BYOK.</h4>

<p align="center">
  Each user gets a managed Linux sandbox where the agent does the work — reading PDFs, running code, drafting reports — instead of asking them to do it on their own machine. Retrieval over your team's knowledge base respects existing folder permissions; the agent only sees what its authenticated user can see. Self-hosted, MCP-native, multi-user.
</p>

<p align="center">
  <a href="https://github.com/opencraig/opencraig/releases"><img src="https://img.shields.io/badge/version-0.6.0-brightgreen?style=for-the-badge" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL_v3-blue.svg?style=for-the-badge" alt="License: AGPLv3"></a>
  <a href="https://github.com/opencraig/opencraig/stargazers"><img src="https://img.shields.io/github/stars/opencraig/opencraig?style=for-the-badge&logo=github" alt="Stars"></a>
  <a href="https://github.com/opencraig/opencraig/issues"><img src="https://img.shields.io/github/issues/opencraig/opencraig?style=for-the-badge" alt="Issues"></a>
  <a href="https://discord.gg/XJadJHvxdQ"><img src="https://img.shields.io/badge/Discord-join-7289da?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> ·
  <a href="#-who-its-for">Who it's for</a> ·
  <a href="#-how-it-works">How</a> ·
  <a href="#-editions">Editions</a> ·
  <a href="docs/">Docs</a> ·
  <a href="./README_CN.md">中文</a>
</p>

---

> ## 📦 v0.6.0 — first preview release
>
> OpenCraig is the **permission-aware knowledge / context layer for
> enterprise agent runtimes**. v0.6.0 is the first public preview
> of the OSS edition; the repo continues to be developed and
> maintained going forward, with v1.0 targeted for a stable cut once
> the API surface settles.
>
> A separate commercial product, **OpenCraig Enterprise (v3.0+)**,
> ships features specifically for enterprise deployments —
> lineage, audit, promote-to-library, auditable team workflows,
> managed sandbox execution, SSO / SCIM. See
> [Editions](#-editions) below for what's where; both editions
> are alive and developed in parallel.
>
> Brief: multi-user knowledge management + agentic search with
> structured citations + MCP-native retrieval surface. Folder-grant
> authz, BYOK LLM, fully self-hosted. Production-grade enough to
> run a team's research workflow today.

---

## ✨ Why OpenCraig

OpenCraig is positioned as **the knowledge / context layer for enterprise agent runtimes**, not as a chat product. The differentiation is at the seams between three things existing tools usually have only one of: **multi-user permission topology**, **MCP-native tool surface**, and **structured retrieval with bbox-precise citations**.

| You're using | What it gives you | What it doesn't |
|---|---|---|
| **Claude Code / Cursor / Cline alone** | Mature agent runtime + great built-in tools | Single-user; no team knowledge backend; no permission scoping when reading shared docs |
| **Claude Agent SDK alone** | Self-hostable agent loop (the same one Claude Code uses) | Same as above — runtime without a team-shared knowledge layer or sandbox infrastructure |
| **Notion AI / Glean / Mendable** | Polished search UX, SaaS-managed | Closed source; not MCP; agents can't plug in; corpus on someone else's servers |
| **AnythingLLM / RAGFlow / GraphRAG** | OSS self-host RAG | Single-user (or shallow multi-user); no permission-scoped retrieval; not exposed as MCP tools that respect per-user authz |
| **LangChain / LlamaIndex** | Building blocks | A library, not a knowledge backend. You'd need to build everything OpenCraig already shipped (folder authz, KG visibility, ingestion pipeline, MCP server) |
| **Hand-rolled embedding RAG** | "We have a Python team" | Skip 6 months of plumbing: path-as-authz, KG extraction with visibility-aware filtering, structured chunking, MCP wrappers, multi-user permission UI |

**The OpenCraig category is "permission-aware knowledge context for agents":**

- **Path-as-authz** — folder grants are the only authorization primitive; every retrieval call resolves the principal's accessible-folder set BEFORE running the search (prefilter on vector / BM25 / tree-nav) and AFTER materialising KG entities (postfilter — entities whose source-doc set isn't fully covered by the user's grants are dropped, no description redaction)
- **MCP-native** — `/api/v1/mcp` exposes search / KG / library tools to any compatible agent runtime (Claude Agent SDK ships in the box; Cursor / Cline / others connect over the same protocol). Per-connection auth scopes the ToolContext to the right user.
- **Structured retrieval, not text blob** — chunks know their bbox, tree position, source page; KG entities know their source chunks; citations open the PDF at the exact rectangle.

---

## 🎯 Who it's for

OpenCraig is built for **knowledge-dense small teams that can't or won't put their corpus on a SaaS**:

- **Patent agents / IP boutiques** — long technical specs, prior art, citation accuracy is a legal asset
- **Small law firms / litigation teams** — privileged documents, case law, internal precedent
- **Biotech / pharma R&D departments** — HIPAA-adjacent compliance, literature, internal protocols
- **Independent analysts / financial research desks** — IPO prospectuses, regulatory filings, alpha hides in cross-document detail
- **University labs / research centers** — student researcher onboarding, paper libraries, internal datasets
- **Independent professionals running an LLC or 个体工作室** — when "your data on your laptop / your VPS" is the whole point

It's **not** built for: customer-support chatbots, public knowledge bases, marketing-content generation, casual ChatPDF use.

---

## 🧠 How it works

```mermaid
flowchart LR
    Q([❓ Question]) --> AGENT[🤖 Agent Loop<br/>any MCP-compatible runtime]
    AGENT -->|MCP| TOOLS{Tool selection}
    TOOLS --> LF[📁 list_folders<br/>browse tree]
    TOOLS --> LD[📃 list_docs<br/>list in folder]
    TOOLS --> VEC[📐 search_vector<br/>semantic recall]
    TOOLS --> KG[🕸️ graph_explore<br/>entities + relations]
    TOOLS --> TREE[🌳 read_tree<br/>section nav]
    TOOLS --> CHUNK[📄 read_chunk<br/>full passage]
    TOOLS --> RR[🔁 rerank<br/>cross-encoder]
    LF --> AGENT
    LD --> AGENT
    VEC --> AGENT
    KG --> AGENT
    TREE --> AGENT
    CHUNK --> AGENT
    RR --> AGENT
    AGENT --> ANS[💬 Answer<br/>+ pixel-precise citations]
    ANS --> A([📝 Page + bbox])

    style Q fill:#0e1116,stroke:#3291ff,color:#fff
    style A fill:#0e1116,stroke:#10b981,color:#fff
    style AGENT fill:#1f1f1f,stroke:#fbbf24,color:#fbbf24
```

**Agentic retrieval, not a fixed pipeline.** The agent (Claude
Agent SDK, in-process or in-container) decides per-question which tools to call
and in which order. For "what does section 3.2 say" it might just
read_tree + read_chunk; for "how does X relate to Y across the
corpus" it leads with graph_explore; for "what do we have on Q3
sales?" it browses with list_folders + list_docs before searching.
Multi-hop questions chain several tools across iterations.

**Search ⇄ browse ⇄ read.** Three orthogonal access patterns the
agent picks between:

* **Search** — `search_vector` / `graph_explore` — best when the
  agent has a query and wants relevant passages
* **Browse** — `list_folders` / `list_docs` — best when the user
  asks open-ended ("what do we have on X?") and the agent should
  walk the corpus tree first
* **Read** — `read_chunk` / `read_tree` — pull the full text of a
  specific passage / outline a specific document

Each tool enforces multi-user authz at the dispatch boundary — the
search hits the agent sees are already scoped to the user's
accessible folders. KG visibility is stricter: entities whose source
docs aren't fully covered by the user's grants are dropped (no
description redaction fallback because LLM context can't render a
visibility banner).

A retrieval trace UI shows every tool call live — what the agent
asked, what came back, how long it took. Click a citation `[c_N]`
in the answer → opens the source PDF at the exact bbox.

---

## 📸 What you get

> **Screenshots:** see [`docs/SCREENSHOTS.md`](docs/SCREENSHOTS.md) for the current set.

| | |
|---|---|
| **Workspace** | File-manager UX with drag-drop, recycle bin, folder Members invite-and-share. Each user has a personal Space at `/users/<username>` displayed as their `/`; admins also see the global tree. Live ingestion status per file (parsing → embedding → building graph). |
| **Chat** | Streaming answers with `[c_N]` citations. Click any citation → opens the source PDF at the exact bbox. Citations carry across follow-up turns. |
| **Document Detail** | 3-pane: tree navigator + PDF viewer + chunks/KG-mini. Hover a chunk → highlights its source region. |
| **Knowledge Graph** | Sigma-rendered force-directed view. Filter by document, search entities, click an edge to see the supporting chunk. |
| **Activity log** (admin) | Every folder / document / share / role mutation, with the actor's identity stamped in. Filter by user, action category, time range. |
| **Setup wizard** | One-key model-platform presets (SiliconFlow / OpenAI / DeepSeek / Anthropic / Ollama). New deploy → web UI → pick a tile → done. No yaml editing. |

---

## 🚀 Quick Start

The **fastest path** is docker compose:

```bash
git clone https://github.com/opencraig/opencraig.git
cd opencraig
cp .env.example .env  &&  $EDITOR .env       # set passwords (LLM key optional — wizard collects it)
docker compose up -d                          # postgres + neo4j + opencraig
```

Then open <http://localhost:8000>:

1. **Pick a model platform** in the wizard (SiliconFlow recommended for China / cost-sensitive deploys; Ollama for fully air-gapped).
2. **Register** the first account — it's auto-promoted to admin.
3. **Drop in a PDF** and ask a question. The first ingest takes a minute; afterwards retrieval is sub-second.

### Bare-metal install

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..

python scripts/setup.py                              # CLI wizard alternative to the web one
python main.py                                        # http://localhost:8000
```

The CLI wizard is bilingual (EN/中文), checkpointed (Ctrl+C resumable), and **only installs the backend deps your config picks** — don't memorize pip names per database.

> **Tip:** Enable [MinerU](https://github.com/opendatalab/MinerU) in the Settings panel for a step-change in PDF parsing quality on tables, formulas, and complex layouts.

---

## 🏗️ Built on

```mermaid
flowchart TB
    subgraph "Frontend (Vue 3)"
        UI[Workspace · Chat · KG · DocDetail · Settings]
    end
    subgraph "Backend (FastAPI · Python 3.13)"
        API[REST + SSE]
        AGENT[Claude Agent SDK<br/>in-process or in-container]
        MCP[MCP server<br/>domain tool surface]
        LLMP[LLM proxy<br/>OpenAI + Anthropic]
        ING[Ingestion Pipeline<br/>parse · tree · chunk · embed · KG]
        TOOLS[Tools<br/>search_vector · graph_explore<br/>list_folders · list_docs<br/>read_chunk · read_tree · rerank<br/>import_from_library]
        AUTH[Auth + Spaces<br/>folder grants · audit log · per-user space]
    end
    subgraph "Pluggable Backends"
        REL[(SQLite · PostgreSQL)]
        VEC[(ChromaDB · pgvector · Qdrant · Milvus · Weaviate)]
        BLOB[(Local · S3 · OSS)]
        KGS[(NetworkX · Neo4j)]
        PARSE[PyMuPDF · MinerU · MinerU-VLM]
        LLM[Any LiteLLM provider]
    end
    UI --> API
    API --> AUTH
    AUTH --> AGENT
    AUTH --> ING
    AGENT --> MCP
    AGENT --> LLMP
    MCP --> TOOLS
    TOOLS --> AUTH
    LLMP --> LLM
    ING -.-> REL & VEC & BLOB & KGS
    ING --> PARSE
    ING --> LLM
```

The agent runtime is the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) —
the same loop that powers Claude Code. It runs
in-process for plain Q&A turns (built-in filesystem tools disabled
so the loop only reaches our MCP-exposed retrieval surface) and
inside the per-user sandbox container for turns that need bash,
edit, grep, and friends. The agent loop is intentionally not where
our differentiation lives — that sits in the **tools and the
infrastructure around them** (multi-user authz, sandbox containers,
structured retrieval, KG, bbox citations).

Every component is a config swap — pick your stack at the wizard, change later by editing `docker/config.yaml`.

---

## ⚙️ Highlights

### 🎯 Retrieval and citations

- Each `[c_N]` carries `doc_id + page + bbox`; clicking opens the PDF at the highlighted rectangle.
- Tree-aware chunking respects document structure: chapters, sections, tables, and figures stay intact.
- Knowledge graph with embeddings on entity names (cross-lingual fuzzy match) and relation descriptions (relation-semantic search).
- The agent loop picks retrieval tools per question; every call is visible in the trace UI.
- Native ingest for PDF, DOCX, PPTX, HTML, Markdown, TXT, common image formats, and spreadsheets (one-block-per-page for tabular).

### 🤖 Sandboxed execution

- Per-user Linux container with the Python data stack, LibreOffice, pandoc, and ~25 CLI tools (`jq`, `ripgrep`, `duckdb`, `xsv`, …). The agent's `bash`, `edit`, and `grep` run inside the container; the user's machine is not touched.
- Each chat anchors to a folder under `<user_workdirs_root>/<user_id>/`. The agent `chdir`s there before running, and any files it writes land there.
- `/api/v1/mcp` exposes domain tools (search, KG, read_chunk, library, workdir) to any MCP-compatible agent runtime.
- LLM proxy supports both OpenAI and Anthropic wire formats; LiteLLM routes to Anthropic, OpenAI, DeepSeek, SiliconFlow, Bedrock, Vertex, Ollama, or any other configured provider.
- First-boot wizard presets cover SiliconFlow, OpenAI, DeepSeek, Anthropic, and Ollama. One key wires chat, embedding, and reranker at once.

### 👥 Multi-user authorization

- Folder grants are the only authorization primitive. Every retrieval call resolves the principal's accessible-folder set before search.
- Right-click any folder to invite teammates, set view or edit role, and see members inherited from parent folders.
- Activity log at `/settings/audit` records every folder, document, share, and role change with actor, filter, and pagination.
- Soft-delete with 30-day retention; restore rebuilds missing parent folders automatically.
- Zero telemetry, analytics, or error reporting back to OpenCraig itself — see [`PRIVACY.md`](PRIVACY.md).

---

## 📊 Benchmark

[UltraDomain](https://github.com/HKUDS/LightRAG) methodology · LLM-as-judge pairwise · win % shown as **OpenCraig / LightRAG**:

| Domain | Comprehensiveness | Diversity | Empowerment | **Overall** |
|---|:---:|:---:|:---:|:---:|
| Agriculture | **58.6** / 41.4 | 47.1 / **52.9** | **52.9** / 47.1 | **56.4** / 43.6 |
| Computer Science | **55.6** / 44.4 | 48.4 / **51.6** | **54.0** / 46.0 | **54.8** / 45.2 |
| Legal | **57.0** / 43.0 | 46.5 / **53.5** | **53.5** / 46.5 | **55.6** / 44.4 |
| Mix | **56.3** / 43.7 | 47.8 / **52.2** | **54.3** / 45.7 | **55.1** / 44.9 |

<sub>Judge: qwen3-max · Reproduce: [`scripts/compare_bench.py`](scripts/compare_bench.py) · OpenCraig additionally provides verifiable `[c_N]` citations the benchmark doesn't score for.</sub>

🚧 _More benchmarks (vs RAGFlow, GraphRAG, vanilla RAG, on more domains and metrics) in progress._

---

## 🗂️ Project Layout

```
OpenCraig/
├── api/                 FastAPI routes, auth middleware, setup wizard
│   ├── auth/             AuthMiddleware, PathRemap, FolderShareService
│   ├── routes/           One file per resource
│   └── setup_presets.py  SiliconFlow / OpenAI / Ollama / ... presets
├── answering/           Answer + citation pipeline
├── ingestion/           Parse → tree → chunk → embed → KG
├── parser/              PDF parsing, chunking, tree building
├── retrieval/           BM25 / vector / KG / tree-nav / RRF merge
├── embedder/            Embedding backends (LiteLLM, sentence-transformers)
├── graph/               KG stores (NetworkX, Neo4j)
├── persistence/         Relational + vector + blob + folder service + share service
├── config/              Pydantic config models, YAML loader (with overlay merge)
├── web/src/             Vue 3 frontend (Workspace, Chat, KG, Settings, Setup wizard)
├── docs/operations/     Backup / restore / upgrading runbooks
├── docs/roadmaps/       In-flight feature design docs (per-user spaces, etc.)
└── scripts/             backup.sh, restore.sh, setup.py, batch_ingest.py
```

---

## 📚 Docs

- **[Getting Started](docs/getting-started.md)** — install, first ingest, first query
- **[Architecture](docs/architecture.md)** — full ingestion + retrieval + answering walkthroughs (with diagrams)
- **[Configuration](docs/configuration.md)** — every YAML option with defaults
- **[API Reference](docs/api-reference.md)** — REST + SSE streaming
- **[Deployment](docs/deployment.md)** — Docker, production checklist, Nginx
- **[Backup & Restore](docs/operations/backup.md)** — RTO/RPO, schedule, cross-version recovery
- **[Upgrading](docs/operations/upgrading.md)** — alembic flow, pinning, rollback
- **[Auth](docs/auth.md)** — multi-user, folder grants, OAuth-proxy mode
- **[Privacy](PRIVACY.md)** — what data leaves your network (spoiler: only LLM API calls you configure)
- **[Roadmaps](docs/roadmaps/)** — design docs for in-flight features

---

## 🗺️ What's in v0.6.0 (and what's not)

### Shipped in OSS (this repo, frozen)

- [x] **Pixel-precise citations** — `doc_id + page + bbox` on every claim
- [x] **Structured retrieval tools** — vector / KG / tree-nav / read_chunk / rerank
- [x] **Agentic retrieval** — Claude Agent SDK in-process and in-container; multi-step tool selection per question
- [x] **MCP tool surface** — `/api/v1/mcp` exposes domain tools to any MCP client
- [x] **OpenAI-compatible LLM proxy** — `/api/v1/llm/v1/chat/completions` via litellm router
- [x] **Web search tool** — Tavily / Brave / Bing with prompt-injection defenses
- [x] **Multi-user, folder grants, per-user Spaces** — path-as-authz, no multi-tenant
- [x] **Folder Members UI** — invite teammates, set view/edit role
- [x] **Audit log** — admin-visible activity feed of every mutation
- [x] **First-boot setup wizard** — one-key model platform presets
- [x] **One-shot docker compose** — postgres + neo4j + opencraig with healthchecks
- [x] **Backup + restore scripts** with cross-version recovery notes
- [x] **AGPL v3 + commercial dual license**

### Reserved for OpenCraig Enterprise (v3.0+, see [Editions](#-editions))

These were intentionally **not** shipped in OSS — the differentiation
lives here, and they need a commercial product behind them:

- **Lineage backbone** — every artifact tracks its source docs +
  agent run + actor, end-to-end queryable
- **Promote-to-Library** — agent outputs ⇄ Library: knowledge
  compounds across runs
- **Audit UI** — "what did the agent do with this folder?" reverse
  query; "this doc influenced which artifacts?"
- **Per-folder agent runtime + lineage** — Enterprise pins each
  user's chats to a separate sandbox per folder, with lineage
  attribution (which run touched which files), runtime isolation
  hardening, and skills-per-folder. OSS ships the baseline
  folder-as-cwd model (one per-user sandbox; agent chdirs into
  the chat's folder, runs bash / edit / grep there).
- **Skills as auditable team workflows** — codified, versioned,
  team-shared agent procedures with full provenance
- **SSO / SCIM** — Okta / Azure AD provisioning, group-based authz
- **Hardened sandbox** — non-root, no-net default, capability drop
- **Managed hosting + SLA** for teams that don't want to ops it

### What OSS keeps getting

The OSS edition continues to receive maintenance + non-Enterprise
improvements: bug fixes, parser / model / vector-store backend
updates, security patches, performance work, ergonomics, docs.
The line we hold is the Enterprise feature list above —
those don't migrate back into OSS, but everything else stays
fair game.

---

## 🎁 Editions

| | **OpenCraig OSS** (this repo) | **OpenCraig Enterprise** |
|---|---|---|
| License | AGPLv3 | Commercial, contact for terms |
| Source | Open, this repo | Closed |
| Self-host | ✅ Free, indefinitely | ✅ Available |
| Managed hosting | ❌ | ✅ |
| Multi-user + folder authz | ✅ | ✅ |
| Agentic retrieval | ✅ | ✅ + sandboxed code execution |
| Lineage / audit / promote-to-library | ❌ | ✅ |
| Skills (team workflows) | ❌ | ✅ |
| SSO / SCIM | ❌ | ✅ |
| Support | GitHub issues, community | SLA, dedicated support |
| Development | Active OSS development | Active commercial development |

Both editions are alive and developed in parallel. The Enterprise
features in the table above don't migrate back into OSS, but
everything else (parsers, backends, models, performance,
ergonomics, security) stays under active OSS work.

Inquiries about Enterprise: [opencraig.com](https://opencraig.com).

---

## 📈 Star history

<a href="https://star-history.com/#opencraig/opencraig&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=opencraig/opencraig&type=Date&theme=dark" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=opencraig/opencraig&type=Date" />
  </picture>
</a>

---

## 🤝 Contributing

Bug reports, features, docs improvements, translations all welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Stop by [Discord](https://discord.gg/XJadJHvxdQ) for design discussions.

Contributions are accepted under AGPLv3, the same license the project ships under. The OSS core stays AGPLv3 — that doesn't change.

Note that Enterprise-edition features (lineage, audit, promote-to-library, sandbox code execution, skills, SSO/SCIM) live in a separate codebase and don't accept external contributions.

## 🔗 Related work

- [LightRAG](https://github.com/HKUDS/LightRAG) — graph-based RAG with dual-level retrieval
- [GraphRAG](https://github.com/microsoft/graphrag) — Microsoft's graph-powered RAG with community summaries
- [PageIndex](https://github.com/VectifyAI/PageIndex) — reasoning-based vectorless retrieval
- [MinerU](https://github.com/opendatalab/MinerU) — document parsing engine OpenCraig uses for rich layouts
- [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm) — closest commercial-OSS peer in the self-host RAG space

## License

OpenCraig is released under the [GNU Affero General Public License v3.0](LICENSE)
(AGPLv3) for community use and self-hosted deployment.

**Commercial licensing** is available for organizations that need to deploy
OpenCraig without AGPLv3 obligations — for example, embedding into a
proprietary product, or running a closed-source managed service. Contact
[info@deeplethe.com](mailto:info@deeplethe.com) for terms.
