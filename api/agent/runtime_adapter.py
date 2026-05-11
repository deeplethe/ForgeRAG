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

    # Sub-agent tracking (Inc 5). The Claude Agent SDK's built-in Task
    # tool spawns a child agent and returns its final answer as the
    # tool result. We don't get our own AgentTaskHandle for it (the SDK
    # owns the lifecycle internally) but we DO want the UI to render
    # the nesting — so we emit synthesized ``sub_agent_start`` /
    # ``sub_agent_done`` events around each Task tool call.
    sub_agent_call_ids: set[str] = set()
    # Soft / hard budget warnings only fire once each per run.
    soft_budget_warned = False
    hard_budget_hit = False

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
                tool = evt.get("tool", "")
                call_id = evt.get("id", "")
                params = evt.get("params") or {}
                # SDK's built-in ``Task`` is the sub-agent spawn primitive.
                # Emit our higher-level event BEFORE the raw tool_start so
                # the UI can frame the nested chunk of activity as a
                # sub-agent block.
                if tool == "Task":
                    sub_agent_call_ids.add(call_id)
                    task_desc = (
                        params.get("prompt")
                        or params.get("description")
                        or params.get("task")
                        or ""
                    )
                    await handle.emit(
                        "sub_agent_start",
                        {
                            "parent_run_id": handle.run_id,
                            "parent_call_id": call_id,
                            "depth": handle.depth + 1,
                            "task_desc": str(task_desc)[:500],
                        },
                    )
                await handle.emit(
                    "tool_start",
                    {
                        "tool": tool,
                        "call_id": call_id,
                        "input": params,
                    },
                )
            elif kind == "tool_end":
                call_id = evt.get("id", "")
                output = str(evt.get("output") or "")
                is_error = bool(evt.get("is_error"))
                await handle.emit(
                    "tool_end",
                    {
                        "call_id": call_id,
                        "latency_ms": int(evt.get("latency_ms") or 0),
                        "output": output,
                        "is_error": is_error,
                        "result_summary": evt.get("result_summary")
                        if isinstance(evt.get("result_summary"), dict)
                        else None,
                    },
                )
                # Pair with the sub_agent_start emitted on the matching
                # tool_start. ``summary`` is the sub-agent's final answer
                # — truncated for the event payload.
                if call_id in sub_agent_call_ids:
                    sub_agent_call_ids.discard(call_id)
                    await handle.emit(
                        "sub_agent_done",
                        {
                            "parent_call_id": call_id,
                            "depth": handle.depth + 1,
                            "summary": output[:1000],
                            "is_error": is_error,
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
                is_incremental = bool(evt.get("incremental"))
                turn_input_tokens = tin
                turn_output_tokens = tout
                if is_incremental:
                    # Per-turn delta (round-6 Bug 12 fix). Each
                    # AssistantMessage reports just its own turn's
                    # tokens; we accumulate so total reflects the
                    # whole run, not the latest turn alone.
                    handle.add_usage(tin, tout)
                else:
                    # Cumulative report (legacy ResultMessage path or
                    # providers that only emit at session end). Take
                    # max so we don't regress if per-turn fired first
                    # and got us further ahead.
                    handle.total_input_tokens = max(
                        handle.total_input_tokens, tin
                    )
                    handle.total_output_tokens = max(
                        handle.total_output_tokens, tout
                    )
                await handle.emit(
                    "usage",
                    {
                        "input_tokens": handle.total_input_tokens,
                        "output_tokens": handle.total_output_tokens,
                    },
                )
                # Budget enforcement (Inc 5):
                #   - 80% of budget → soft warning event (once)
                #   - >=100% of budget → hard warning + cancel agent_task
                #     so the cleanup path lands the run in ``failed`` with
                #     a clear reason. The SDK call in flight finishes at
                #     its next safe point.
                budget = handle.token_budget_total
                if budget:
                    total = handle.total_input_tokens + handle.total_output_tokens
                    if not soft_budget_warned and total >= int(budget * 0.8):
                        soft_budget_warned = True
                        await handle.emit(
                            "budget_warning",
                            {
                                "severity": "soft",
                                "kind": "token",
                                "current": total,
                                "limit": budget,
                                "pct": round(100 * total / budget, 1),
                                "message": (
                                    "approaching token budget; consider "
                                    "narrowing remaining tool calls"
                                ),
                            },
                        )
                    if not hard_budget_hit and total >= budget:
                        hard_budget_hit = True
                        await handle.emit(
                            "budget_warning",
                            {
                                "severity": "hard",
                                "kind": "token",
                                "current": total,
                                "limit": budget,
                                "pct": round(100 * total / budget, 1),
                                "message": (
                                    "token budget exhausted — stopping"
                                ),
                            },
                        )
                        # Stop the run. The CancelledError will land in
                        # the wrapping background task's handler, which
                        # already does the right cleanup (emit
                        # interrupted, close handle, mark agent_run).
                        # We raise it ourselves rather than relying on
                        # an external cancel call so the in-flight pump
                        # doesn't need to round-trip through asyncio.
                        raise asyncio.CancelledError(
                            f"token budget {total}/{budget} exhausted"
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
