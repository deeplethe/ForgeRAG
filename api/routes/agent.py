"""
/api/v1/agent — agentic chat with live SSE event feedback.

    POST /api/v1/agent/chat       SSE stream of agent loop events

The chat endpoint replaces the fixed-pipeline ``/api/v1/query``
route as the primary chat surface. Old route kept around for the
benchmark + eval harness during the rewrite; will be deleted in
step 7 once the agent path proves out.

Wire format:

    Content-Type: text/event-stream
    Cache-Control: no-cache
    X-Accel-Buffering: no       (nginx-friendly)

Each event is one ``data: <json>\\n\\n`` block. The client receives
events in order:

    agent.turn_start   { turn, synthesis_only? }
    tool.call_start    { id, tool, params }
    tool.call_end      { id, tool, latency_ms, result_summary }
        (parallel tool calls land in completion order, not
        submission order — fast BM25 lands before slow vector)
    agent.turn_end     { turn, tools_called, decision }
    answer             { text }
    done               { stop_reason, citations, total_latency_ms,
                         tokens_in, tokens_out, ... }

The ``done`` event is always the last one — clients close the
stream after receiving it.

Authz: ``Depends(get_principal)`` enforces session/SK auth.
``build_tool_context`` runs ``AuthorizationService.resolve_paths``
+ ``build_accessible_set`` once per request — every tool inside
the loop inherits scope filtering for free.

Errors:
    * ``UnauthorizedPath`` from explicit path_filters → 403
    * Anything else inside the loop → caught + emitted as a final
      ``done { stop_reason: "error" }`` event. The client sees the
      stream finish gracefully rather than a half-open connection.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent import (
    AgentConfig,
    AgentLoop,
    LiteLLMClient,
    build_tool_context,
)
from ..auth import AuthenticatedPrincipal, UnauthorizedPath
from ..deps import get_principal, get_state
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------


class _HistoryMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str


class AgentChatRequest(BaseModel):
    """Body of POST /api/v1/agent/chat.

    ``message`` is the new user turn. ``history`` is prior turns
    (user/assistant only — tool_result messages from prior turns
    are NOT replayed; the citation pool is per-query).

    ``path_filters`` optionally narrows the agent's accessible
    folder scope below the user's full grant — same semantics as
    the old ``/api/v1/query`` body's ``path_filters``. ``None``
    falls back to the user's full accessible set.
    """

    message: str = Field(..., min_length=1, max_length=8192)
    history: list[_HistoryMessage] = Field(default_factory=list)
    path_filters: list[str] | None = None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/chat")
def agent_chat(
    body: AgentChatRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
) -> StreamingResponse:
    """SSE stream of agent loop events. See module docstring for
    the wire format + event vocabulary.
    """
    # Resolve scope synchronously — UnauthorizedPath surfaces as 403
    # BEFORE we open the stream so the client gets a clean HTTP error
    # instead of a malformed event stream.
    try:
        ctx = build_tool_context(
            state,
            principal,
            requested_path_filters=body.path_filters,
        )
    except UnauthorizedPath as e:
        raise HTTPException(
            status_code=403,
            detail={"error": "unauthorized_path", "path": e.path},
        ) from e

    cfg = _agent_config_for(state)
    llm = _llm_client_for(cfg)
    loop = AgentLoop(cfg, llm)

    history = [{"role": h.role, "content": h.content} for h in body.history]

    def _events() -> Iterator[bytes]:
        try:
            for evt in loop.stream(body.message, ctx, history=history):
                yield _sse_chunk(evt)
        except Exception:
            # Catch-all so the stream always terminates with a
            # ``done`` event. Without this a tool-layer bug would
            # leave the client hanging on a half-open connection.
            log.exception("agent stream raised")
            yield _sse_chunk(
                {"type": "done", "stop_reason": "error", "answer": ""}
            )

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tells nginx / corporate proxies not to buffer — the
            # whole point of SSE is incremental delivery.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_chunk(event: dict) -> bytes:
    """Encode one event as an SSE ``data:`` block.

    SSE wire format is ``data: <text>\\n\\n``. Multi-line payloads
    need each line prefixed; we serialise as a single-line JSON to
    keep that case out of the hot path.
    """
    payload = json.dumps(event, ensure_ascii=False, default=str)
    return f"data: {payload}\n\n".encode()


def _agent_config_for(state: AppState) -> AgentConfig:
    """Build an AgentConfig from app state.

    For now we read defaults from ``AgentConfig()`` plus the
    answering model + API key already configured on AppState.
    A dedicated ``config/agent.py`` config block lands when we
    want per-deployment knob overrides; v1 ships with sane
    defaults baked in.
    """
    cfg = AgentConfig()
    # Inherit the answering model + key when configured — saves
    # operators from setting a second key. ``answering`` may not be
    # wired (single-user dev), so be defensive.
    answering = getattr(state.cfg, "answering", None) if hasattr(state, "cfg") else None
    if answering is not None:
        model = getattr(answering, "model", None)
        if model:
            cfg.model = model
        # api_key may be on the cfg or env-resolved; agent will read
        # ANTHROPIC_API_KEY etc. from env when not explicit.
        cfg.api_key = getattr(answering, "api_key", None)
        cfg.api_base = getattr(answering, "api_base", None)
    return cfg


def _llm_client_for(cfg: AgentConfig) -> LiteLLMClient:
    """LiteLLMClient with the cfg's model + key.

    Env var fallback: when ``cfg.api_key`` is None we let litellm
    discover ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / etc. from
    env on its own — no extra plumbing needed.
    """
    return LiteLLMClient(
        model=cfg.model,
        api_key=cfg.api_key or os.environ.get("ANTHROPIC_API_KEY"),
        api_base=cfg.api_base,
    )
