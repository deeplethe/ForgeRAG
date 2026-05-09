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
    call_id: str, tool: str, latency_ms: int, result_summary: dict | None
) -> dict:
    return {
        "kind": "tool_end",
        "id": call_id,
        "tool": tool,
        "latency_ms": latency_ms,
        "result_summary": result_summary or {},
    }


def _evt_done(final_text: str, **extras: Any) -> dict:
    return {"kind": "done", "final_text": final_text, **extras}


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
        async def _prompt_stream():
            for msg in conversation_history or []:
                role = msg.get("role")
                content = msg.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content:
                    yield {
                        "type": "user" if role == "user" else "assistant",
                        "message": {"role": role, "content": content},
                        "parent_tool_use_id": None,
                        "session_id": "",
                    }
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_message},
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
                emit(_evt_tool_end(cid, str(tool_name), latency_ms, summary))
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
                        # Whole-message content blocks. Token-level
                        # deltas come via StreamEvent (handled below)
                        # so we only emit thought / tool_use here —
                        # text content blocks would double-emit.
                        for block in msg.content:
                            if isinstance(block, sdk.ThinkingBlock):
                                emit(_evt_thinking(block.thinking))
                            elif isinstance(block, sdk.TextBlock):
                                # If partial-streaming is OFF (or no
                                # delta events arrived for this block),
                                # emit the whole text once.
                                if block.text and not _seen_partial.get(id(block)):
                                    emit(_evt_answer_delta(block.text))
                    elif isinstance(msg, sdk.StreamEvent):
                        # Token-level streaming. Anthropic SSE format:
                        #   {"type": "content_block_delta",
                        #    "delta": {"type": "text_delta", "text": "..."}}
                        ev = msg.event or {}
                        if ev.get("type") == "content_block_delta":
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

        # Track which TextBlock objects already had their text fed
        # via partial deltas — avoids double-emission of the same
        # text on the wrap-up AssistantMessage.
        _seen_partial: dict[int, bool] = {}

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
