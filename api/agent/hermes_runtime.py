"""
Hermes Agent runtime — backend-process integration.

Wraps the ``run_agent.AIAgent`` class from the upstream Hermes Agent
package (NousResearch, MIT) into a thin API the rest of OpenCraig
talks to. The Wave 2.5 chat route uses this module to drive a
single conversational turn end-to-end:

    user message ──► HermesRuntime.run_turn(...)
                       │
                       ├─► AIAgent.run_conversation()  (in worker thread)
                       │     │
                       │     └─► tool callbacks fire as Hermes loops
                       │           │
                       │           └─► on_event(...) callback bridges
                       │                  to the SSE stream
                       │
                       └─► returns final answer + history

Why hermes-as-library, not subprocess CLI:

  1. The ``hermes chat`` CLI's quiet mode (``-Q``) suppresses
     intermediate tool activity and only emits the final answer
     as plain text. There's no machine-readable event stream.
     Library mode exposes per-event callbacks (tool_start /
     tool_end / thinking / stream_delta / etc.) we can map cleanly
     to our SSE format.

  2. No subprocess startup latency per turn (~100–300 ms).

  3. Same code path on Windows + Linux + macOS dev — no Docker
     dependency for the agent loop tests. (Container *workspace*
     mode where the agent needs real filesystem isolation lands in
     Wave 2.5b — uses a different runtime path.)

Built-in tool isolation:

  Hermes ships 40+ built-in tools (Read / Edit / Bash / Glob /
  Grep / WebFetch / browser / file_operations / etc.). When Hermes
  runs IN OUR BACKEND PROCESS, those tools would operate on OUR
  process's filesystem — escape risk. So we initialise AIAgent
  with ``enabled_toolsets=[]``, leaving only what we explicitly
  expose via MCP. The MCP server (``api.routes.mcp_server``) and
  the wrappers in ``api.routes.mcp_tools`` are the agent's entire
  tool surface in B-MVP.

  Wave 2.5b will introduce a sandbox-proxy MCP tool family
  (``sandbox_bash`` / ``sandbox_read`` / etc.) that proxies
  filesystem ops INTO the user's container. Same isolation model,
  just executed remotely.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event types emitted by run_turn(..., on_event=...)
# ---------------------------------------------------------------------------
#
# Stable event shapes the chat route can turn into SSE without
# touching the runtime internals. Everything Hermes exposes via
# callbacks fans out to one of these.


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
class HermesTurnConfig:
    """Per-turn knobs. Stable shape so the chat route can pass these
    in from request body / conversation context without coupling to
    the upstream AIAgent constructor."""

    model: str
    base_url: str  # OpenAI-compat URL — points at our /api/v1/llm/v1
    api_key: str  # session bearer / API token
    max_iterations: int = 90
    system_message: str | None = None
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    """``{name: {url: "...", headers: {...}}}`` — MCP servers Hermes
    should connect to. The default OpenCraig MCP server is added by
    the chat route, but extra (e.g. user-configured) servers can be
    threaded through here."""


@dataclass
class HermesTurnResult:
    """What ``run_turn`` returns after the agent finishes."""

    final_text: str
    history: list[dict] = field(default_factory=list)
    iterations: int = 0
    raw: dict = field(default_factory=dict)
    """The full ``run_conversation`` return dict, preserved for the
    chat route to extract whatever extras Hermes adds in future
    versions (cost / token counts / fallback events / ...)."""


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class HermesUnavailableError(RuntimeError):
    """Raised when the ``run_agent`` module isn't importable. The
    chat route catches this and surfaces a 503 — better than a
    cryptic ImportError mid-stream."""


def _import_agent_class():
    """Lazy import of ``run_agent.AIAgent``. Kept out of module
    top-level so test environments without hermes-agent installed
    can still ``import api.agent.hermes_runtime`` for type checks."""
    try:
        from run_agent import AIAgent  # type: ignore
    except ImportError as e:
        raise HermesUnavailableError(
            "hermes-agent not installed. Run "
            "``pip install hermes-agent`` to enable the agent route."
        ) from e
    return AIAgent


class HermesRuntime:
    """Single-turn runner. Stateless across turns — instantiate once
    per chat turn or reuse a single instance, both work; conversation
    history travels in/out via the run_turn args.

    Test-friendliness: ``agent_factory`` lets tests inject a fake
    AIAgent class without monkey-patching ``run_agent``. Production
    code constructs ``HermesRuntime()`` with no args.
    """

    def __init__(self, *, agent_factory=None):
        self._agent_factory = agent_factory or _import_agent_class

    def run_turn(
        self,
        user_message: str,
        *,
        config: HermesTurnConfig,
        conversation_history: list[dict] | None = None,
        on_event: callable | None = None,
    ) -> HermesTurnResult:
        """Run one chat turn synchronously. Returns the final result.

        ``on_event`` is invoked from the same thread that drives the
        AIAgent; the chat route bridges this into its SSE async
        generator via a queue (see ``stream_turn`` for the helper
        the route actually uses).
        """
        AIAgentCls = self._agent_factory()

        emit = on_event or (lambda _evt: None)

        # Adapt our standardised event names to AIAgent's callback
        # naming. Each callback signature is best-effort: AIAgent
        # versions may add args, so we accept ``*args, **kwargs`` and
        # extract what we need.
        def _tool_start_cb(*args, **kwargs):
            try:
                tool = kwargs.get("tool") or (args[0] if args else "")
                params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
                call_id = kwargs.get("call_id") or kwargs.get("id") or ""
                emit(_evt_tool_start(str(call_id), str(tool), dict(params or {})))
            except Exception:
                log.exception("hermes_runtime: tool_start_cb formatting")

        def _tool_complete_cb(*args, **kwargs):
            try:
                tool = kwargs.get("tool") or (args[0] if args else "")
                call_id = kwargs.get("call_id") or kwargs.get("id") or ""
                latency_ms = int(kwargs.get("latency_ms") or 0)
                summary = kwargs.get("result_summary") or kwargs.get("result") or {}
                if not isinstance(summary, dict):
                    summary = {"text": str(summary)[:200]}
                emit(_evt_tool_end(str(call_id), str(tool), latency_ms, summary))
            except Exception:
                log.exception("hermes_runtime: tool_complete_cb formatting")

        def _thinking_cb(*args, **kwargs):
            try:
                text = kwargs.get("text") or (args[0] if args else "")
                if text:
                    emit(_evt_thinking(str(text)))
            except Exception:
                log.exception("hermes_runtime: thinking_cb formatting")

        def _stream_delta_cb(*args, **kwargs):
            try:
                text = kwargs.get("text") or kwargs.get("delta") or (
                    args[0] if args else ""
                )
                if text:
                    emit(_evt_answer_delta(str(text)))
            except Exception:
                log.exception("hermes_runtime: stream_delta_cb formatting")

        agent_kwargs: dict[str, Any] = {
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
            "max_iterations": config.max_iterations,
            "quiet_mode": True,
            # Built-in tool isolation: Hermes runs in OUR backend
            # process — its built-in Read / Edit / Bash / Grep / etc.
            # would operate on our filesystem. Hard-disable them; MCP
            # is the agent's entire tool surface for B-MVP.
            "enabled_toolsets": [],
            # Don't persist sessions to ~/.hermes/sessions/ —
            # OpenCraig's Conversation table is the source of truth.
            "persist_session": False,
            # Don't load the user's ambient context files / memory
            # from ~/.hermes/ — those are Hermes-CLI-user concepts
            # and would leak the operator's personal config into
            # tenant chats.
            "skip_context_files": True,
            "skip_memory": True,
            # Per-event callbacks → standardised events.
            "tool_start_callback": _tool_start_cb,
            "tool_complete_callback": _tool_complete_cb,
            "thinking_callback": _thinking_cb,
            "stream_delta_callback": _stream_delta_cb,
        }
        if config.system_message:
            agent_kwargs["ephemeral_system_prompt"] = config.system_message

        try:
            agent = AIAgentCls(**agent_kwargs)
        except Exception as e:
            log.exception("hermes_runtime: AIAgent init failed")
            emit(_evt_error(f"AIAgent init failed: {type(e).__name__}",
                            type_=type(e).__name__))
            raise

        try:
            raw = agent.run_conversation(
                user_message,
                conversation_history=conversation_history or [],
            )
        except Exception as e:
            log.exception("hermes_runtime: run_conversation raised")
            emit(_evt_error(f"agent failed: {type(e).__name__}",
                            type_=type(e).__name__))
            raise

        # Extract a sane "final answer" + history regardless of
        # version-specific Hermes return shape.
        final_text = ""
        history: list[dict] = []
        iterations = 0
        if isinstance(raw, dict):
            for k in ("response", "final_response", "text", "answer", "content"):
                v = raw.get(k)
                if isinstance(v, str) and v:
                    final_text = v
                    break
            history_v = raw.get("messages") or raw.get("history")
            if isinstance(history_v, list):
                history = [m for m in history_v if isinstance(m, dict)]
            iter_v = raw.get("iterations")
            if isinstance(iter_v, int):
                iterations = iter_v

        emit(_evt_done(final_text, iterations=iterations))
        return HermesTurnResult(
            final_text=final_text,
            history=history,
            iterations=iterations,
            raw=raw if isinstance(raw, dict) else {},
        )


# ---------------------------------------------------------------------------
# Streaming helper used by the chat route
# ---------------------------------------------------------------------------


def stream_turn(
    runtime: HermesRuntime,
    user_message: str,
    *,
    config: HermesTurnConfig,
    conversation_history: list[dict] | None = None,
) -> Iterator[dict]:
    """Run a turn in a worker thread, yield events as they arrive.

    The chat route (Wave 2.5) wraps this in an async SSE generator.
    Worker thread + queue is the same pattern the existing agent
    route already uses for its ``loop.py`` driver — keeps the SSE
    machinery identical so the cutover (Wave 3) is minimal.
    """
    q: queue.Queue = queue.Queue()
    SENTINEL = object()

    def _on_event(evt: dict):
        try:
            q.put_nowait(evt)
        except Exception:
            log.exception("hermes_runtime: queue put failed")

    error_holder: dict = {}

    def _worker():
        try:
            runtime.run_turn(
                user_message,
                config=config,
                conversation_history=conversation_history,
                on_event=_on_event,
            )
        except Exception as e:
            error_holder["error"] = e
            # If run_turn raised before the ``done`` event, emit
            # an explicit error event so the consumer ends cleanly.
            try:
                q.put_nowait(
                    _evt_error(
                        f"agent failed: {type(e).__name__}",
                        type_=type(e).__name__,
                    )
                )
            except Exception:
                log.exception("hermes_runtime: error-event put failed")
        finally:
            q.put(SENTINEL)

    t = threading.Thread(target=_worker, daemon=True, name="hermes-turn")
    t.start()
    while True:
        evt = q.get()
        if evt is SENTINEL:
            break
        yield evt
    t.join(timeout=5.0)
