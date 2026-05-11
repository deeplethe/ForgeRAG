"""
Runtime adapter — bridges the existing sync-generator Claude runtimes
(``claude_runtime.stream_turn`` / ``claude_container_runtime.stream_turn_container``)
to the new event-bus model.

The runtimes were written before AgentTaskHandle existed: they yield
event dicts via a Python generator that the route's pump loop converted
to SSE. This adapter pumps the same generator on a worker thread but
emits each event through ``handle.emit(...)`` instead — so the agent
becomes fully decoupled from any SSE connection, and disconnect-survival
+ reconnect-via-stream falls out automatically.

Translation table (runtime kind → handle event type):

    "thinking"     → "thought"        {text}
    "answer_delta" → "token"          {delta}
    "tool_start"   → "tool_start"     {tool, call_id, input}
    "tool_end"     → "tool_end"       {call_id, latency_ms, output, is_error,
                                       result_summary}
    "citations"    → "citation"       {items}
    "usage"        → "usage"          {input_tokens, output_tokens}
    "error"        → "error"          {message, type}
    "done"         → (folded into the caller's terminal "done" emit)

The adapter does NOT emit the terminal "done" or call ``handle.close()`` —
the caller's background task owns lifecycle (so it can persist the
assistant Message + final AgentRun state before closing).

Sandbox-agnostic: this file picks container-vs-in-process based on
``state.sandbox``; future microVM / Firecracker backends slot in
behind ``state.sandbox`` without touching this layer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .claude_container_runtime import (
    ClaudeContainerRunner,
    SandboxUnavailableError,
    stream_turn_container,
)
from .claude_runtime import (
    ClaudeRuntime,
    ClaudeTurnConfig,
    ClaudeUnavailableError,
    stream_turn,
)
from .task_handle import AgentTaskHandle

log = logging.getLogger(__name__)


async def run_agent_through_handle(
    handle: AgentTaskHandle,
    *,
    composed_query: str,
    config: ClaudeTurnConfig,
    use_container: bool,
    state: Any,  # AppState (Any to dodge a circular import)
    conversation_history: list[dict] | None = None,
    extra_user_content_blocks: list[dict] | None = None,
    principal_user_id: str | None = None,
    cwd_path: str | None = None,
) -> dict[str, Any]:
    """Run one agent turn, emitting all events through ``handle.emit``.

    Returns a dict with:
        final_text         str   — the concatenated answer body
        iterations         int   — number of SDK iterations
        input_tokens       int   — turn input tokens (from ResultMessage.usage)
        output_tokens      int   — turn output tokens
        citations_pool     list  — final citations snapshot
        error              str | None  — set when the run failed mid-stream
                                         (the caller decides how to record this
                                          on the run row; the adapter has already
                                          emitted an "error" event by then)

    Cancellation: if the calling task is cancelled (e.g. shutdown), the
    asyncio executor wrapping ``next(it)`` will get cleaned up by
    asyncio. We don't try to stop the underlying SDK process — that's
    Inc 4 (interrupt via user_inbox check between events) when we wrap
    the runtime to honour cooperative cancellation. For now an
    interrupted run waits until the SDK returns its next event before
    actually stopping. Acceptable for MVP — SDK iterations are typically
    1-10s.
    """
    # ── Build the right runtime iterator ─────────────────────────────
    try:
        if use_container:
            container_runner = ClaudeContainerRunner(state.sandbox)
            iter_ = stream_turn_container(
                container_runner,
                composed_query,
                config=config,
                principal_user_id=principal_user_id,
                cwd_path=cwd_path,
                conversation_history=conversation_history or [],
                extra_user_content_blocks=extra_user_content_blocks or [],
            )
        else:
            runtime = ClaudeRuntime()
            iter_ = stream_turn(
                runtime,
                composed_query,
                config=config,
                conversation_history=conversation_history or [],
                extra_user_content_blocks=extra_user_content_blocks or [],
            )
    except (ClaudeUnavailableError, SandboxUnavailableError) as e:
        await handle.emit("error", {"message": str(e), "type": type(e).__name__})
        return {
            "final_text": "",
            "iterations": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "citations_pool": [],
            "error": str(e),
        }

    # ── Pump the sync generator on a thread ──────────────────────────
    loop = asyncio.get_event_loop()
    sentinel = object()

    def _next_or_sentinel(it):
        try:
            return next(it)
        except StopIteration:
            return sentinel

    final_text = ""
    iterations = 0
    delta_buf: list[str] = []
    citations_pool: list[dict] = []
    turn_input_tokens = 0
    turn_output_tokens = 0
    error_message: str | None = None

    await handle.emit("phase", {"phase": "executing"})

    try:
        while True:
            evt = await loop.run_in_executor(None, _next_or_sentinel, iter_)
            if evt is sentinel:
                break
            if not isinstance(evt, dict):
                continue

            kind = evt.get("kind")

            if kind == "thinking":
                await handle.emit("thought", {"text": evt.get("text", "")})
            elif kind == "answer_delta":
                text = evt.get("text", "")
                delta_buf.append(text)
                await handle.emit("token", {"delta": text})
            elif kind == "tool_start":
                await handle.emit(
                    "tool_start",
                    {
                        "tool": evt.get("tool", ""),
                        "call_id": evt.get("id", ""),
                        "input": evt.get("params"),
                    },
                )
            elif kind == "tool_end":
                await handle.emit(
                    "tool_end",
                    {
                        "call_id": evt.get("id", ""),
                        "latency_ms": int(evt.get("latency_ms") or 0),
                        "output": str(evt.get("output") or ""),
                        "is_error": bool(evt.get("is_error")),
                        "result_summary": evt.get("result_summary")
                        if isinstance(evt.get("result_summary"), dict)
                        else None,
                    },
                )
            elif kind == "citations":
                items = evt.get("items") or []
                if isinstance(items, list):
                    citations_pool = items
                await handle.emit("citation", {"items": items})
            elif kind == "usage":
                tin = int(evt.get("input_tokens") or 0)
                tout = int(evt.get("output_tokens") or 0)
                turn_input_tokens = tin
                turn_output_tokens = tout
                # Set absolute values on the handle (single-turn run). Sub-agent
                # aggregation (parent total = sum over children + own) lands in
                # Inc 5; for Inc 3 the agent is single-shot per run.
                handle.total_input_tokens = tin
                handle.total_output_tokens = tout
                await handle.emit(
                    "usage",
                    {"input_tokens": tin, "output_tokens": tout},
                )
                if handle.is_over_budget():
                    await handle.emit(
                        "budget_warning",
                        {
                            "kind": "token",
                            "current": tin + tout,
                            "limit": handle.token_budget_total,
                        },
                    )
            elif kind == "error":
                error_message = (
                    f"{evt.get('type', 'RuntimeError')}: "
                    f"{evt.get('message', 'agent failed')}"
                )
                await handle.emit(
                    "error",
                    {
                        "message": error_message,
                        "type": evt.get("type", "RuntimeError"),
                    },
                )
            elif kind == "done":
                iterations = int(evt.get("iterations") or 0)
                ft = evt.get("final_text") or ""
                if ft:
                    final_text = ft
            # Unknown kinds: drop silently. The runtime may emit
            # implementation-detail events (e.g. heartbeats) we don't
            # care to forward.
    except asyncio.CancelledError:
        # Caller cancelled (shutdown / interrupt). Surface as error event
        # so reconnecting clients see a terminal state, then re-raise so
        # the wrapping task knows it was cancelled.
        #
        # Python 3.11+ re-delivers cancellation on every await even
        # inside ``except CancelledError`` — uncancel() consumes the
        # pending cancel request so the emit completes; we raise a
        # fresh CancelledError after to propagate normally.
        try:
            asyncio.current_task().uncancel()
        except Exception:
            pass
        try:
            await handle.emit(
                "error",
                {"message": "agent run cancelled", "type": "Cancelled"},
            )
        except Exception:
            log.exception(
                "runtime_adapter: cancel-emit failed run=%s", handle.run_id
            )
        raise asyncio.CancelledError()
    except Exception as e:
        log.exception("runtime_adapter: pump raised run=%s", handle.run_id)
        error_message = error_message or f"agent failed: {e}"
        await handle.emit(
            "error", {"message": error_message, "type": type(e).__name__}
        )

    # Fall back to delta concat if the SDK's done event didn't carry final_text.
    if not final_text:
        final_text = "".join(delta_buf)

    return {
        "final_text": final_text,
        "iterations": iterations,
        "input_tokens": turn_input_tokens,
        "output_tokens": turn_output_tokens,
        "citations_pool": citations_pool,
        "error": error_message,
    }
