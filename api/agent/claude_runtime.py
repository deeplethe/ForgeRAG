"""
Claude Agent SDK runtime — backend-process integration.

Drives one chat turn through ``claude-agent-sdk`` — the same loop
that powers Claude Code, exposed as a Python library wrapping a
self-contained CLI binary bundled in the wheel (no Node.js needed
at runtime).

Architecture::

    user message ──► ClaudeRuntime.run_turn(...)
                       │
                       │   asyncio loop in worker thread
                       │     │
                       │     └─► claude_agent_sdk.query(prompt, options)
                       │            │
                       │            yields AssistantMessage / UserMessage /
                       │            ResultMessage / StreamEvent
                       │            │
                       │            └─► event mapper → on_event(...)
                       │                   │
                       │                   └─► SSE stream (chat route)
                       │
                       └─► returns final answer + iteration count

Why this module exists:

  1. Same agent loop as Claude Code — production-tested at scale.

  2. PyPI-installable. The SDK ships a self-contained binary per
     platform; ``pip install claude-agent-sdk`` is enough.

  3. Native MCP support — the SDK takes a dict of MCP server configs
     (``http`` / ``sse`` / ``stdio`` / in-process ``sdk``) and the
     loop calls them automatically. Drops cleanly onto our existing
     ``/api/v1/mcp`` endpoint.

  4. BYOK / multi-provider preserved: the SDK reads
     ``ANTHROPIC_BASE_URL`` so we point it at our LiteLLM proxy's
     Anthropic-compat path; LiteLLM translates to whatever provider
     OpenCraig is configured with (OpenAI / DeepSeek / SiliconFlow
     / Ollama / Bedrock / Vertex).

Built-in tool isolation:

  When the SDK runs IN-PROCESS on the backend, its built-in tools
  (Read / Edit / Write / Bash / Glob / Grep / WebFetch) would
  operate on the BACKEND'S filesystem — escape risk. We pass
  ``allowed_tools=[only-mcp-prefixed]`` so only our MCP-exposed
  domain tools fire. Container path (``ClaudeContainerRunner``)
  re-enables the built-ins because the container is the user's
  isolated workdir — Read / Edit / Bash there are wanted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event types emitted by run_turn(..., on_event=...)
# ---------------------------------------------------------------------------
#
# Stable wire shape the chat route translates into SSE blocks. Match
# the legacy claude_runtime event vocabulary so the route's mapping
# stays identical post-cutover.


def _evt_thinking(text: str) -> dict:
    return {"kind": "thinking", "text": text}


def _evt_answer_delta(text: str) -> dict:
    return {"kind": "answer_delta", "text": text}


def _evt_tool_start(call_id: str, tool: str, params: dict) -> dict:
    return {
        "kind": "tool_start",
        "id": call_id,
        "tool": tool,
        "params": params,
    }


def _evt_tool_end(
    call_id: str,
    tool: str,
    latency_ms: int,
    result_summary: dict | None,
    output: str = "",
) -> dict:
    return {
        "kind": "tool_end",
        "id": call_id,
        "tool": tool,
        "latency_ms": latency_ms,
        "result_summary": result_summary or {},
        # Stringified, length-capped tool response. Lets the frontend
        # render the raw output (Bash stdout, Read body, search hit
        # JSON, …) inside an expandable chip without us having to
        # bake per-tool formatting into the runtime. Cap is 8 KiB —
        # enough for a typical Bash run / file read; larger payloads
        # truncate with a marker so the trace JSON stays bounded.
        "output": output or "",
    }


# Per-call output cap — 8 KiB strikes the balance between "useful
# preview" and "DB row stays small". A run with 20 tool calls hits
# at most 160 KiB of trace JSON, comfortably inside the SQLite
# ``messages.agent_trace_json`` column's practical ceiling.
_TOOL_OUTPUT_MAX = 8192


def _stringify_tool_output(value) -> str:
    """Coerce a tool response (dict / list / str / None) to a
    string-and-truncate so it can ride a JSON event without bloating
    the trace blob. Dicts/lists go through ``json.dumps`` for
    readability; long strings get a "..[N chars truncated]" tail.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False, default=str, indent=2)
        except Exception:
            s = str(value)
    if len(s) > _TOOL_OUTPUT_MAX:
        s = s[:_TOOL_OUTPUT_MAX] + f"\n…[+{len(s) - _TOOL_OUTPUT_MAX} chars truncated]"
    return s


def _evt_citations(items: list[dict]) -> dict:
    """Per-turn citation pool. Emitted after each search / read tool
    finishes so the route can ship the running list to the frontend
    in the terminal ``done`` event. Each entry mirrors the legacy
    citation shape that ``Chat.vue::renderMsg`` matches against
    (``citation_id`` / ``cite_id``, ``chunk_id``, ``doc_id``,
    ``doc_name``, ``page_start``, ``page_end``)."""
    return {"kind": "citations", "items": items}


def _evt_done(final_text: str, **extras: Any) -> dict:
    return {"kind": "done", "final_text": final_text, **extras}


def _extract_citations_from_tool_response(tool_name: str, tool_response: Any) -> list[dict]:
    """Pull citation dicts out of an MCP tool response.

    Search / read / graph tools return JSON whose hits / chunk records
    carry ``cite``, ``chunk_id``, ``doc_id``, ``doc_name``, etc. The
    MCP framework wraps the JSON inside a content-block envelope
    (``[{"type": "text", "text": "<serialised json>"}]``); this
    helper strips the wrapping and parses the inner records.

    Returns an empty list for non-search tools or unparseable
    responses; the caller folds the result into a per-turn pool
    that's emitted with ``done``.
    """
    citation_tools = {
        "mcp__opencraig__search_vector",
        "mcp__opencraig__search_bm25",
        "mcp__opencraig__read_chunk",
        "mcp__opencraig__graph_explore",
        "mcp__opencraig__rerank",
    }
    if tool_name not in citation_tools:
        return []

    # MCP returns content blocks in a few shapes depending on the
    # SDK version: a dict with ``content`` list, a bare list of
    # blocks, or already-parsed JSON. Normalise to a JSON-string we
    # can json.loads.
    raw = tool_response
    if isinstance(raw, dict) and "content" in raw:
        raw = raw["content"]
    if isinstance(raw, list):
        # Take the first text-typed block.
        for blk in raw:
            if isinstance(blk, dict) and blk.get("type") == "text":
                raw = blk.get("text", "")
                break

    parsed: Any = None
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
    else:
        return []

    items: list[dict] = []

    def _absorb(record: dict) -> None:
        # Both ``hits`` and a single ``read_chunk`` result share the
        # same field names — keep the citation fields, drop the rest.
        chunk_id = record.get("chunk_id")
        cite = record.get("cite") or record.get("citation_id")
        if not (chunk_id or cite):
            return
        items.append(
            {
                "citation_id": cite,
                "cite_id": cite,
                "chunk_id": chunk_id,
                "doc_id": record.get("doc_id"),
                "doc_name": record.get("doc_name"),
                "path": record.get("path"),
                "page_start": record.get("page_start"),
                "page_end": record.get("page_end"),
                "snippet": record.get("snippet"),
                "score": record.get("score"),
            }
        )

    if isinstance(parsed, dict):
        # search_vector / search_bm25 → {"hits": [...]}
        for hit in (parsed.get("hits") or []):
            if isinstance(hit, dict):
                _absorb(hit)
        # read_chunk → flat record
        if "chunk_id" in parsed:
            _absorb(parsed)
        # graph_explore → {entities: [...], relations: [...]} — these
        # don't carry chunk-level cite ids today, so they contribute
        # nothing to the citation list. Skip silently.
    return items


def _evt_error(message: str, *, type_: str = "RuntimeError") -> dict:
    return {"kind": "error", "type": type_, "message": message}


# ---------------------------------------------------------------------------
# Config + result types
# ---------------------------------------------------------------------------


@dataclass
class ClaudeTurnConfig:
    """Per-turn knobs.

    Names match the legacy ClaudeTurnConfig 1:1 so callers can
    swap the import without touching the body. ``base_url`` /
    ``api_key`` are forwarded as ``ANTHROPIC_BASE_URL`` /
    ``ANTHROPIC_API_KEY`` env vars to the SDK's bundled CLI.
    """

    model: str
    base_url: str
    """The Anthropic-compat URL the SDK should hit. In our
    deployment this is the LiteLLM proxy at
    ``/api/v1/llm/anthropic`` (with no ``/v1/messages`` suffix —
    the SDK appends that itself)."""

    api_key: str
    max_iterations: int = 90
    system_message: str | None = None
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    """``{name: {url: "...", headers: {...}}}``. Each entry becomes
    an HTTP MCP server config (``type: "http"``) for the SDK. The
    chat route adds the built-in OpenCraig MCP server (with the
    user's bearer threaded into headers) before calling run_turn."""

    cwd: str | None = None
    """Working directory the agent's built-in shell tools see. For
    the in-process backend path this is unused (we disable built-in
    tools); for the container path the container entrypoint sets
    this to ``/workdir/<conversation cwd>/`` before spawning the
    SDK."""

    allowed_tools: list[str] | None = None
    """Override the SDK's allowed-tools list. ``None`` falls back to
    the runtime's built-in policy: in-process = MCP-only; in-
    container = MCP + built-in file/shell tools."""


@dataclass
class ClaudeTurnResult:
    """What ``run_turn`` returns after the loop terminates."""

    final_text: str
    history: list[dict] = field(default_factory=list)
    iterations: int = 0
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class ClaudeUnavailableError(RuntimeError):
    """Raised when ``claude_agent_sdk`` isn't importable or the
    bundled CLI binary can't be located. The chat route catches
    this and surfaces a 503."""


def _import_sdk():
    """Lazy import of claude_agent_sdk. Module-level import would
    pull the SDK + its bundled binary into every test process,
    including ones that don't exercise the agent path."""
    try:
        import claude_agent_sdk  # type: ignore
    except ImportError as e:
        raise ClaudeUnavailableError(
            "claude-agent-sdk not installed. Run "
            "``pip install claude-agent-sdk`` to enable the agent route."
        ) from e
    return claude_agent_sdk


class ClaudeRuntime:
    """Single-turn runner. Stateless across turns — instantiate once
    or reuse, both work; conversation history travels in/out via the
    run_turn args.

    Test-friendliness: ``sdk_module`` lets tests inject a fake module
    without monkey-patching ``claude_agent_sdk`` globally. Production
    code constructs ``ClaudeRuntime()`` with no args and the lazy
    importer pulls the real SDK on first use.
    """

    def __init__(self, *, sdk_module: Any | None = None):
        self._sdk_module = sdk_module
        self._sdk = sdk_module  # cached after first _resolve_sdk()

    def _resolve_sdk(self):
        if self._sdk is None:
            self._sdk = _import_sdk()
        return self._sdk

    def run_turn(
        self,
        user_message: str,
        *,
        config: ClaudeTurnConfig,
        conversation_history: list[dict] | None = None,
        on_event=None,
    ) -> ClaudeTurnResult:
        """Run one chat turn synchronously.

        ``on_event`` fires from a worker-thread asyncio loop; the
        chat route bridges those events into its SSE async generator
        via ``stream_turn`` (queue-based handoff).
        """
        sdk = self._resolve_sdk()
        emit = on_event or (lambda _evt: None)

        # Build an Anthropic-shape prompt for streaming-input mode
        # so prior conversation turns are visible to the model.
        # ``query()`` accepts an AsyncIterable[dict] when you want
        # multi-message history; the dict envelope matches the SDK's
        # streaming-input contract documented in query()'s docstring.
        # The SDK's streaming-input mode treats EVERY ``type:"user"``
        # dict as a fresh prompt the agent must respond to. Replaying
        # history as a series of user/assistant pairs causes the SDK
        # to re-answer each historical user turn (observed: msgs=1 →
        # msgs=3 → msgs=5 progression on the LLM proxy logs, with the
        # frontend rendering all the responses concatenated as one
        # ballooning answer). Instead, fold history into a single
        # ``type:"user"`` whose content includes the prior turns as
        # context — the SDK then makes exactly one LLM call.
        #
        # The bundled claude.exe CLI iterates ``message.content`` looking
        # for tool-use blocks (``"tool_use_id" in block``); a plain string
        # makes JS iterate characters and the ``in`` operator throws
        # ``J is not an Object``. Always wrap as a content-block array.
        async def _prompt_stream():
            if conversation_history:
                lines: list[str] = ["[prior conversation]"]
                for m in conversation_history:
                    role = m.get("role")
                    content = m.get("content")
                    if role in ("user", "assistant") and isinstance(content, str) and content:
                        speaker = "User" if role == "user" else "Assistant"
                        lines.append(f"{speaker}: {content}")
                lines.append("[end prior conversation]")
                lines.append("")
                lines.append(user_message)
                composed = "\n".join(lines)
            else:
                composed = user_message
            yield {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": composed}],
                },
                "parent_tool_use_id": None,
                "session_id": "",
            }

        # Convert our flat ``mcp_servers`` config into the SDK's
        # tagged-union shape. We only support HTTP MCP today (our
        # /api/v1/mcp endpoint); stdio / sdk modes can be added when
        # there's a caller for them.
        sdk_mcp_servers: dict[str, dict] = {}
        for name, cfg in (config.mcp_servers or {}).items():
            sdk_mcp_servers[name] = {
                "type": "http",
                "url": cfg.get("url", ""),
                "headers": dict(cfg.get("headers", {})),
            }

        # Built-in tool isolation. When run on the backend host the
        # SDK's Read / Edit / Bash etc. would touch OUR filesystem —
        # we pass an allowed_tools list that excludes them, leaving
        # only MCP-prefixed names. The container runtime overrides
        # this with the full set since the container IS the
        # user-isolated workdir.
        if config.allowed_tools is not None:
            allowed_tools = list(config.allowed_tools)
        else:
            allowed_tools = [f"mcp__{n}__" for n in sdk_mcp_servers]

        # Per-tool-call timing ledger so tool_end events carry a
        # latency. The SDK doesn't surface end-to-end tool latency
        # itself; we tag start times by tool_use_id at PreToolUse
        # and diff at PostToolUse.
        tool_call_t0: dict[str, float] = {}
        tool_call_name: dict[str, str] = {}
        # Per-run citation pool: accumulated dedup'd records the model
        # may quote inline as ``[c_<id>]``. The post-tool hook appends
        # to this and emits a snapshot via ``_evt_citations`` so the
        # SSE route can ship the running list to the frontend.
        _citations: list[dict] = []
        _citation_keys: set[str] = set()

        def _on_pre_tool(input_data, tool_use_id, context):
            try:
                tool_name = input_data.get("tool_name") or ""
                tool_input = input_data.get("tool_input") or {}
                cid = str(tool_use_id or "")
                tool_call_t0[cid] = time.time()
                tool_call_name[cid] = tool_name
                emit(_evt_tool_start(cid, str(tool_name), dict(tool_input)))
            except Exception:
                log.exception("claude_runtime: pre-tool hook formatting")
            return {}

        def _on_post_tool(input_data, tool_use_id, context):
            try:
                cid = str(tool_use_id or "")
                t0 = tool_call_t0.pop(cid, None)
                latency_ms = int((time.time() - t0) * 1000) if t0 else 0
                tool_name = tool_call_name.pop(cid, "") or input_data.get("tool_name") or ""
                tool_response = input_data.get("tool_response")
                summary: dict
                if isinstance(tool_response, dict):
                    summary = {
                        k: v for k, v in tool_response.items()
                        if k in ("hit_count", "entity_count", "chunk_count", "error")
                    }
                    if not summary and "content" in tool_response:
                        summary = {"text": str(tool_response["content"])[:200]}
                else:
                    summary = {"text": str(tool_response)[:200]} if tool_response else {}
                # Full stringified output (capped) — lets the
                # frontend render the actual response (Bash stdout,
                # Read body, hit list, ...) inside an expandable
                # chip. ``result_summary`` keeps its narrow,
                # backwards-compatible shape for the chip headline.
                output = _stringify_tool_output(tool_response)
                emit(_evt_tool_end(cid, str(tool_name), latency_ms, summary, output))

                # Citation pool: search / read / rerank tools carry the
                # records the model will quote inline as ``[c_<id>]``.
                # Accumulate dedup'd by chunk_id (a follow-up
                # ``read_chunk`` for an already-seen hit shouldn't
                # dupe the rail). Emit AFTER tool_end so the
                # frontend's running citation list is populated in
                # arrival order.
                new_cites = _extract_citations_from_tool_response(
                    str(tool_name), tool_response,
                )
                if new_cites:
                    for c in new_cites:
                        key = c.get("chunk_id") or c.get("citation_id")
                        if not key or key in _citation_keys:
                            continue
                        _citation_keys.add(key)
                        _citations.append(c)
                    emit(_evt_citations(list(_citations)))
            except Exception:
                log.exception("claude_runtime: post-tool hook formatting")
            return {}

        async def _drive() -> tuple[str, int, dict]:
            options = sdk.ClaudeAgentOptions(
                model=config.model,
                system_prompt=config.system_message,
                mcp_servers=sdk_mcp_servers,
                allowed_tools=allowed_tools,
                cwd=config.cwd,
                # Pre-approve every tool the model picks. Backend has
                # already enforced authz at the MCP boundary; mid-loop
                # interactive prompts would deadlock the SSE stream.
                permission_mode="bypassPermissions",
                max_turns=config.max_iterations,
                # Token-level streaming — fans out to answer_delta
                # events so the frontend ticker shows progress.
                include_partial_messages=True,
                # Thinking disabled by default. Non-Anthropic providers
                # reached through our LiteLLM Anthropic-compat proxy
                # (DeepSeek / OpenAI / SiliconFlow / etc.) typically
                # don't emit signed thinking blocks; their reasoning
                # comes back as plain text content and bleeds into the
                # answer body. Disabling at the SDK level keeps the
                # answer clean. Native-Anthropic deployments can opt
                # back in by setting OPENCRAIG_THINKING=enabled.
                thinking={"type": "disabled"},
                env={
                    "ANTHROPIC_BASE_URL": config.base_url,
                    "ANTHROPIC_API_KEY": config.api_key,
                },
                hooks={
                    "PreToolUse": [
                        sdk.HookMatcher(matcher=".*", hooks=[_on_pre_tool])
                    ],
                    "PostToolUse": [
                        sdk.HookMatcher(matcher=".*", hooks=[_on_post_tool])
                    ],
                },
                # Don't bleed the operator's filesystem skills /
                # CLAUDE.md / etc. into tenant chats — explicit empty
                # source list keeps the SDK from auto-loading them.
                setting_sources=None,
            )

            final_text = ""
            iterations = 0
            raw: dict = {}

            try:
                async for msg in sdk.query(
                    prompt=_prompt_stream(), options=options
                ):
                    if isinstance(msg, sdk.AssistantMessage):
                        # Whole-message content blocks land here AFTER all
                        # the per-token StreamEvent deltas for this turn.
                        # If we already streamed a block's content via
                        # ``content_block_delta`` events, re-emitting the
                        # full block text would duplicate the answer in
                        # the UI (observed: every reply rendered twice
                        # back-to-back). Skip blocks at indices we already
                        # streamed; emit only blocks that arrived without
                        # partial deltas (non-streaming providers).
                        for i, block in enumerate(msg.content):
                            if i in _streamed_indices:
                                continue
                            if isinstance(block, sdk.ThinkingBlock):
                                if block.thinking:
                                    emit(_evt_thinking(block.thinking))
                            elif isinstance(block, sdk.TextBlock):
                                if block.text:
                                    emit(_evt_answer_delta(block.text))
                        # Reset for the next assistant message — each
                        # turn opens a fresh content_block sequence.
                        _streamed_indices.clear()
                    elif isinstance(msg, sdk.StreamEvent):
                        # Token-level streaming. Anthropic SSE format:
                        #   {"type": "content_block_delta",
                        #    "index": 0,
                        #    "delta": {"type": "text_delta", "text": "..."}}
                        ev = msg.event or {}
                        if ev.get("type") == "content_block_delta":
                            idx = ev.get("index")
                            if isinstance(idx, int):
                                _streamed_indices.add(idx)
                            delta = ev.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                t = delta.get("text") or ""
                                if t:
                                    emit(_evt_answer_delta(t))
                            elif delta.get("type") == "thinking_delta":
                                t = delta.get("thinking") or ""
                                if t:
                                    emit(_evt_thinking(t))
                    elif isinstance(msg, sdk.ResultMessage):
                        final_text = msg.result or ""
                        iterations = msg.num_turns or 0
                        raw = {
                            "stop_reason": msg.stop_reason,
                            "duration_ms": msg.duration_ms,
                            "duration_api_ms": msg.duration_api_ms,
                            "total_cost_usd": msg.total_cost_usd,
                            "usage": msg.usage,
                            "session_id": msg.session_id,
                            "is_error": msg.is_error,
                        }
                    # UserMessage / SystemMessage: no event mapping —
                    # tool results travel through PostToolUse hooks
                    # instead, and system init messages aren't
                    # interesting to the SSE consumer.
            except sdk.CLINotFoundError as e:
                raise ClaudeUnavailableError(
                    f"claude-agent-sdk CLI binary not found: {e}"
                ) from e
            return final_text, iterations, raw

        # Track which content_block indices were emitted via
        # token-level StreamEvent deltas this turn, so we don't
        # re-emit the same text when the wrap-up AssistantMessage
        # arrives with the same blocks. Reset at every
        # AssistantMessage (each turn opens fresh indices).
        _streamed_indices: set[int] = set()

        try:
            final_text, iterations, raw = asyncio.run(_drive())
        except ClaudeUnavailableError:
            raise
        except Exception as e:
            log.exception("claude_runtime: query failed")
            emit(_evt_error(
                f"agent failed: {type(e).__name__}: {e}",
                type_=type(e).__name__,
            ))
            raise

        emit(_evt_done(final_text, iterations=iterations))
        return ClaudeTurnResult(
            final_text=final_text,
            history=[],
            iterations=iterations,
            raw=raw,
        )


# ---------------------------------------------------------------------------
# Streaming helper used by the chat route
# ---------------------------------------------------------------------------


def stream_turn(
    runtime: ClaudeRuntime,
    user_message: str,
    *,
    config: ClaudeTurnConfig,
    conversation_history: list[dict] | None = None,
) -> Iterator[dict]:
    """Run a turn in a worker thread, yield events as they arrive.

    Same queue-based bridge as the legacy claude_runtime helper —
    the chat route's SSE machinery doesn't have to change. Worker
    thread spins up its own asyncio loop just for the SDK's async
    iterator (FastAPI's loop runs in the main thread; nesting a
    second ``asyncio.run()`` there would error).
    """
    q: queue.Queue = queue.Queue()
    SENTINEL = object()

    def _on_event(evt: dict):
        with contextlib.suppress(Exception):
            q.put_nowait(evt)

    def _worker():
        try:
            runtime.run_turn(
                user_message,
                config=config,
                conversation_history=conversation_history,
                on_event=_on_event,
            )
        except Exception as e:
            with contextlib.suppress(Exception):
                q.put_nowait(
                    _evt_error(
                        f"agent failed: {type(e).__name__}",
                        type_=type(e).__name__,
                    )
                )
        finally:
            q.put(SENTINEL)

    t = threading.Thread(target=_worker, daemon=True, name="claude-turn")
    t.start()
    while True:
        evt = q.get()
        if evt is SENTINEL:
            break
        yield evt
    t.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Backwards-compat aliases for code paths still importing the old names.
# Kept for the grace period during the cutover; remove after all
# callsites migrate to Claude* names.
# ---------------------------------------------------------------------------

ClaudeRuntime = ClaudeRuntime
ClaudeTurnConfig = ClaudeTurnConfig
ClaudeTurnResult = ClaudeTurnResult
ClaudeUnavailableError = ClaudeUnavailableError
