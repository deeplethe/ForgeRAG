# Changelog

All notable changes to OpenCraig (OSS edition).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.0.0] — 2026-05-09 — Final OSS release

This is the final open-source release under AGPLv3. Future
development continues as **OpenCraig Enterprise (v3.0+)**, a
separate commercial product. This repo is feature-frozen; security
patches accepted through 2027-05-09.

See the [README](README.md#-editions) for the OSS-vs-Enterprise
boundary.

### Changed — major

- **Agent runtime swapped to [Hermes Agent](https://github.com/NousResearch/hermes-agent)
  (NousResearch, MIT)**, running in-process with built-in
  filesystem tools hard-disabled (`enabled_toolsets=[]`). The
  prior handcrafted agent loop is gone. Hermes' tool surface is
  exclusively what OpenCraig exposes via MCP. Per-event callbacks
  (`tool_start_callback`, `stream_delta_callback`, etc.) bridge
  cleanly into our SSE stream.
- **Chat route is now `POST /api/v1/agent/hermes-chat`**.
  Wire format unchanged from v0.x — same `data: {type, ...}`
  SSE block envelope so existing frontend parsers keep working.
  The legacy `/api/v1/agent/chat` route was deleted in this
  release; rebind any external clients before upgrading.

### Added

- **MCP server at `/api/v1/mcp`** — domain tools (`search_vector`,
  `read_chunk`, `read_tree`, `graph_explore`, `web_search`,
  `rerank`, `import_from_library`) exposed via the Model Context
  Protocol. Any MCP-compatible agent (Hermes, Claude Code, Cline,
  custom) can plug into OpenCraig's retrieval surface.
- **OpenAI-compatible LLM proxy at `/api/v1/llm/v1/chat/completions`**
  — backed by litellm, multi-provider routing, streaming SSE,
  authenticated via the standard principal chain. Lets in-container
  agents (or any OpenAI-SDK client) reach configured providers
  through one URL with one key.
- **Per-conversation principal scoping for MCP** — every MCP tool
  call is wrapped with the authenticated user's
  `ToolContext` so multi-user folder-grant authz applies to the
  agent's retrieval the same way it does to direct API calls.
  Forward-compat hook lands a `call_id` per dispatch (logged
  today; persisted in the Enterprise edition's lineage backbone).
- **`HermesRuntime` wrapper** (`api/agent/hermes_runtime.py`) —
  driver for in-process Hermes that translates upstream callbacks
  into a stable event vocabulary, pumps work in a daemon thread,
  hard-disables built-in toolsets to prevent filesystem escape.

### Removed

- **Legacy agent loop and route** — `api/agent/loop.py`,
  `api/agent/llm.py`, `api/routes/agent.py`. Replaced by the
  Hermes-driven path.
- **KernelManager + python_exec / bash_exec wrappers** — the
  earlier Phase 2 design built our own per-project ipykernel
  via `jupyter_client`. Hermes runs in-process for OSS; sandboxed
  code execution moves to Enterprise (where Hermes runs in a
  hardened container with bash/edit/grep enabled).
- **`benchmark/` module + `BenchmarkConfig`** — was coupled to
  the deleted agent loop. Re-implementable against `HermesRuntime`
  if needed; not shipped here.
- **`ToolRichOutputs.vue`** — rendered `python_exec` matplotlib
  output. With Python kernel removed, this preview surface is
  empty; the new code-execution UX lives in the Enterprise edition.

### Dependencies

- Added `hermes-agent==0.10.0` (MIT) — pulls
  `firecrawl-py`, `parallel-web`, `fal-client`, `edge-tts`,
  `exa-py` as transitive deps (~50–100 MB, none used by
  OpenCraig). Acceptable cost for not maintaining our own loop.
- Added `mcp>=1.27` (MIT) — Anthropic-maintained Python SDK
  for the MCP server transport.

### Fixed (May 2026 audit)

- `_list_owned_project_ids` now caches on `ToolContext` per-turn.
  Previously each tool call in the same chat turn re-queried the
  projects table; with caching, first call queries, subsequent
  calls return the tuple. No staleness risk — `ToolContext` is
  built once per turn.
- `SandboxManager._lock_for` no longer holds the global table
  lock. CPython's `dict.setdefault` is atomic for the
  "get-or-create" pattern; the wrapper added contention without
  buying anything (every `ensure_container_for_user` was
  serialised through one mutex regardless of which user it was
  for).

### Test suite

- 852 tests passing, 4 skipped. Down from 897 in the v0.3.x line
  due to the deletion of legacy agent / benchmark tests
  (≈45 tests removed alongside the code they exercised). New
  Hermes / MCP / LLM-proxy tests added: 51.

### Migration from v0.3.x

If you have an existing v0.3.x deployment:

1. Database schema is unchanged; alembic migrations apply
   normally.
2. **Frontend integrators**: swap any direct calls to
   `/api/v1/agent/chat` for `/api/v1/agent/hermes-chat`. Body
   shape is `{query, conversation_id}` (no `path_filters` or
   `message`). Wire format identical.
3. Set `OPENAI_API_KEY` (or Anthropic / Gemini equivalent) in
   `.env` — Hermes reads this directly. The setup wizard
   continues to work for first-boot configuration.
4. Old `python_exec` / `bash_exec` calls in conversation
   history will silently be ignored — Hermes won't see those
   tools. Re-running a turn produces a fresh trace.

---

## Pre-1.0.0

Released under the v0.1.x – v0.3.x line during the OSS dev
period (2025–early 2026). See git history for per-commit detail.
The 1.0.0 release is the **first stable OSS milestone** and the
last; all prior tags should be considered development snapshots.
