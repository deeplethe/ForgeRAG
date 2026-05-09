# Changelog

All notable changes to OpenCraig (OSS edition).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.6.0] — 2026-05-09 — First preview release

This release reframes OpenCraig as **managed agentic workspaces
with permission-aware retrieval** — each user gets a per-user
sandbox container where the agent does the work, and retrieval
across the team's knowledge respects existing folder permissions.
v0.6.0 is the first public preview of the OSS edition; the repo
continues to be developed and maintained going forward, with v1.0
targeted for a stable cut once the API surface settles.

A separate commercial product, **OpenCraig Enterprise (v3.0+)**,
ships features specifically for enterprise deployments — lineage,
audit, promote-to-library, sandboxed code execution, skills,
SSO / SCIM. Both editions are alive and developed in parallel.
See the [README](README.md#-editions) for the OSS-vs-Enterprise
boundary.

### Changed — major

- **Agent runtime: Claude Agent SDK.** v0.6.0 ships the same loop
  that powers Claude Code as the in-process and in-container agent
  driver. The runtime selection that landed in an earlier internal
  wave referenced a package that turned out not to exist on PyPI;
  tests had been mocking the import, masking a runtime gap until
  the sandbox image actually tried to install it. The cutover to
  claude-agent-sdk keeps the same SSE event vocabulary so frontend
  parsers don't change.
- **In-container path uses the SDK's bundled CLI binary.** The wheel
  ships a per-platform self-contained binary (no Node.js runtime
  dep). `pip install claude-agent-sdk` in the sandbox image plus a
  symlink to `/usr/local/bin/claude` is the entire installation. The
  Python entrypoint at `/opt/opencraig/opencraig_run_turn.py` calls
  `query()` and emits one JSONL event per stdout line for the
  backend to translate into SSE.
- **LLM proxy now serves both OpenAI and Anthropic wire formats.**
  - `POST /api/v1/llm/v1/chat/completions` — OpenAI shape.
  - `POST /api/v1/llm/anthropic/v1/messages` — Anthropic shape.
  Configured `api_key` / `api_base` from `answering.generator` are
  injected automatically; provider-specific env vars (`DEEPSEEK_
  API_KEY`, `OPENAI_API_KEY`, …) are not required. The agent points
  `ANTHROPIC_BASE_URL` at the proxy and any LiteLLM-supported
  provider works behind it (Anthropic, OpenAI, DeepSeek,
  SiliconFlow, Bedrock, Vertex, Ollama, …).
- **Auth middleware accepts `x-api-key` alongside `Authorization:
  Bearer`.** The bundled Claude CLI sends bearers as `x-api-key`
  per Anthropic API convention; without this branch every
  in-container turn 401'd. Same DB lookup either way; only the
  header source differs.
- **Chat route stays at `POST /api/v1/agent/chat`** for
  backwards compat. Wire format unchanged. The legacy
  `/api/v1/agent/chat` route from v0.x was deleted; rebind any
  external clients before upgrading.

### Verified end-to-end

The full pipe was exercised against a real DeepSeek deployment via
`docker run` against the sandbox image, with the agent reaching
back through a reverse SSH tunnel to the backend. The sample turn
"Reply with EXACTLY: pong" produced the JSONL events:

```
{"kind": "thinking", "text": "The user wants me to reply with exactly \"pong\"."}
{"kind": "done", "final_text": "pong", "iterations": 1}
```

Confirms: SDK install, bundled binary execution, MCP HTTP transport,
LiteLLM Anthropic ↔ DeepSeek wire-format translation, `api_key`
injection from configured generator, `x-api-key` auth, and the
JSONL event mapping that backends to our SSE protocol all work
together.

### Added

- **Folder-as-cwd workspace model.** v1.0 drops the project
  entity from the agent's mental model: each user owns a private
  workdir tree at `<user_workdirs_root>/<user_id>/`, bind-mounted
  into their sandbox container at `/workdir/`. Chats carry a
  `cwd_path` (e.g. `/sales/2025`) and the agent chdirs there
  before reading or writing files. The Workspace UI is now a
  folder browser over that tree; "Open chat here" anchors a
  conversation to the folder you're in. The Project entity and
  its routes are kept rendering for legacy project-bound chats
  but marked deprecated.
- **`/api/v1/workdir/...` route family** — list / mkdir / upload /
  download against the user's private workdir. Path safety:
  leading `/` means workdir root, `..` rejected, descendant
  validation via `Path.relative_to`.
- **`import_from_library(target_subpath=...)`** — workdir-relative
  target, resolves against `<user_workdirs_root>/<user_id>/`. The
  legacy `target_subdir` + bound `project_id` path stays for
  project-bound chats.
- **MCP server at `/api/v1/mcp`** — domain tools (`search_vector`,
  `read_chunk`, `read_tree`, `graph_explore`, `web_search`,
  `rerank`, `import_from_library`) exposed via the Model Context
  Protocol. Any MCP-compatible agent (the SDK, Claude Code, Cline,
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
- **`ClaudeRuntime` wrapper** (`api/agent/claude_runtime.py`) —
  driver for in-process Claude SDK that translates upstream callbacks
  into a stable event vocabulary, pumps work in a daemon thread,
  hard-disables built-in toolsets to prevent filesystem escape.

### Removed

- **Legacy agent loop and route** — `api/agent/loop.py`,
  `api/agent/llm.py`, `api/routes/agent.py`. Replaced by the
  SDK-driven path.
- **KernelManager + python_exec / bash_exec wrappers** — the
  earlier Phase 2 design built our own per-project ipykernel
  via `jupyter_client`. the Claude Agent SDK runs in-process for OSS; sandboxed
  code execution moves to Enterprise (where the Claude Agent SDK runs in a
  hardened container with bash/edit/grep enabled).
- **`benchmark/` module + `BenchmarkConfig`** — was coupled to
  the deleted agent loop. Re-implementable against `ClaudeRuntime`
  if needed; not shipped here.
- **`ToolRichOutputs.vue`** — rendered `python_exec` matplotlib
  output. With Python kernel removed, this preview surface is
  empty; the new code-execution UX lives in the Enterprise edition.

### Dependencies

- Added `claude-agent-sdk>=0.1.80` — wraps a self-contained
  per-platform `claude` CLI binary (~200 MB) shipped in the wheel.
  No Node.js runtime dep. The same loop that powers Claude Code,
  programmable from Python.
- Added `mcp>=1.27` — Anthropic-maintained Python SDK for the
  MCP server transport.

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
  the SDK / MCP / LLM-proxy tests added: 51.

### Migration from v0.3.x

If you have an existing v0.3.x deployment:

1. Database schema is unchanged; alembic migrations apply
   normally.
2. **Frontend integrators**: swap any direct calls to
   `/api/v1/agent/chat` for `/api/v1/agent/chat`. Body
   shape is `{query, conversation_id}` (no `path_filters` or
   `message`). Wire format identical.
3. Set `OPENAI_API_KEY` (or Anthropic / Gemini equivalent) in
   `.env` — the SDK reads this directly. The setup wizard
   continues to work for first-boot configuration.
4. Old `python_exec` / `bash_exec` calls in conversation
   history will silently be ignored — the SDK won't see those
   tools. Re-running a turn produces a fresh trace.

---

## Pre-1.0.0

Released under the v0.1.x – v0.3.x line during the pre-stable
period (2025–early 2026). See git history for per-commit detail.
The 1.0.0 release is the **first stable OSS milestone**; all
prior tags should be considered development snapshots.
