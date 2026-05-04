# Roadmap: Retrieval Evolution

**Status:** design
**Last updated:** 2026-05-04

This document captures the design and sequencing for the next wave of retrieval-layer features: **file search**, **retrieval MCP**, **agentic search**, and **deep research**. It is self-contained — readable without the prior design discussion — so context-window compression can't lose the key calls.

---

## TL;DR

The current retrieval pipeline (BM25 + vector + KG + tree-nav, fused via RRF, reranked, with pixel-precise citations) is solid for one-shot question answering. The next four features compose on top of that foundation, each unlocking a different kind of usage:

1. **File search** (foundation) — find documents by *name/path/type*, not by content. Solves "where's that file I uploaded last week" without polluting the content index.
2. **Retrieval MCP** (interface) — expose the retrieval + answering pipeline as MCP tools so external agents (Claude Desktop, Claude Code, custom workflows) can use ForgeRAG as their RAG backend.
3. **Agentic search** (orchestration) — multi-step retrieval where an LLM drives follow-up queries based on intermediate results. Replaces "one-shot retrieval" with an agentic loop bounded by a budget.
4. **Deep research** (composition) — long-horizon research mode. Builds an outline, runs agentic search per section in parallel, synthesises a structured citation-grounded report.

Each layer reuses everything below it. **File search** is standalone. **MCP** wraps existing retrieval (and gets richer as later layers ship). **Agentic search** drives the existing pipeline iteratively. **Deep research** orchestrates many agentic-search runs.

Sequenced delivery: ship in the order above. File search lands first as a quick foundation; MCP comes second to start collecting external integration feedback while the heavier features are built; agentic search third; deep research last.

---

## Why these four, in this order

The current pipeline answers a question in a single shot: parse query → run 4 paths in parallel → fuse → rerank → answer. That model breaks down in three places, in increasing severity:

* **Filename queries** — "find that legal report" doesn't match content; current BM25 indexes chunk text only, not filenames or folder paths.
* **Multi-hop questions** — "Compare LangChain and LlamaIndex's approaches to RAG" needs at least two targeted searches plus a synthesis pass; one shot doesn't cover it.
* **Reports / long-horizon work** — "Write me a survey of tariff impacts using my 50 uploaded papers" needs planning, parallel research per topic, then synthesis with citations.

File search solves the first directly. The other two need the system to *plan* — to decide what to search for next based on what it has so far. That's the agentic-search shape. Deep research is the same shape extended to multiple parallel research threads.

MCP doesn't fix any of those internally — it's about *exposure*. But shipping it early means external agents can use the system while the deeper features are under development; their feedback shapes the rest.

---

## Feature 1: File search

### What

A **lexical search over file metadata** — filename, folder path, mime type, upload time. Distinct from content search:

* Content search: "what does the contract say about termination" → searches chunk text via BM25 + vector
* File search: "where's the termination contract" → searches `Document.filename`, `Document.path`, `File.original_name`

### Why

Current state:

* `GET /api/v1/files` and `GET /api/v1/documents` both list, neither searches.
* The BM25 index is content-only; filename tokens leak into it via `section_path` prefixes only inconsistently.
* Workspace UI has folder navigation but no global "find file" search bar.

Users routinely have hundreds of uploads. Hunting by clicking through folders doesn't scale; expecting them to remember a content fragment to find a file by content is a worse UX than a literal Cmd-F.

### Architecture

**Reuse `InMemoryBM25Index`, second instance, fed filenames instead of chunks.**

The existing content BM25 indexes `(chunk_id, doc_id, chunk.content + section_path)`. The filename BM25 indexes `(doc_id, doc_id, "filename + path")` — same class, same persistence shape, different data and a different cache file. No new dependencies, no SQL-dialect splits, no migrations.

* **Index keying**: per-document, not per-chunk. Each doc contributes one entry whose text is `f"{filename} {path}"` — tokenized normally so `invoice-2024-Q3.pdf` becomes `["invoice", "2024", "Q3", "pdf"]`. Folder segments tokenize the same way, so `/legal/2024/contracts/` adds `legal`, `2024`, `contracts` to the entry. Format also added (`pdf`, `xlsx`, ...) so type-filter queries can be lexical too.
* **Build at startup**: parallel to `build_bm25_index()`, add `build_filename_bm25_index()`. Both load from disk if cached; both rebuild from scratch if cache is stale.
* **Incremental updates**: when a doc lands in `_on_ingest_complete`, update both indices alongside each other. Same lock pattern (`_init_lock`).
* **Path / type filter**: applied as a post-filter on the BM25 candidate set rather than baked into the index. Filter-then-score is the same shape the content path uses for trashed-doc filtering.

**Result shape**: `list[FileSearchHit]` with `doc_id, filename, path, format, score, matched_field`.

**Frontend**: Workspace gets a top-of-page search bar. Cmd/Ctrl+K opens a global file palette. Results render the matched portion bolded.

### Rejected alternatives

* **Add filenames to the EXISTING content BM25** — pollutes ranking. A search for "Apple" would surface every PDF named `apple-quarterly.pdf` even when the content is irrelevant; conversely, a content-search query that incidentally hits a filename gets noise. Keep the indices separate, share the class.
* **Postgres `tsvector` + GIN** — premature dialect split. The Python BM25 already handles the production-scale content index; filenames are 1-2 orders of magnitude smaller and trivially fit in the same backend. Adding a dialect-specific path here means SQLite users get a different code path with different bug surface.
* **SQLite trigram via `rapidfuzz`** — fixes "no Postgres dialect", introduces "no relevance ranking, no idf weighting" and a new dependency. Same critique.
* **`SQL ILIKE`** — no relevance ranking, no fuzzy match, scales linearly. Bad UX even at small N.
* **Vector embedding for filename matching** — embeddings are bad at literal-string queries (`invoice-2024-Q3-final.pdf` has no useful semantics). Lexical match is correct here.

### Implementation

* New module: `retrieval/file_search.py` with `FileSearcher` (build + search). Internally just an `InMemoryBM25Index` instance plus path/type post-filter logic.
* `api/state.py`: add `_filename_bm25` attribute and an incremental update path mirroring `_update_bm25_for_doc`.
* New route: `POST /api/v1/files/search` (POST not GET — body carries filter dicts, easier telemetry).
* New schema: `FileSearchHit` in `api/schemas.py`.
* Telemetry: one OTel span per search.

Estimated size: ~150 LOC backend (the wrapper around the existing BM25 class is the bulk) + ~150 LOC frontend.

---

## Feature 2: Retrieval MCP

### What

An **MCP server** that exposes ForgeRAG's retrieval and answering layers as tools callable by external agents (Claude Desktop, Claude Code, custom MCP clients).

Tools exposed:

| Tool | What it returns | Wraps |
|---|---|---|
| `search_files` | ranked file metadata | `FileSearcher` (Feature 1) |
| `search_chunks` | ranked chunks with citations | `RetrievalPipeline` |
| `query` | streamed answer + citations | `AnsweringPipeline` |
| `get_document` | metadata + tree summary | `Store.get_document` |
| `read_chunk` | full chunk text + neighbors | `Store.get_chunk` + expansion |
| `list_folders` | folder tree | `FolderService` |

### Why

The retrieval pipeline is already accessible via `POST /query` for our own frontend, but external agents speak MCP. Bridging this:

* Lets a Claude Desktop user attach their ForgeRAG instance and ask questions over their docs without leaving the chat.
* Lets Claude Code use ForgeRAG as a code-doc search backend.
* Lets users compose ForgeRAG into multi-tool agents (e.g. one MCP for code, one for docs, one for the web).

It's also a natural delivery vehicle for the later features: once `agentic_research` is a tool, every MCP-enabled agent gets it for free.

### Architecture

```
┌───────────────────────────────────┐
│ External agent (Claude Desktop)   │
└───────────────┬───────────────────┘
                │ stdio | SSE
                ▼
┌───────────────────────────────────┐
│  forgerag.mcp_server              │
│   ┌─────────────────────────┐     │
│   │  Tool dispatcher        │     │
│   │  (mcp.ServerSession)    │     │
│   └────┬────────────────────┘     │
│        │                          │
│        ▼                          │
│   AppState (shared with FastAPI)  │
└───────────────────────────────────┘
                │
                ▼
   RetrievalPipeline · AnsweringPipeline · Store · FileSearcher
```

* Same process as the FastAPI app — `AppState` is shared, so the MCP server reuses the in-memory BM25 index, embedder, and graph store. No extra startup cost; no risk of split-brain caches.
* Two transports:
  - **stdio**: primary use case. `python -m forgerag.mcp_server` is launched by the agent host; communication via standard streams.
  - **HTTP/SSE**: secondary, for remote agents. Mounted under `/mcp/sse` on the existing FastAPI app, gated by the same SK-token auth.

**Auth**:
* stdio is local-process trust — no auth, since the host already runs as the user.
* SSE reuses existing `Authorization: Bearer <sk-token>` middleware.

**Path scoping**: every tool accepts an optional `path_prefix` — the agent can confine searches to `/personal` or `/work/legal` per-call. Reuses `PathScopeResolver`.

### Rejected alternatives

* **HTTP-only MCP** — Claude Desktop / Code prefer stdio for local installs. Forcing HTTP means users have to expose ForgeRAG on a port and pass a token; way worse onboarding.
* **Separate MCP process** — splits the BM25 cache, doubles memory, complicates startup. Same-process is much simpler.
* **Translate the existing REST API to MCP via auto-generation** — REST shapes (path params, query strings) don't map cleanly to MCP tool schemas. Hand-curated tools are fewer and clearer; users see only the verbs that make sense for an agent, not e.g. `DELETE /api/v1/folders/{id}`.

### Implementation

* New package: `forgerag/mcp_server/` with:
  - `tools.py` — tool definitions (one function per MCP tool, using the official `mcp` Python SDK).
  - `server.py` — stdio entry point.
  - `sse.py` — FastAPI-mountable router for HTTP/SSE.
* New config section: `mcp.enabled`, `mcp.transport: stdio | sse | both`, `mcp.tools_allowed: list[str]` (default all). Disabled by default.
* New entrypoint: `python -m forgerag.mcp_server`.
* Auth handler shared with `api/auth/`.

Estimated size: ~600 LOC for the server + tool wrappers + tests.

### Open questions

* **Streaming**: should `query` stream tokens via MCP's incremental result mechanism? The MCP spec supports it, the SDK is still maturing. Defer to v2 if SDK ergonomics aren't ready.
* **Cost guardrails**: an agent could spam `query` and burn tokens. Add a rate limit per session (configurable, default 100 calls / hour).

---

## Feature 3: Agentic search

### What

A **multi-step retrieval pipeline** where an LLM iteratively decides what to search for next, given what it has so far. Replaces one-shot retrieval with an agentic loop bounded by a budget.

Loop:

```
1. LLM reads the user query, proposes 1-3 initial sub-queries.
2. Run RetrievalPipeline on each (parallel).
3. LLM reads the results, decides:
     a. "I have enough" → exit with collected chunks
     b. "I need more on X" → propose new queries
     c. "I need to refine query Y" → drop low-quality hits, re-search
4. If iter < max_iter and budget remains: goto 2.
5. Synthesize final answer + citations from collected chunks.
```

### Why

One-shot retrieval is fine for "what year did Apple release the iPhone 4" but breaks down on:

* **Comparison**: "Compare LangChain and LlamaIndex on chunking strategy" — needs two parallel retrievals plus a comparison synthesis.
* **Multi-hop**: "Who is the CEO of Apple's largest supplier?" — needs first to find the supplier, then to retrieve facts about that entity.
* **Disambiguation**: "What does the contract say about termination?" — finds termination clauses, but the right answer requires knowing which contract; if multiple are matched, follow-up to disambiguate.

Each of these is a multi-step problem. The query-understanding layer in the current pipeline detects intent (`comparison`, `summary`, ...) but doesn't act on it — it just routes paths and expands queries. Agentic search is the layer that uses intent to drive iteration.

### Architecture

Two modes; users pick per-call:

**Tool-call mode** (preferred for most cases):

* Standard agentic loop with the LLM-generated function calls.
* Tools: `search(query, top_k=10)`, `read_chunk(chunk_id)`, `done(summary)`.
* Works with any tool-using model (GPT-4, Claude 3+, Gemini 2+).
* Trace recorded automatically via existing OTel.

**Workflow mode** (cheap fixed-shape):

* Predefined steps for known intents:
  - `comparison`: decompose into entities → retrieve each → cross-search "X and Y" → synthesize.
  - `multi-hop`: extract subject → retrieve → extract object → retrieve → synthesize.
  - `summary`: retrieve broad → cluster → summarize per cluster.
* Cheaper (fewer LLM calls), less flexible, deterministic.
* Falls back to tool-call mode if intent not in {comparison, multi-hop, summary}.

**Budget**:

* `max_iterations` (default 5) — hard cap on the loop.
* `max_tokens` (default 8000) — total LLM tokens across the loop.
* `time_budget_s` (default 30) — wallclock cap.

When a budget binds, the loop synthesises with what it has. The trace records which budget triggered termination.

**Trace and observability**: every iteration emits an OTel span with `iteration, query_proposed, hits_count, decision`. Surfaced in the existing trace UI.

### Composition with existing pipeline

The agentic loop calls the existing `RetrievalPipeline` once per iteration. No changes to the inner pipeline. The Q-understanding step from the existing pipeline is *replaced* by the agentic loop's planning step — there's no point running the standalone QU on each iteration since the agent itself is doing query rewriting.

```
                ┌──────────────────────────────┐
                │ AgenticSearchPipeline        │
                │                              │
   Question ──▶ │  ┌──────────────────────┐    │
                │  │  Plan & Iterate      │    │
                │  │   (LLM tool-call)    │    │
                │  └─────────┬────────────┘    │
                │            │ for each call:  │
                │            ▼                 │
                │     RetrievalPipeline ◄──────┼── existing
                │     (current 4-path)         │
                │            │                 │
                │            ▼                 │
                │  ┌──────────────────────┐    │
                │  │  Synthesise          │    │
                │  │   (uses Generator)   │    │
                │  └─────────┬────────────┘    │
                └────────────┼─────────────────┘
                             ▼
                      Answer + citations
```

### Rejected alternatives

* **ReAct with raw text outputs** — fragile parsing, easy for the LLM to drift. Tool-calls are a strict-schema interface that recent models handle reliably.
* **Hardcoded decomposition for every query** — brittle. The current QU layer's intent detection is enough for the workflow-mode cases; the rest go to tool-call mode.
* **Multi-agent orchestration** — overkill for a single-user RAG. One LLM in a loop is plenty.

### Implementation

* New package: `agentic/` with:
  - `pipeline.py` — `AgenticSearchPipeline` orchestrator.
  - `tools.py` — search/read tool implementations.
  - `workflows/` — fixed-shape workflows for comparison/multi-hop/summary.
  - `trace.py` — span emission.
* New config: `agentic.enabled`, `agentic.default_mode`, `agentic.budget_*`, `agentic.model`.
* New API: `POST /api/v1/agent/search { query, mode?, budget? }`.
* Streaming: each iteration emits an SSE event so the UI can show live progress.
* Reuses: `RetrievalPipeline`, `Generator` (for tool-calls and synthesis), existing telemetry, MCP exposure (via Feature 2).

Estimated size: ~1200 LOC backend + ~400 LOC frontend (live progress UI).

### Open questions

* **Caching across iterations**: if iteration 3 proposes a query that iteration 1 already ran, should we hit cache or re-run? Cache for the obvious win; surface in trace as `cached: true`.
* **Citation stability**: the same chunk retrieved across multiple iterations should get a stable `c_N` ID, not a fresh number each time. Implement a session-scoped citation registry.
* **Cost ceiling per session**: do we expose a "max $0.10 per query" knob, or punt to the budget knobs?

---

## Feature 4: Deep research

### What

A **long-horizon research mode**. User gives a topic (and optionally a scope like a folder or document set); ForgeRAG produces a structured, multi-section, citation-grounded report.

Phases:

```
1. Plan      LLM proposes outline (sections + sub-questions per section).
             User can edit before approving.
2. Research  Each section runs AgenticSearch in parallel.
             (Up to N concurrent research threads.)
3. Draft     LLM writes each section using its findings, citing chunks.
4. Synthesis LLM polishes transitions, dedups citations across sections,
             writes intro / conclusion.
5. Review    Final document with bibliography, exported as MD or PDF.
```

### Why

The above three features all answer a question. Deep research *writes a document*. Different artefact, different requirements:

* Output structure is a Markdown document, not a chat reply.
* Cost budget is order-of-magnitudes higher (minutes/hours vs seconds).
* Citations need to be deduped across sections (same source cited from multiple sections gets a single bibliography entry).
* User wants to interrupt + edit the outline before letting it loose.

Use cases:
* Lit review: "Survey transformer architectures from these 50 papers."
* Compliance memo: "Summarise our termination rights under each contract in /legal."
* Investment thesis: "Build a thesis on X using uploaded earnings transcripts."

### Architecture

```
                    ┌─────────────────────────┐
   Topic + scope ──▶│  Plan phase             │
                    │   (LLM → outline JSON)  │
                    └────┬────────────────────┘
                         ▼
                    User reviews/edits outline (optional)
                         │
                         ▼
                    ┌─────────────────────────────────┐
                    │  Research phase (parallel)       │
                    │   ┌──────┐ ┌──────┐ ┌──────┐     │
                    │   │ Sec1 │ │ Sec2 │ │ Sec3 │ ... │
                    │   │  AS  │ │  AS  │ │  AS  │     │
                    │   └──┬───┘ └──┬───┘ └──┬───┘     │
                    │      │        │        │         │
                    └──────┼────────┼────────┼─────────┘
                           ▼        ▼        ▼
                    ┌─────────────────────────────────┐
                    │  Draft phase (sequential)        │
                    │   LLM writes Sec1 → Sec2 → ...   │
                    │   given findings + outline ctx   │
                    └────┬────────────────────────────┘
                         ▼
                    ┌─────────────────────────┐
                    │  Synthesis phase        │
                    │   intro/conclusion +    │
                    │   citation dedup        │
                    └────┬────────────────────┘
                         ▼
                    Final document + bibliography
```

`AS` = AgenticSearchPipeline (Feature 3). Deep research is, fundamentally, a planner that orchestrates many AgenticSearch runs and stitches the output together.

### Storage

New tables:

* `research_sessions(id, user_id?, topic, outline_json, status, created_at, completed_at, cost_usd_estimate)` — top-level session state.
* `research_findings(session_id, section_idx, chunks_json, agentic_trace_id)` — per-section research results.
* `research_drafts(session_id, section_idx, content_md, citations_json)` — per-section drafts.
* `research_documents(session_id, final_md, bibliography_json)` — the final deliverable.

Outputs are stored as a `Document` in addition to the new tables, so the research output is itself ingestable by the normal pipeline (and queryable later).

### API

* `POST /api/v1/research/plan { topic, sources?, depth? }` — returns proposed outline; no research yet.
* `POST /api/v1/research/start { session_id, outline }` — kicks off the research phase. Returns immediately; status polled.
* `GET /api/v1/research/{id}` — current state + outline + partial drafts.
* `GET /api/v1/research/{id}/stream` — SSE for live progress (one event per phase transition + per-section completion).
* `GET /api/v1/research/{id}/document` — final rendered MD.
* `DELETE /api/v1/research/{id}` — cancel running session.

### UI

A new top-level "Research" tab with:

* **Topic input** + scope selector (folder picker, document multi-select).
* **Outline editor** — edit titles, reorder, delete sections, add custom sub-questions.
* **Live dashboard** during research — per-section progress bars, current sub-query, cost so far.
* **Document viewer** for the finished report — side-by-side with citations panel.

### Rejected alternatives

* **Single LLM call with all chunks** — works for tiny corpora (<100 chunks), fails on context. No structure either; just "summarize everything."
* **Map-reduce summarization** — what GraphRAG does for community summaries. Good for global digests, bad for *per-section research*. Doesn't follow an outline; doesn't produce a structured document.
* **Tree-structured rollup using existing DocTree** — that tree is per-document, not cross-document. Wrong granularity.
* **One AgenticSearch call with a 50-paragraph response** — exceeds output budgets; LLMs are bad at maintaining 50-paragraph coherence in one shot.

### Implementation

* New package: `research/` with:
  - `pipeline.py` — `DeepResearchPipeline` orchestrator.
  - `planner.py` — outline generation.
  - `drafter.py` — section drafter.
  - `synthesizer.py` — final synthesis.
* New persistence layer for the four new tables.
* New API routes under `/api/v1/research/`.
* New frontend route + components.
* Reuses: `AgenticSearchPipeline` (heavy), `Generator`, `RetrievalPipeline`, citation builder.

Estimated size: ~2000 LOC backend + ~1500 LOC frontend.

### Open questions

* **Caching across sessions**: should two sessions on similar topics share cached chunk-retrieval results? Probably yes, with a topic-similarity check; defer to v2.
* **Outline interactivity**: how heavy-weight should the outline editor be? Start with title-only edits + add/remove/reorder; add sub-question editing in v2.
* **Cost transparency**: show running cost in real-time? Yes — it's the user's bill. Use the per-call usage from `Generator`.
* **Cancellation semantics**: cancellation should stop pending agentic-search calls but preserve already-computed sections so the user can salvage partial work.

---

## Cross-cutting concerns

### Tracing

Every new feature emits OTel spans with consistent attributes (`feature, query, hits_count, cost_estimate`). Existing trace UI extends to show the new span types. Hierarchies:

```
forgerag.research
 └─ forgerag.research.plan
 └─ forgerag.research.section[0]
     └─ forgerag.agentic
         ├─ forgerag.agentic.iteration[0]
         │   └─ forgerag.retrieve  (existing)
         ├─ forgerag.agentic.iteration[1]
         │   └─ forgerag.retrieve
         └─ forgerag.agentic.synthesize
 └─ forgerag.research.draft
 └─ forgerag.research.synthesize
```

### Caching

Three layers:

1. **Embedding cache** — already exists, untouched.
2. **Retrieval cache** — currently per-conversation in answering pipeline. Extend to a session-scoped cache for agentic search and research.
3. **LLM call cache** — the `forgerag.llm_cache` module already caches LLM completions by hash; reuse it in agentic and research.

### Path scoping & permissions

Every new feature MUST honour `path_prefix` filters. The `PathScopeResolver` already supports this; new code paths just need to plumb it through. Add a regression test per feature that confirms a `path_prefix=/public` query never returns chunks under `/private`.

### Cost control

Three knobs at every level:

* **Per-call**: `budget` parameter (max iterations, max tokens, time).
* **Per-user/session**: rate-limit (default 100 agentic calls / hour).
* **Per-deploy**: config caps (`agentic.max_iterations_hard`, `research.max_concurrent_sections`).

Soft warnings vs hard caps: warnings log + surface in trace; hard caps reject with 429.

### Citation stability across iterations

Within a session (agentic search or research), the same chunk retrieved by multiple iterations gets a single `c_N` ID. Implement a `CitationRegistry(session_id)` that's reused across the loop.

---

## Sequencing & estimates

| # | Feature | Estimate (eng-weeks) | Depends on | Why this order |
|---|---|---|---|---|
| 1 | File search | 0.5 | — | Smallest. Reuses existing BM25 class, second instance fed filenames. Standalone. |
| 2 | Retrieval MCP | 1.5 | (1) for the file-search tool | Exposes existing surface; collects external feedback while heavier work proceeds. |
| 3 | Agentic search | 3 | (1), (2) | Core capability that deep research depends on. Ships independently as a power-user feature. |
| 4 | Deep research | 4 | (3) | Largest. Heavy frontend work + new persistence + the most user-visible output. |

Total: ~9 eng-weeks for all four. Each can ship independently with its own branch + merge to `dev`.

---

## What we're explicitly NOT doing

* **Multi-agent frameworks** (autogen, crewai). One LLM in a loop, tooled, is enough for this scale.
* **Distributed retrieval** across remote ForgeRAG instances. Single-deploy only for now.
* **Live ingestion during research** ("watch this folder, update the report"). Periodic re-runs are user-driven for now.
* **Speech / video output** of research results. Markdown + PDF only.
* **Custom DSL for outlines**. Plain JSON outline schema, generated by the LLM, edited by the user in a form UI.

---

## Future hooks (post-roadmap)

These features open natural extensions worth noting:

* **Cross-doc KG queries** — once agentic search exists, exposing "find paths between entities X and Y in the KG" as an agent tool is straightforward.
* **Live source tracking** — "watch sources for updates and re-run section 3 when they change" — natural extension once `research_sessions` exist.
* **Comparative research** — "compare my answer vs ForgeRAG's answer" — exposing this through MCP would let an agent fact-check humans.
* **Cited answer caching** — agentic search results are themselves citable; answers from research mode could be ingested as a new document for follow-up questions.
