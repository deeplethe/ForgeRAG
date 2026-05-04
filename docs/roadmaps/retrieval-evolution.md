# Roadmap: Retrieval Evolution

**Status:** design
**Last updated:** 2026-05-04

This document captures the design and sequencing for the next wave of retrieval-layer features: **file search**, **retrieval MCP**, **agentic search**, and **deep research**. It is self-contained — readable without the prior design discussion — so context-window compression can't lose the key calls.

---

## TL;DR

The current retrieval pipeline (BM25 + vector + KG + tree-nav, fused via RRF, reranked, with pixel-precise citations) is solid for one-shot question answering. The next four features compose on top of that foundation, each unlocking a different kind of usage:

1. **Unified `/search`** (foundation) — the retrieval primitive exposed standalone, no answer synthesis. Returns chunks by default; opt in to a file-level rollup view via `include=["files"]`. Filename signal feeds both views (small per-doc boost on chunks, RRF-fused per-file ranking on the files view) so a query for "Q3 financial report" surfaces both filename-matches and content-matches in the same call. `/query` becomes definitionally `/search + answering`.
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

## Feature 1: Unified search (`/search`)

### What

The retrieval primitive — exposed standalone, no answer synthesis. `/search` is the single endpoint for "find me things matching this query"; the existing `/query` becomes "`/search` then ask the LLM about the results."

```
POST /api/v1/search
{
  query: str,
  filter?: dict,
  path_prefix?: str,
  overrides?: QueryOverrides,    # reuse existing per-call knobs
  include?: ["chunks"]           # default. Add "files" for the file-level rollup view.
  limit?: { chunks?: 30, files?: 10 }
}

→ {
  chunks: [ ScoredChunkHit, ... ],          # always present
  files?: [ FileHit, ... ],                  # when include contains "files"
  stats: { ... }
}
```

Two views of the same retrieval, returned together when asked:

* **`chunks`** (always) — the primary retrieval output: ranked chunks with snippet, doc_id, page_no, bbox, score. Same shape callers already see on `/query` minus the LLM answer + citation IDs.
* **`files`** (opt-in via `include`) — file-level rollup: same query collapsed to one row per doc, snippet from the best content chunk, badge for which signal matched (`filename` / `content` / both). Workspace search-bar's natural fit.

A query for `"Q3 financial report"` returns:

* `chunks`: top scored chunks across the corpus, naturally including chunks from `Q3_financials_2024.pdf` boosted by the filename match
* `files` (if requested): three files ranked together — the filename-only match (`Q3_financials_2024.pdf`), the content-deep match (`board-deck.pdf` page 12), and the partial-overlap match (`annual-report.pdf` mentions "Q3 financial").

### Why

Two real needs that today's API doesn't cleanly serve:

1. **"Show me the retrieval results without answering"** — agents, debug UIs, and the future agentic / research layers all want chunks without paying for the LLM answer. Today they have to call `/query` and discard the answer.
2. **"Search for a file"** — the workspace UI has no search bar. Users with hundreds of uploads can't find files by name, and they can't find files by remembered content fragment without entering chat.

A single `/search` endpoint solves both. Mode-switching is via `include` (which views to compute), not by routing to different endpoints.

`/query` becomes definitionally `/search + answering`; we'll likely refactor it that way internally even if the URL stays for back-compat.

### Architecture

**One pipeline, two views.** The retrieval pipeline runs once; views are projections.

```
query
  │
  ├─► filename BM25 ─► { doc_id: filename_score }   (new index, doc-keyed)
  │                              │
  │                              └────────┐
  │                                       ▼
  └─► RetrievalPipeline ───► [ScoredChunk] ── boost chunks of filename-matched docs ──► chunks view
                                            (small additive bonus, capped)
                                       │
                                       └─ for files view: roll up by doc_id (best chunk wins),
                                          RRF-fuse with filename BM25 hits ──► files view

  type / path filter applied to both views (post-filter)
```

* **Existing pipeline reused** for the chunks view. The full BM25 + vector + KG + tree-nav + RRF + expand + rerank stack runs unchanged. We DON'T re-run rerank for chunks here unless `overrides` says to — `/search` is meant to be cheap. Default behaviour: skip the rerank LLM call (`rerank.backend = "passthrough"` for `/search`).
* **New filename BM25 index** — same `InMemoryBM25Index` class, parallel persistence, doc-keyed (one entry per doc whose text is `f"{filename} {path} {format}"`). Built at startup alongside the content index, updated incrementally on ingest / rename.
* **Filename signal feeds both views**:
  - **chunks view**: `chunks_score += α * filename_score(chunk.doc_id)`. Small additive boost capped at ~20% of the top content score so an irrelevant-content file doesn't beat a perfect-content match purely on a vague filename. `α` defaults to `0.15`.
  - **files view**: per-doc RRF of (filename rank, best-content-chunk rank) — the parameter-free fusion that avoids "filename match always wins."
* **Path / type filter**: applied to the fused candidate set after both views are computed. Same shape the existing pipeline uses for trashed-doc filtering.
* **Stats payload**: counts per signal (`filename_hits`, `content_hits`, `total_files`, `total_chunks`), elapsed ms per phase. Cheap, lets clients show a "Searched 12,000 chunks across 340 files in 80ms" footer.

**Result shapes**:

```python
class ScoredChunkHit:                       # chunks view (always returned)
    chunk_id: str
    doc_id: str
    filename: str                           # convenience — UI doesn't need a second call
    path: str
    page_no: int
    snippet: str                            # ~200 chars
    score: float                            # post-RRF, post-filename-boost
    bbox: tuple[float, float, float, float] | None
    boosted_by_filename: bool               # provenance flag for trace

class FileHit:                              # files view (opt-in)
    doc_id: str
    filename: str
    path: str
    format: str
    score: float                            # RRF
    matched_in: list[str]                   # subset of {"filename", "content"}
    best_chunk: ChunkMatch | None           # populated when content matched
    filename_tokens: list[str] | None       # matched filename tokens, for UI bolding

class ChunkMatch:                           # the file's best content chunk, for snippet
    chunk_id: str
    snippet: str
    page_no: int
    score: float
```

**Frontend**:

* Workspace top-bar gets a search input that calls `/search?include=files` and renders the `files` view as a palette. Cmd/Ctrl+K opens a global file palette with the same input.
* Chat already calls `/query`; no change there yet. Future iterations may switch chat's "show retrieved chunks" debug pane to `/search` so it can render before the answer streams.

### Rejected alternatives

* **Two endpoints (`/search/chunks` + `/search/files`)** — defeats "unified". Two clients have to stitch two responses together when they want both views; one endpoint with `include` returns the joint view in one round-trip.
* **`/search` returns files only** (the previous draft of this roadmap) — too narrow. Agents and debug UIs primarily want chunks; files is a workspace-UX concern. Making chunks an opt-in feels backwards.
* **`/search` returns chunks only, separate `/files/search`** — same critique as the splitter above plus the URL-symmetry trap (`/chunks/search`, `/entities/search` next).
* **Mode parameter (`?mode=chunks|files|both`)** — `include` is more composable. `mode` reads as mutually exclusive; `include=["chunks","files","entities"]` reads as additive (and is forward-compatible with future result types).
* **Add filenames to the EXISTING content BM25** — pollutes chunk scoring on `/query`. A search for "Apple" would re-rank every PDF named `apple-quarterly.pdf` regardless of content. Keep indices separate, share the class.
* **Re-implement filename match as a Postgres `tsvector` / SQLite trigram** — premature dialect split. `InMemoryBM25Index` already handles the production-scale content index; filenames are 1–2 orders of magnitude smaller and trivially fit the same backend.
* **`SQL ILIKE`** — no relevance ranking, no fuzzy match, scales linearly.
* **Vector embedding for filename matching** — embeddings are unreliable on literal-string queries; lexical match is correct here.
* **Weighted-sum fusion for the files view** (`α * filename + β * content`) — needs hand-tuned weights. RRF is parameter-free and the codebase already uses it for the main retrieval merge.
* **Run rerank by default on `/search`** — defeats the "cheap retrieval primitive" goal. Default skips rerank; callers that want rerank pass `overrides.rerank = True` (existing per-call knob).

### Implementation

* New module: `retrieval/unified_search.py` with `UnifiedSearcher` — owns the filename `InMemoryBM25Index`, calls the existing `RetrievalPipeline` for the chunks view, applies the filename-boost, computes the files view from the per-doc rollup of chunks plus the filename BM25 hits.
* `api/state.py`: add `_filename_bm25` attribute and an incremental update path mirroring `_update_bm25_for_doc`. Rename hook updates the filename index entry.
* New route: `POST /api/v1/search` — flat `/search`, not nested under `/files` or `/chunks`. Calls `UnifiedSearcher.search()`.
* New schemas: `SearchRequest`, `SearchResponse`, `ScoredChunkHit`, `FileHit`, `ChunkMatch` in `api/schemas.py`.
* Refactor (later, optional): `/query` reimplemented as `/search` + `AnsweringPipeline.synthesize()`. URL stays for back-compat; internal code path collapses.
* Telemetry: one OTel span per search, attributes `q.length, include, chunk_hits, filename_hits, file_hits, rerank_used`.

Estimated size: ~400 LOC backend (filename index + searcher + boost logic + dual-view aggregator) + ~200 LOC frontend (workspace search bar + palette).

### Open questions

* **Default `include` value** — chunks-only is simplest; chunks+files is more useful by default for the workspace UI. Decision: chunks-only default keeps cost predictable; the workspace search bar will explicitly request `include=["files"]` (and only that, to skip computing the chunk view it doesn't render).
* **Filename-boost coefficient `α`** — start at `0.15`, expose as a config knob in `retrieval.unified_search.filename_boost_alpha`. Tune from real query logs once the feature has traffic.
* **Snippet generation for chunks view** — the existing pipeline produces snippets from chunk content; reuse. Files view's snippet comes from `best_chunk` (which is just the top-scored content chunk for that doc).

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
| 1 | Unified `/search` | 1.5 | — | New filename BM25 + dual-view aggregator on top of the existing pipeline; `/query` keeps working unchanged, gains a cheap retrieval-only sibling. |
| 2 | Retrieval MCP | 1.5 | (1) for the file-search tool | Exposes existing surface; collects external feedback while heavier work proceeds. |
| 3 | Agentic search | 3 | (1), (2) | Core capability that deep research depends on. Ships independently as a power-user feature. |
| 4 | Deep research | 4 | (3) | Largest. Heavy frontend work + new persistence + the most user-visible output. |

Total: ~10 eng-weeks for all four. Each can ship independently with its own branch + merge to `dev`.

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
