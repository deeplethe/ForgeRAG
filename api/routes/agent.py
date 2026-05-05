"""
/api/v1/agent — agentic chat with live SSE event feedback.

    POST /api/v1/agent/chat       SSE stream of agent loop events

The chat endpoint REPLACES the fixed-pipeline ``/api/v1/query``
route as the primary chat surface — old route deleted post-
benchmark since agent path won decisively (54% latency drop, 220%
CP improvement on a 9-Q agriculture testset).

Conversation persistence: when the request body carries
``conversation_id``, the route loads prior user/assistant text
turns from the ``messages`` table, injects them as ``history``,
runs the agent loop, then writes the new user turn + final
answer back. Tool-call traces are NOT persisted — the schema's
``role`` column is restricted to ``user``/``assistant`` and
re-running the agent on next turn is cheaper than the schema
migration to add tool_use rows. If the user references prior
results ("the paper you found"), the agent re-searches; this
trade was validated in v2 benchmark (low repeat-cost since
BM25/vector hits cache via the embedding cache layer).

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
from uuid import uuid4

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

    ``message`` is the new user turn.

    ``conversation_id`` (preferred) — when set, the server loads
    prior user/assistant turns from the messages table and injects
    them as the agent's conversation history. The client doesn't
    have to track history. A new conversation is auto-created if
    the id doesn't exist yet (with the caller as ``user_id``).

    ``history`` (legacy / stateless) — when ``conversation_id`` is
    not set, the client may pass an explicit history array. Useful
    for one-off queries or tests that don't want a DB row.

    ``path_filters`` optionally narrows the agent's accessible
    folder scope below the user's full grant. ``None`` falls back
    to the user's full accessible set.
    """

    message: str = Field(..., min_length=1, max_length=8192)
    conversation_id: str | None = None
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

    # ── Conversation persistence ──
    # When a conversation_id is supplied, load prior messages and
    # use them as the agent's history. Otherwise use the explicit
    # history array (or no history at all).
    conv_id = body.conversation_id
    if conv_id is not None:
        history = _load_or_create_conversation(state, principal, conv_id, body.message)
    else:
        history = [{"role": h.role, "content": h.content} for h in body.history]

    def _events() -> Iterator[bytes]:
        # Capture the final answer so we can persist it after the
        # stream closes. Generator-local var; SSE downstream still
        # gets the answer event in real time.
        final_answer: str = ""
        try:
            for evt in loop.stream(body.message, ctx, history=history):
                if evt.get("type") == "answer":
                    final_answer = evt.get("text") or final_answer
                yield _sse_chunk(evt)
        except Exception:
            # Catch-all so the stream always terminates with a
            # ``done`` event. Without this a tool-layer bug would
            # leave the client hanging on a half-open connection.
            log.exception("agent stream raised")
            yield _sse_chunk(
                {"type": "done", "stop_reason": "error", "answer": ""}
            )
        # ── Persist the new turn AFTER the stream finishes ──
        # Keeps the SSE round-trip as fast as possible — DB write
        # happens after the client has the answer in hand.
        if conv_id is not None:
            try:
                _persist_turn(
                    state, conv_id, body.message, final_answer, ctx
                )
            except Exception:
                log.exception("agent_chat: conversation persist failed")

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


# ---------------------------------------------------------------------------
# Conversation persistence
# ---------------------------------------------------------------------------


def _load_or_create_conversation(
    state: AppState,
    principal: AuthenticatedPrincipal,
    conv_id: str,
    new_user_message: str,
) -> list[dict]:
    """Resolve the conversation row + return its message history as
    OpenAI-style messages for the agent loop.

    * Existing conversation owned by another user → 404. Per S5.2,
      conversations are private; admin role does NOT bypass.
    * Existing conversation owned by caller → load messages, return
      the user/assistant text turns as history.
    * Missing conversation → auto-create it with caller as
      ``user_id`` and a title derived from the first user message.

    The new user message is NOT appended to the returned history —
    ``AgentLoop.stream`` adds it itself. Persistence of the new
    turn (user + assistant) happens AFTER the stream closes via
    ``_persist_turn``.
    """
    owner = _effective_owner(state, principal)
    existing = state.store.get_conversation(conv_id)
    if existing is not None:
        # Privacy gate — admin does NOT bypass for conversations.
        row_user = existing.get("user_id")
        if owner is not None and row_user != owner:
            # Legacy auth-disabled rows have user_id=None; allow
            # those to pass for the synthetic local-admin caller
            # (preserves single-user dev history).
            if not (row_user is None and owner == "local"):
                raise HTTPException(404, "conversation not found")
        prior = state.store.get_messages(conv_id, limit=50) or []
        return [
            {"role": m["role"], "content": m["content"]}
            for m in prior
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]

    # Not found → create with caller as owner. ``user_id=None`` is
    # legitimate when auth is disabled (the synthetic ``local``
    # principal lands here).
    state.store.create_conversation(
        {
            "conversation_id": conv_id,
            "title": new_user_message[:100],
            "user_id": owner,
        }
    )
    return []


def _persist_turn(
    state: AppState,
    conv_id: str,
    user_message: str,
    assistant_answer: str,
    ctx,
) -> None:
    """Write the new user message + final assistant answer to the
    DB. Citations are serialised onto the assistant row's
    ``citations_json`` so the conversation viewer can render them
    later.

    Idempotent against partial failure: each ``add_message`` is its
    own transaction so a crash between the two writes doesn't lose
    the user message.
    """
    state.store.add_message(
        {
            "message_id": uuid4().hex,
            "conversation_id": conv_id,
            "role": "user",
            "content": user_message,
        }
    )
    if not assistant_answer:
        return
    # Compact citation snapshot — frontend's "click citation → open
    # PDF" flow needs chunk_id + doc_id + page; full content lives
    # in the chunks table.
    cits = []
    for c in (getattr(ctx, "citation_pool", {}) or {}).values():
        cits.append(
            {
                "chunk_id": c.get("chunk_id"),
                "doc_id": c.get("doc_id"),
                "page_start": c.get("page_start"),
                "page_end": c.get("page_end"),
                "score": c.get("score"),
            }
        )
    state.store.add_message(
        {
            "message_id": uuid4().hex,
            "conversation_id": conv_id,
            "role": "assistant",
            "content": assistant_answer,
            "citations_json": cits or None,
        }
    )


def _effective_owner(
    state: AppState, principal: AuthenticatedPrincipal
) -> str | None:
    """Resolve the user_id we'll record on the conversation row.

    Auth-disabled deploys use the synthetic ``"local"`` sentinel
    so single-user history pre-multi-user remains queryable. Auth
    enabled → caller's real user_id.
    """
    if not state.cfg.auth.enabled or principal.via == "auth_disabled":
        return "local"
    return principal.user_id


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
