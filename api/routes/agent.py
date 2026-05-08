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
import queue
import threading
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
    # If the request carries a conversation_id, resolve any project
    # binding BEFORE we build the tool context — ``import_from_library``
    # needs ``ctx.project_id``, and the system-prompt augmentation
    # uses the same project_id elsewhere.
    bound_project_id: str | None = None
    if body.conversation_id is not None:
        try:
            conv_row = state.store.get_conversation(body.conversation_id)
            if conv_row is not None:
                bound_project_id = conv_row.get("project_id")
        except Exception:
            log.exception(
                "agent: failed to resolve conversation project_id; "
                "falling back to plain Q&A binding"
            )

    # Resolve scope synchronously — UnauthorizedPath surfaces as 403
    # BEFORE we open the stream so the client gets a clean HTTP error
    # instead of a malformed event stream.
    try:
        ctx = build_tool_context(
            state,
            principal,
            requested_path_filters=body.path_filters,
            project_id=bound_project_id,
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
        history, prior_citations = _load_or_create_conversation(
            state, principal, conv_id, body.message
        )
        # Seed the citation pool with citations from earlier
        # assistant turns. Without this, follow-up answers that
        # reuse [c_N] markers from prior tool calls (because the
        # LLM has the answer in context and skips fresh
        # retrieval) emit raw text — the frontend has no
        # cite_id → chunk_id mapping for those markers in the
        # current turn's persisted ``citations_json``.
        # ``register_chunk`` is idempotent on chunk_id, so any
        # tool call this turn that re-encounters a seeded chunk
        # merges into the existing entry (keeping the original
        # cite_id stable). New chunks discovered THIS turn pick
        # up cite_ids starting at ``c_{len(pool) + 1}``, so
        # numbering doesn't collide with the seeded set.
        for c in prior_citations:
            cid = c.get("chunk_id")
            if cid and cid not in ctx.citation_pool:
                ctx.citation_pool[cid] = dict(c)
        # Persist the user message NOW, before the SSE stream
        # opens. A mid-stream refresh otherwise loses the question
        # entirely (the post-stream persist block doesn't run if
        # the client disconnects). With the user row already in
        # DB, the reloaded Chat.vue sees a trailing user message
        # and switches into poll-for-assistant mode while the
        # backend keeps generating. If THIS write fails we fail
        # the request — there's no point streaming an answer that
        # can never be linked back to a question.
        try:
            _persist_user_message(state, conv_id, body.message)
        except Exception:
            log.exception("agent_chat: user message persist failed")
            raise HTTPException(
                500, "failed to persist user message — try again"
            )
    else:
        history = [{"role": h.role, "content": h.content} for h in body.history]

    # ── Decoupled agent execution ────────────────────────────────
    # The agent runs in a background thread and pushes events into
    # an in-memory queue. The SSE generator is a pure observer:
    # it drains the queue and yields. This makes the agent's run
    # lifecycle independent of the SSE connection — which matters
    # for two reasons:
    #
    #   1. Mid-stream refresh / navigate-away no longer aborts the
    #      run. The thread keeps going to completion and persists
    #      the assistant message. Reloading the page hits the poll
    #      path and picks the answer up. Without this, refreshing
    #      while the agent was still calling the LLM left a
    #      perpetual "thinking" state.
    #
    #   2. Future deep-research mode will run for minutes, well
    #      beyond a typical SSE keep-alive horizon. A pure-observer
    #      SSE means the user can close the tab and come back later
    #      to see results.
    #
    # Per-request thread (no pool): agent runs are bursty and
    # usually quick; the cost of starting a thread per request is
    # noise compared to the LLM round-trips inside. Thread is
    # daemon so it doesn't block process shutdown — if the server
    # exits mid-run, the assistant row stays empty (next user
    # message triggers a fresh attempt; the orphaned user row is
    # harmless context).

    # Sentinel pushed by the worker after the final event so the
    # observer knows to stop reading. Module-level so the queue
    # value is identity-comparable (vs accidentally matching a
    # legitimate string event).
    EOS = object()

    event_q: queue.Queue = queue.Queue()
    final_answer = [""]
    trace: list[dict] = []
    tokens_in_box = [0]
    tokens_out_box = [0]

    # Phase 1.6: when the conversation is bound to a project, augment
    # the agent's system prompt with project context (name + workdir
    # file list). Plain unbound chats fall through with system_prompt=
    # None so the base SYSTEM_PROMPT is used unchanged.
    system_prompt: str | None = None
    if conv_id is not None:
        try:
            system_prompt = _build_system_prompt_for_conversation(
                state, principal, conv_id
            )
        except Exception:
            log.exception("agent: project-context system prompt failed")

    def _worker() -> None:
        try:
            for evt in loop.stream(
                body.message, ctx, history=history, system_prompt=system_prompt
            ):
                kind = evt.get("type")
                if kind == "answer":
                    final_answer[0] = evt.get("text") or final_answer[0]
                elif kind == "done":
                    tokens_in_box[0] = int(evt.get("tokens_in") or 0)
                    tokens_out_box[0] = int(evt.get("tokens_out") or 0)
                _accumulate_trace(trace, evt)
                event_q.put(evt)
        except Exception as outer_e:
            # Surface the failure to the SSE consumer (if still
            # connected) AND make sure the trace records it for
            # any post-disconnect refresh. Never let an exception
            # escape — finally below MUST run to persist.
            log.exception("agent worker raised")
            from ..agent.loop import _format_user_error

            err_evt = {
                "type": "done",
                "stop_reason": "error",
                "answer": "",
                "error": _format_user_error(outer_e),
            }
            try:
                _accumulate_trace(trace, err_evt)
            except Exception:
                pass
            try:
                event_q.put(err_evt)
            except Exception:
                pass
        finally:
            # ── Persist the assistant reply ──
            # Runs regardless of stream success / failure / SSE
            # disconnect. The user message was already saved
            # before the worker started; persisting (even an
            # empty) assistant row terminates the frontend's poll
            # loop on reload.
            if conv_id is not None:
                try:
                    _persist_assistant_message(
                        state, conv_id,
                        final_answer[0], ctx, trace,
                        tokens_in=tokens_in_box[0],
                        tokens_out=tokens_out_box[0],
                    )
                except Exception:
                    log.exception("agent_chat: assistant message persist failed")
            # Tell the SSE observer (if still attached) to wrap up.
            try:
                event_q.put(EOS)
            except Exception:
                pass

    threading.Thread(target=_worker, name="agent-run", daemon=True).start()

    def _events() -> Iterator[bytes]:
        """SSE observer — reads from the worker's event queue and
        forwards. Disconnect just ends iteration; the worker keeps
        running in the background and persists when done."""
        try:
            while True:
                # ``timeout`` keeps the consumer responsive to
                # disconnect without hot-spinning. 30s is long
                # enough that a slow LLM call between events
                # doesn't trip a spurious wakeup.
                try:
                    evt = event_q.get(timeout=30.0)
                except queue.Empty:
                    # Heartbeat to keep proxies + the browser
                    # connection alive across long idle gaps. Just
                    # an SSE comment — clients ignore it.
                    yield b": keepalive\n\n"
                    continue
                if evt is EOS:
                    return
                yield _sse_chunk(evt)
        except GeneratorExit:
            # Client closed the SSE (refresh / nav). DON'T raise
            # back into the worker — the worker is in its own
            # thread and will finish the run + persist on its own.
            # Just log and exit the generator cleanly.
            log.info("agent SSE observer disconnected; worker continues")
            raise

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
# Project-context system prompt (Phase 1.6)
# ---------------------------------------------------------------------------


# How many workdir files to enumerate inline. Past this we summarise
# ("…plus N more") to keep the prompt token budget reasonable. The
# top-level workdir typically has <30 entries; the per-doc-flat
# enumeration is for users who genuinely have hundreds of files.
_WORKDIR_FILE_LIMIT = 30


def _build_system_prompt_for_conversation(
    state: AppState,
    principal: AuthenticatedPrincipal,
    conv_id: str,
) -> str | None:
    """Return the augmented system prompt for a conversation, or
    None when the conversation is unbound / inaccessible.

    Loads:
      * Conversation row → project_id (None means unbound; return None)
      * Project row → name + description
      * Project workdir top-level listing (paths + sizes) — up to
        ``_WORKDIR_FILE_LIMIT`` entries
    Constructs the project-context block via prompts.build_system_prompt.

    Failure modes (missing project, unreadable workdir, listing
    error) all degrade to "no project context" — agent falls back to
    base SYSTEM_PROMPT cleanly.
    """
    from pathlib import Path

    from persistence.project_file_service import ProjectFileService
    from persistence.project_service import ProjectNotFound, ProjectService

    from ..agent.prompts import build_system_prompt

    conv = state.store.get_conversation(conv_id)
    if not conv:
        return None
    project_id = conv.get("project_id")
    if not project_id:
        return None

    is_admin = (
        not state.cfg.auth.enabled
        or principal.role == "admin"
        or principal.via == "auth_disabled"
    )
    projects_root = Path(
        getattr(state.cfg.agent, "projects_root", "./storage/projects")
    )

    with state.store.transaction() as sess:
        psvc = ProjectService(
            sess,
            projects_root=projects_root,
            actor_id=principal.user_id,
        )
        try:
            proj = psvc.require(project_id)
        except ProjectNotFound:
            return None
        if not psvc.can_access(
            proj, principal.user_id, "read", is_admin=is_admin
        ):
            return None

        # List the conventional subdirs (inputs / outputs / scratch)
        # and their immediate contents — enough for the agent to know
        # what files exist without a full recursive walk.
        fsvc = ProjectFileService(
            sess,
            project=proj,
            projects_root=projects_root,
            actor_id=principal.user_id,
        )
        listed_files: list[tuple[str, int]] = []  # (rel_path, size_bytes)
        for subdir in ("inputs", "outputs", "scratch"):
            try:
                entries = fsvc.list(subdir)
            except Exception:
                continue
            for e in entries:
                if e.is_dir:
                    continue
                listed_files.append((e.path, e.size_bytes))
                if len(listed_files) >= _WORKDIR_FILE_LIMIT:
                    break
            if len(listed_files) >= _WORKDIR_FILE_LIMIT:
                break

    block = _format_project_block(
        name=proj.name,
        description=proj.description,
        files=listed_files,
        truncated=len(listed_files) >= _WORKDIR_FILE_LIMIT,
    )
    return build_system_prompt(project_context=block)


def _format_project_block(
    *,
    name: str,
    description: str | None,
    files: list[tuple[str, int]],
    truncated: bool,
) -> str:
    """Render the project context block for the system prompt.

    Format is deliberately plain text — no markdown headers (those
    can confuse some LLMs into copying them into answers); just
    short labelled lines that read naturally as instructions. The
    Phase-2 caveat is explicit so the agent doesn't hallucinate
    file-IO tools it doesn't have yet.
    """
    lines: list[str] = ["PROJECT CONTEXT:"]
    lines.append(f"You are working in the user's project \"{name}\".")
    if description:
        lines.append(f"Project description: {description}")
    if not files:
        lines.append("The project workdir is currently empty.")
    else:
        lines.append("Project workdir contains:")
        for path, size in files:
            lines.append(f"  - {path} ({_fmt_size(size)})")
        if truncated:
            lines.append(
                f"  …plus more files (showing first {_WORKDIR_FILE_LIMIT})."
            )
    lines.append("")
    lines.append(
        "IMPORTANT — Phase 1: you can already retrieve from the Library "
        "(search_vector / read_chunk / graph_explore / read_tree as usual). "
        "You CANNOT yet directly read these project workdir files, run code "
        "against them, or create new ones — code execution ships in a "
        "follow-up phase via the in-container agent runtime. The "
        "``import_from_library`` tool is available now if you need to copy "
        "a Library document into this project's inputs/. If the user asks "
        "you to operate on a workdir file, explain that the agent can see "
        "the file exists but can't yet open or process it; offer to "
        "retrieve relevant Library content instead."
    )
    return "\n".join(lines)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


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
    ``AgentLoop.stream`` adds it itself. Persistence is split:
    ``_persist_user_message`` runs synchronously right after this
    helper returns (so a mid-stream refresh recovers the
    question), and ``_persist_assistant_message`` lands the reply
    after the SSE stream closes.

    Returns a ``(history, prior_citations)`` pair. ``prior_citations``
    is a deduped list of citation entries collected from every
    earlier assistant turn — the caller seeds them into
    ``ctx.citation_pool`` so that when the LLM answers from
    context (no fresh tool calls) and references ``[c_14]`` from
    turn 1, the new turn's persisted ``citations_json`` still
    carries that entry → frontend renders the marker as a
    clickable chip instead of a raw token.
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
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in prior
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        # Walk every prior assistant message in chronological
        # order, dedup by chunk_id (the FIRST occurrence wins so
        # the LLM-visible cite_id stays stable across turns).
        prior_citations: list[dict] = []
        seen_chunks: set[str] = set()
        for m in prior:
            if m.get("role") != "assistant":
                continue
            for c in (m.get("citations_json") or []):
                cid = c.get("chunk_id")
                if cid and cid not in seen_chunks:
                    seen_chunks.add(cid)
                    prior_citations.append(c)
        return history, prior_citations

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
    return [], []


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


def _persist_user_message(
    state: AppState,
    conv_id: str,
    user_message: str,
) -> None:
    """Write the user's question to the DB.

    Called BEFORE the SSE stream opens so a mid-stream refresh
    always recovers at least the question. The frontend's reload
    path (``Chat.vue::_loadAndPoll``) sees a trailing user
    message with no assistant reply and switches into poll mode
    — the in-flight turn finishes server-side, _persist_assistant_
    message lands the answer, polling picks it up. Without this
    early write, mid-stream refresh shows an empty conversation
    and the user thinks the message vanished.
    """
    state.store.add_message(
        {
            "message_id": uuid4().hex,
            "conversation_id": conv_id,
            "role": "user",
            "content": user_message,
        }
    )


def _persist_assistant_message(
    state: AppState,
    conv_id: str,
    assistant_answer: str,
    ctx,
    trace: list[dict] | None = None,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    """Write the assistant reply (or a placeholder for a failed
    turn) to the DB.

    Always writes a row, even when ``assistant_answer`` is empty.
    Empty content marks a failed turn (LLM error / aborted /
    tool crash) — but the row's PRESENCE is what tells the
    frontend's poll loop to stop waiting. Without this, a failed
    stream leaves a dangling user message and the reloaded UI
    polls fruitlessly until the 3-minute cap. The trace is still
    persisted so the user can see what tool calls ran before the
    failure.

    Token counts (``tokens_in`` / ``tokens_out``) come from the
    agent loop's final ``done`` event. Citations come from the
    enriched ``ctx.citation_pool`` populated during the stream.
    """
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
            "content": assistant_answer or "",
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
