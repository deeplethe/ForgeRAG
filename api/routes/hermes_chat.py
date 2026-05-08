"""
/api/v1/agent/hermes-chat — chat surface backed by Hermes Agent.

This is the Wave 2.5 route, the chat endpoint that B-MVP ships.
Coexists with the legacy ``/api/v1/agent/chat`` (handcrafted
``loop.py``-driven path) until Wave 3 cuts over.

Wire format (SSE, ``text/event-stream``):

    Each event is one ``data: <json>\\n\\n`` block. Order:

        agent.turn_start { turn: 1 }
        agent.thought    { text }            (zero or more)
        tool.call_start  { id, tool, params }
        tool.call_end    { id, tool, latency_ms, result_summary }
            (interleaved with answer.delta as Hermes loops)
        answer.delta     { text }            (token-stream of the model)
        agent.turn_end   { turn: 1 }
        done             { stop_reason, total_latency_ms,
                           final_text, error? }

The shape mirrors the legacy ``/agent/chat`` event vocabulary so
the frontend trace UI works against either route with minimal
adaptation. Wave 2.6's frontend changes are mostly about labelling
and artifact preview, not protocol.

Authz: the standard ``Depends(get_principal)`` covers cookie /
bearer auth (same as every other route). Hermes itself runs
in-process — its tool surface is whatever our MCP server exposes
(``api.routes.mcp_tools``), and those tools enforce per-user authz
via ``build_tool_context`` just like the legacy SSE route.

Persistence:
    * user message lands BEFORE the SSE stream opens (so
      mid-stream refresh always recovers at least the question)
    * final assistant answer lands after the ``done`` event
    * agent_run row records the turn (forward-compat hook for
      Wave 3.5 lineage backbone — turn_id ties back to the
      tool_call_log rows we'll start persisting then)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent.hermes_runtime import (
    HermesRuntime,
    HermesTurnConfig,
    HermesUnavailableError,
    stream_turn,
)
from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["hermes-chat"])


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


class HermesChatRequest(BaseModel):
    query: str = Field(..., min_length=1)
    conversation_id: str | None = None
    """Optional conversation to continue. When set, prior user /
    assistant messages are loaded as history and threaded into
    AIAgent.run_conversation(conversation_history=...)."""

    model: str | None = None
    """Override the default model from cfg.answering.generator.model.
    Useful for per-conversation experimentation; the chat UI sets
    this from a dropdown."""

    system_prompt_override: str | None = None
    """Per-turn system prompt override. Same knob the legacy route
    has; passed straight to AIAgent as ``ephemeral_system_prompt``."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_model_config(state: AppState, override: str | None) -> tuple[str, str | None, str | None]:
    """Pick (model, base_url, api_key) for this turn.

    Reads cfg.answering.generator as the default; ``override`` lets a
    request pin a specific model. Returns ``base_url=None`` when the
    config doesn't pin one — Hermes falls back to the OPENAI_BASE_URL
    env var or the provider default.
    """
    gen = getattr(getattr(state.cfg, "answering", None), "generator", None)
    if gen is None:
        # Defensive default — config schema guarantees this exists,
        # but keep the route from crashing on a stale-config test.
        return (override or "openai/gpt-4o-mini", None, None)

    model = override or gen.model
    base_url = gen.api_base or None

    # Resolve api_key: explicit value wins, then env var name, then
    # let Hermes / openai SDK fall back to OPENAI_API_KEY env.
    api_key: str | None = None
    if gen.api_key:
        api_key = gen.api_key
    elif gen.api_key_env:
        import os

        api_key = os.environ.get(gen.api_key_env)

    return (model, base_url, api_key)


def _load_conversation_history(state: AppState, conv_id: str) -> list[dict]:
    """Load prior user / assistant turns for an existing conversation
    so Hermes sees the context. Tool-call detail is NOT included —
    re-running the agent from text-only history is cheap enough
    (BM25 + vector hits cache via the embedding cache layer) and
    the schema's role column is restricted to user / assistant."""
    msgs: list[dict] = []
    rows = state.store.list_messages(conv_id)
    for row in rows or []:
        role = row.get("role") if isinstance(row, dict) else getattr(row, "role", None)
        content = (
            row.get("content") if isinstance(row, dict) else getattr(row, "content", None)
        )
        if role in ("user", "assistant") and isinstance(content, str) and content:
            msgs.append({"role": role, "content": content})
    return msgs


def _persist_user_message(state: AppState, conv_id: str, content: str) -> None:
    """Store the user turn before the SSE stream opens. Same rationale
    as legacy ``_persist_user_message``: a mid-stream refresh always
    recovers the question even if the answer never lands."""
    state.store.add_message(
        {
            "message_id": uuid.uuid4().hex,
            "conversation_id": conv_id,
            "role": "user",
            "content": content,
        }
    )


def _persist_assistant_message(
    state: AppState, conv_id: str, content: str
) -> None:
    """Always writes a row, even when ``content`` is empty. Empty =
    failed turn (LLM error / aborted) — the row's PRESENCE is what
    tells the frontend's poll loop to stop waiting."""
    state.store.add_message(
        {
            "message_id": uuid.uuid4().hex,
            "conversation_id": conv_id,
            "role": "assistant",
            "content": content,
        }
    )


def _persist_agent_run(
    state: AppState,
    *,
    run_id: str,
    conv_id: str | None,
    user_id: str,
    final_text: str,
    iterations: int,
    error: str | None,
    started_at: float,
    finished_at: float,
) -> None:
    """Write an ``agent_runs`` row recording this turn. Forward-compat
    hook for Phase C lineage: tool_call_log rows (Wave 3.5) reference
    this run_id, and artifacts produced during the run will too. For
    B-MVP we only write the row; the lineage queries that consume it
    ship later.

    Failures are logged + swallowed — a missing run row shouldn't
    fail the user-visible turn.
    """
    try:
        store = state.store
        if not hasattr(store, "add_agent_run"):
            log.debug(
                "agent_runs not persisted — store has no add_agent_run "
                "method (older schema or test stub)"
            )
            return
        store.add_agent_run(
            {
                "run_id": run_id,
                "conversation_id": conv_id,
                "user_id": user_id,
                "status": "error" if error else "ok",
                "final_text": final_text,
                "iterations": iterations,
                "error": error,
                "started_at": started_at,
                "finished_at": finished_at,
            }
        )
    except Exception:
        log.exception("hermes_chat: agent_run persist failed run_id=%s", run_id)


# ---------------------------------------------------------------------------
# SSE event translation: HermesRuntime events → wire format
# ---------------------------------------------------------------------------


def _sse(name: str, payload: dict) -> str:
    """One SSE block. We use the ``event:`` field plus a JSON
    ``data:`` so clients can switch on event type without parsing
    the JSON envelope first. Same shape the legacy route uses."""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _translate(evt: dict) -> str | None:
    """Map a HermesRuntime event dict to a single SSE block.

    Returns ``None`` for events we don't surface (none right now,
    but future event types might be debug-only).
    """
    kind = evt.get("kind")
    if kind == "thinking":
        return _sse("agent.thought", {"text": evt.get("text", "")})
    if kind == "answer_delta":
        return _sse("answer.delta", {"text": evt.get("text", "")})
    if kind == "tool_start":
        return _sse(
            "tool.call_start",
            {
                "id": evt.get("id", ""),
                "tool": evt.get("tool", ""),
                "params": evt.get("params", {}),
            },
        )
    if kind == "tool_end":
        return _sse(
            "tool.call_end",
            {
                "id": evt.get("id", ""),
                "tool": evt.get("tool", ""),
                "latency_ms": evt.get("latency_ms", 0),
                "result_summary": evt.get("result_summary", {}),
            },
        )
    if kind == "error":
        # Error events are folded into ``done`` at the outer layer
        # so the client only sees one terminal event. Returning
        # None here lets ``_run_stream`` capture the error and emit
        # a single ``done { stop_reason: "error" }`` at the end.
        return None
    if kind == "done":
        # Hermes' ``done`` is internal — we emit our own ``done``
        # at the SSE layer so it carries total_latency_ms + the
        # final assembled text.
        return None
    return None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/hermes-chat")
async def hermes_chat(
    body: HermesChatRequest,
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    """SSE stream of Hermes-driven agent events. See module docstring
    for the wire format."""

    # Resolve runtime config first so a misconfigured model fails
    # fast with a clean 400, not a mid-stream error.
    try:
        model, base_url, api_key = _resolve_model_config(state, body.model)
    except Exception as e:
        log.exception("hermes_chat: model config resolution failed")
        raise HTTPException(status_code=500, detail=str(e))

    # Load history + persist user message BEFORE the stream opens.
    # Idempotent for new conversations (history empty).
    history: list[dict] = []
    if body.conversation_id:
        try:
            history = _load_conversation_history(state, body.conversation_id)
        except Exception:
            log.exception(
                "hermes_chat: history load failed conv=%s", body.conversation_id
            )
            history = []

        try:
            _persist_user_message(state, body.conversation_id, body.query)
        except Exception:
            log.exception(
                "hermes_chat: user-message persist failed conv=%s",
                body.conversation_id,
            )

    config = HermesTurnConfig(
        model=model,
        base_url=base_url or "",  # empty = let openai SDK use its default
        api_key=api_key or "",
        max_iterations=90,
        system_message=body.system_prompt_override,
    )

    run_id = uuid.uuid4().hex
    started_at = time.time()

    async def _run_stream() -> AsyncIterator[bytes]:
        # Emit turn_start synchronously so the client sees activity
        # immediately even if Hermes' first network call is slow.
        yield _sse("agent.turn_start", {"turn": 1, "run_id": run_id}).encode("utf-8")

        runtime = HermesRuntime()
        final_text = ""
        error_message: str | None = None
        iterations = 0
        delta_buf: list[str] = []

        # ``stream_turn`` is a sync generator running on a worker
        # thread (see hermes_runtime.stream_turn). We pump events
        # through ``asyncio.to_thread`` to keep the FastAPI event
        # loop free for SSE flushes + concurrent connections.
        try:
            iter_ = stream_turn(
                runtime,
                body.query,
                config=config,
                conversation_history=history,
            )
        except HermesUnavailableError as e:
            error_message = f"hermes-agent not installed: {e}"
            yield _emit_done(
                error_message=error_message,
                final_text="",
                started_at=started_at,
                run_id=run_id,
            )
            _persist_agent_run(
                state,
                run_id=run_id,
                conv_id=body.conversation_id,
                user_id=principal.user_id,
                final_text="",
                iterations=0,
                error=error_message,
                started_at=started_at,
                finished_at=time.time(),
            )
            return

        # Pump the sync iterator on a thread so we can ``await``
        # between events without blocking the event loop.
        loop = asyncio.get_event_loop()
        sentinel = object()

        def _next_or_sentinel(it):
            try:
                return next(it)
            except StopIteration:
                return sentinel

        try:
            while True:
                evt = await loop.run_in_executor(None, _next_or_sentinel, iter_)
                if evt is sentinel:
                    break
                if not isinstance(evt, dict):
                    continue
                if evt.get("kind") == "answer_delta":
                    delta_buf.append(evt.get("text", ""))
                elif evt.get("kind") == "error":
                    error_message = (
                        f"{evt.get('type', 'RuntimeError')}: "
                        f"{evt.get('message', 'agent failed')}"
                    )
                elif evt.get("kind") == "done":
                    iterations = int(evt.get("iterations") or 0)
                    # If Hermes' final dict already had a final_text
                    # (extracted by the runtime), prefer that —
                    # delta_buf may have lost partial chunks.
                    ft = evt.get("final_text") or ""
                    if ft:
                        final_text = ft
                line = _translate(evt)
                if line:
                    yield line.encode("utf-8")
        except Exception:
            log.exception("hermes_chat: stream pump raised")
            error_message = error_message or "agent failed: stream pump"

        # ``final_text`` falls back to the assembled deltas if the
        # ``done`` event didn't carry one.
        if not final_text:
            final_text = "".join(delta_buf)

        yield _sse("agent.turn_end", {"turn": 1, "run_id": run_id}).encode("utf-8")
        yield _emit_done(
            error_message=error_message,
            final_text=final_text,
            started_at=started_at,
            run_id=run_id,
        )

        # Post-stream persistence — never block the response on this
        if body.conversation_id:
            try:
                _persist_assistant_message(
                    state, body.conversation_id, final_text
                )
            except Exception:
                log.exception(
                    "hermes_chat: assistant persist failed conv=%s",
                    body.conversation_id,
                )

        _persist_agent_run(
            state,
            run_id=run_id,
            conv_id=body.conversation_id,
            user_id=principal.user_id,
            final_text=final_text,
            iterations=iterations,
            error=error_message,
            started_at=started_at,
            finished_at=time.time(),
        )

    return StreamingResponse(
        _run_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _emit_done(
    *,
    error_message: str | None,
    final_text: str,
    started_at: float,
    run_id: str,
) -> bytes:
    """Single ``done`` SSE block — always the final event the client
    sees, in success or error case."""
    payload: dict[str, Any] = {
        "stop_reason": "error" if error_message else "end_turn",
        "total_latency_ms": int((time.time() - started_at) * 1000),
        "final_text": final_text,
        "run_id": run_id,
    }
    if error_message:
        payload["error"] = error_message
    return _sse("done", payload).encode("utf-8")
