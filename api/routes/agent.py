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
    agent.thought      { turn, text }
        (only on DSML-fallback turns where deltas were suppressed —
        normally the model's preface streams as ``answer.delta``)
    tool.call_start    { id, tool, params }
    tool.call_end      { id, tool, latency_ms, result_summary }
        (parallel tool calls land in completion order, not
        submission order — fast BM25 lands before slow vector)
    agent.turn_end     { turn, tools_called, decision }
    answer.delta       { text }
        (token-by-token stream of the model's content; for
        tool-decision turns this is the preface text, for
        direct-answer turns it's the final answer)
    answer             { text }
        (final aggregated text — same content as the deltas;
        non-streaming consumers read this)
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
        # Build the agent reasoning trace from the events we
        # forward to the client. Persisted onto the assistant
        # message so a page refresh keeps the chain visible —
        # without this, reload shows just the answer body and the
        # "Thought for Xs · N tools" header / row breakdown all
        # disappear (the trace was previously frontend-only state).
        trace: list[dict] = []
        # Per-turn token totals — captured from the loop's final
        # ``done`` event (see api/agent/loop.py). Persisted onto the
        # assistant message so per-user usage aggregation can SUM
        # over messages without re-parsing the trace blob.
        tokens_in: int = 0
        tokens_out: int = 0
        try:
            for evt in loop.stream(body.message, ctx, history=history):
                kind = evt.get("type")
                if kind == "answer":
                    final_answer = evt.get("text") or final_answer
                elif kind == "done":
                    tokens_in = int(evt.get("tokens_in") or 0)
                    tokens_out = int(evt.get("tokens_out") or 0)
                _accumulate_trace(trace, evt)
                yield _sse_chunk(evt)
        except Exception as outer_e:
            # Catch-all so the stream always terminates with a
            # ``done`` event. Without this a tool-layer bug would
            # leave the client hanging on a half-open connection.
            # Include the exception message on the event so the
            # UI can render a red error bubble instead of a silent
            # empty assistant message.
            log.exception("agent stream raised")
            from ..agent.loop import _format_user_error

            yield _sse_chunk(
                {
                    "type": "done",
                    "stop_reason": "error",
                    "answer": "",
                    "error": _format_user_error(outer_e),
                }
            )
        # ── Persist the new turn AFTER the stream finishes ──
        # Keeps the SSE round-trip as fast as possible — DB write
        # happens after the client has the answer in hand.
        if conv_id is not None:
            try:
                _persist_turn(
                    state, conv_id, body.message, final_answer, ctx, trace,
                    tokens_in=tokens_in, tokens_out=tokens_out,
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

    Inherits the answering generator's model + key + api_base —
    same configured LLM the (now-deleted) AnsweringPipeline used.
    Saves operators from filling in a second key for the agent.

    The model + key live at ``cfg.answering.generator.{model,
    api_key, api_base}`` — NOT directly on ``cfg.answering``
    (that was the v1 wiring bug that surfaced as "x-api-key
    header is required" against Anthropic when the agent fell
    through to the AgentConfig default model
    ``anthropic/claude-sonnet-4-5`` with no key).
    """
    cfg = AgentConfig()
    answering = getattr(state.cfg, "answering", None) if hasattr(state, "cfg") else None
    gen = getattr(answering, "generator", None) if answering is not None else None
    if gen is not None:
        model = getattr(gen, "model", None)
        if model:
            cfg.model = model
        cfg.api_key = getattr(gen, "api_key", None)
        cfg.api_base = getattr(gen, "api_base", None)
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


def _accumulate_trace(trace: list[dict], evt: dict) -> None:
    """Build the agent reasoning chain from SSE events, mirroring
    the Vue frontend's logic in ``Chat.vue`` so a refreshed page
    sees the same shape it would build live.

    Trace entry shapes (matches frontend's ``streamTrace``):
      * {kind: 'phase', phase, text, status: 'done'} — bare or
        thought (text populated when the model narrated before
        tool calls or via ``agent.thought`` event).
      * {kind: 'tool', call_id, name, detail, summary,
         elapsedMs, status: 'done'}
    Phase entries carry ``elapsedSec``, tools carry ``elapsedMs``;
    we don't have wall-clock here (events arrive in real time
    but we don't track t0 per entry server-side) so timing stays
    at zero. The summary header on the persisted side just shows
    "Reasoned · N tools" without a duration; live mode still
    has the full timer experience.

    The frontend's ``tool.call_start`` handler folds streamed
    preface text into the trailing phase entry — we mirror that
    by using ``last_phase_text_buffer`` to track what would
    have been the streamed preface.
    """
    kind = evt.get("type")
    if kind == "agent.turn_start":
        trace.append({
            "kind": "phase",
            "phase": "composing" if evt.get("synthesis_only") else (
                "planning" if not any(e["kind"] == "phase" for e in trace) else "reviewing"
            ),
            "text": "",
            "elapsedSec": 0,
            "status": "running",
        })
    elif kind == "agent.thought":
        # Find the trailing running phase, attach text.
        for e in reversed(trace):
            if e["kind"] == "phase" and e.get("status") == "running":
                e["text"] = evt.get("text") or ""
                break
    elif kind == "tool.call_start":
        # Mark trailing phase done.
        for e in reversed(trace):
            if e["kind"] == "phase" and e.get("status") == "running":
                e["status"] = "done"
                break
        params = evt.get("params") or {}
        detail = params.get("query") or params.get("chunk_id") or params.get("doc_id") or ""
        if not isinstance(detail, str):
            detail = str(detail)
        trace.append({
            "kind": "tool",
            "call_id": evt.get("id"),
            "name": evt.get("tool"),
            "detail": detail[:64],
            "summary": "",
            "elapsedMs": 0,
            "status": "running",
        })
    elif kind == "tool.call_end":
        cid = evt.get("id")
        summary = evt.get("result_summary") or {}
        sum_text = (
            f"{summary.get('hit_count')} hits" if summary.get("hit_count") is not None
            else f"{summary.get('entity_count')} entities" if summary.get("entity_count") is not None
            else f"{summary.get('chunk_count')} chunks" if summary.get("chunk_count") is not None
            else "error" if summary.get("error")
            else ""
        )
        for e in trace:
            if e["kind"] == "tool" and e.get("call_id") == cid:
                e["status"] = "done"
                e["summary"] = sum_text
                e["elapsedMs"] = evt.get("latency_ms") or 0
                break
    elif kind == "agent.turn_end":
        # Mark any remaining running phase done.
        for e in reversed(trace):
            if e["kind"] == "phase" and e.get("status") == "running":
                e["status"] = "done"
                break


def _persist_turn(
    state: AppState,
    conv_id: str,
    user_message: str,
    assistant_answer: str,
    ctx,
    trace: list[dict] | None = None,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    """Write the new user message + final assistant answer to the
    DB. Citations are serialised onto the assistant row's
    ``citations_json`` so the conversation viewer can render them
    later.

    Token counts (``tokens_in`` / ``tokens_out``) come from the
    agent loop's final ``done`` event and land on the ASSISTANT
    row only — the user row stays at 0/0. Per-user usage views sum
    over messages → conversations → user_id.

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
    # Enrichment ran already during the SSE response — pool entries
    # carry the highlights / file_id / source_file_id / source_format
    # fields (see api/agent/dispatch.py::enrich_citations). Persist
    # them so reloaded conversations open the PDF preview on the
    # right page with the right rectangles, no second-pass needed.
    cits = []
    for c in (getattr(ctx, "citation_pool", {}) or {}).values():
        cits.append(
            {
                # ``cite_id`` is what the inline ``[c_N]`` markers in
                # the answer reference — must round-trip to DB so a
                # reloaded conversation still wires markers back to
                # citation chips. Without this, reloads show inline
                # markers but the rail can't match them.
                "cite_id": c.get("cite_id"),
                "chunk_id": c.get("chunk_id"),
                "doc_id": c.get("doc_id"),
                "page_start": c.get("page_start"),
                "page_end": c.get("page_end"),
                "score": c.get("score"),
                # Preview rendering payload — populated by
                # enrich_citations, possibly empty on legacy rows.
                "file_id": c.get("file_id"),
                "source_file_id": c.get("source_file_id"),
                "source_format": c.get("source_format"),
                "highlights": c.get("highlights") or [],
            }
        )
    # Filter the trace to entries with actual content. Bare phases
    # (``status='done'`` but no text) carry no information once the
    # live timer is gone, but we keep them anyway because the
    # frontend renders them as italic "pause beats" between actions
    # and dropping them would make the persisted chain feel
    # truncated. Tools always retain their detail + summary so
    # "Read 8 passages" / "Semantic search '…'" survive reload.
    persisted_trace = list(trace) if trace else None
    state.store.add_message(
        {
            "message_id": uuid4().hex,
            "conversation_id": conv_id,
            "role": "assistant",
            "content": assistant_answer,
            "citations_json": cits or None,
            "agent_trace_json": persisted_trace,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
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
