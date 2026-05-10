"""
/api/v1/agent/chat — chat surface backed by Claude Agent SDK.

This is the Wave 2.5 route, the chat endpoint that B-MVP ships.
Coexists with the legacy ``/api/v1/agent/chat`` (handcrafted
``loop.py``-driven path) until Wave 3 cuts over.

Wire format (SSE, ``text/event-stream``):

    Each event is one ``data: <json>\\n\\n`` block. Order:

        agent.turn_start { turn: 1 }
        agent.thought    { text }            (zero or more)
        tool.call_start  { id, tool, params }
        tool.call_end    { id, tool, latency_ms, result_summary }
            (interleaved with answer.delta as the SDK loops)
        answer.delta     { text }            (token-stream of the model)
        agent.turn_end   { turn: 1 }
        done             { stop_reason, total_latency_ms,
                           final_text, error? }

The shape mirrors the legacy ``/agent/chat`` event vocabulary so
the frontend trace UI works against either route with minimal
adaptation. Wave 2.6's frontend changes are mostly about labelling
and artifact preview, not protocol.

Authz: the standard ``Depends(get_principal)`` covers cookie /
bearer auth (same as every other route). the SDK itself runs
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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent.claude_container_runtime import (
    ClaudeContainerRunner,
    SandboxUnavailableError,
    stream_turn_container,
)
from ..agent.claude_runtime import (
    ClaudeRuntime,
    ClaudeTurnConfig,
    ClaudeUnavailableError,
    stream_turn,
)
from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["claude-chat"])


# Used as the system prompt when the request doesn't override one.
# The bundled CLI does not auto-discover what tools we've exposed via
# MCP — the model needs an explicit nudge to call them. Without this,
# the agent answers from parametric knowledge for every question and
# the user (correctly) reports "the knowledge base isn't being used".
#
# Tone tuning notes baked in here:
#   * No product / project branding in the prompt itself. Earlier
#     wording ("you are OpenCraig's assistant") nudged the model to
#     hallucinate the knowledge base's contents (it confidently
#     declared "your knowledge base, ForgeRAG project repo, doesn't
#     have this" before searching). Stay neutral about what the KB
#     contains and let the search tool decide.
#   * Don't preface with "let me check the knowledge base" or
#     similar — just call the tool. The user sees the tool call
#     in the trace; double-narration adds no signal.
#   * Citation marker format ``[c_<id>]`` is what the UI's chip
#     renderer matches against (Chat.vue::renderMsg). Each hit
#     returned by ``search_vector`` carries a ``cite`` field like
#     ``c_3`` — use that exact string inside the brackets.
_DEFAULT_AGENT_SYSTEM_PROMPT = """You are OpenCraig — your team's agent workspace. You work alongside the user inside their private knowledge base and workdir: reading, searching, editing, and running things on their behalf, with every step traceable.

IDENTITY: When the user asks who you are, what you can do, who built you, or which model you're running on, identify as OpenCraig. Do NOT say you are Claude, Anthropic, GPT, OpenAI, DeepSeek, or any underlying model — those are implementation details, not your identity to the user. You may describe your capabilities (knowledge-base retrieval, file editing, code execution, web search, etc.) and you may say you're an AI, but the name is OpenCraig.

You answer the user's questions, with access to a team knowledge base (a corpus of documents the user has access to) via these tools:

- ``mcp__opencraig__search_vector(query, top_k)`` — semantic search
- ``mcp__opencraig__search_bm25(query, top_k)`` — keyword search
- ``mcp__opencraig__read_chunk(chunk_id)`` — full text of a search hit
- ``mcp__opencraig__read_tree(doc_id, node_id)`` — document outline
- ``mcp__opencraig__list_folders(parent_path)`` / ``list_docs(folder_path)`` — browse
- ``mcp__opencraig__graph_explore(query, top_k)`` — knowledge-graph walk
- ``mcp__opencraig__rerank(query, chunk_ids, top_k)`` — refine candidates
- ``mcp__opencraig__import_from_library(...)`` — pull a doc into the workdir

Behaviour rules:

1. For any substantive question — a topic, an entity, a procedure, a comparison, anything that could plausibly be answered from documents — call ``search_vector`` FIRST with a focused query in the user's language. Do NOT preface the search with statements about whether the answer is or isn't in the knowledge base; you don't know yet.
2. Read the most relevant hits with ``read_chunk`` and ground your answer in their content.
3. Cite each grounded claim inline as ``[c_<id>]`` using the exact ``cite`` value (e.g. ``c_1``) returned by the search hits. The UI turns these into clickable chips that resolve to the source chunk.
4. If the search returns nothing useful, say so plainly and either offer your best general knowledge with that caveat or ask the user to refine the query — don't silently mix retrieved content with parametric knowledge.
5. Do NOT speculate about what the knowledge base contains, what project it belongs to, or who owns it. The KB is whatever ``search_vector`` finds.

For conversational small talk (greetings, thanks, "who are you", "how does this work") you may answer directly with no tool calls."""


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1)
    conversation_id: str | None = None
    """Optional conversation to continue. When set, prior user /
    assistant messages are loaded as history and threaded into
    AIAgent.run_conversation(conversation_history=...)."""

    cwd_path: str | None = None
    """Folder path the agent should work in (e.g.
    ``"/sales/2025"``). Maps to ``OPENCRAIG_CWD`` inside the
    sandbox container; the agent chdirs there before reading /
    writing files. Editable per-turn — the UI's "switch folder"
    gesture sends a new cwd_path on the next message and the
    Conversation row is updated to match. NULL or empty = pure
    Q&A chat (agent works at /workspace root, no folder context).

    Folder-as-cwd refactor (20260518) replaces the prior
    project-id-based binding; conversations store the latest
    cwd_path on the row, so a re-load of the chat resumes in
    the right folder automatically."""

    model: str | None = None
    """Override the default model from cfg.answering.generator.model.
    Useful for per-conversation experimentation; the chat UI sets
    this from a dropdown."""

    system_prompt_override: str | None = None
    """Per-turn system prompt override. Same knob the legacy route
    has; passed straight to AIAgent as ``ephemeral_system_prompt``."""

    path_filters: list[str] | None = None
    """User-pinned knowledge scopes for this turn (chip-rail entries).

    Forwarded to the agent as a "preferred search scope" hint —
    the chat route prepends a one-line note to the user message
    naming each pinned path, and the agent fans out
    ``mcp__opencraig__search_vector(query, path_filter=…)`` calls
    once per path as needed. The hint is non-binding: the agent may
    search outside if the answer clearly isn't in any pinned scope.

    Sticky across turns: when ``None`` is sent, the chat route falls
    back to whatever's stored on the Conversation row (so a
    refresh / reopen replays the same pins). When a non-None list
    is sent, the route updates the Conversation row to match — the
    chip rail is the source of truth.
    """

    attachment_ids: list[str] = Field(default_factory=list)
    """Draft attachments the user pinned to this turn before sending.
    The route binds them to the persisted user message (so they
    survive conv reloads) and feeds their content to the agent:

      * ``kind=text``  — decoded UTF-8 inlined into the user's prompt
        as a labelled block (file name / mime header for context).
      * ``kind=image`` — Anthropic-format image content block (base64),
        gated by ``cfg.answering.generator.capabilities.vision``.
      * ``kind=pdf``   — Anthropic-format document content block,
        gated by ``cfg.answering.generator.capabilities.pdf``.

    Capability gating already runs at upload time (415 if the model
    can't handle the kind), so by the time we see ``attachment_ids``
    here every entry is admissible. We re-check at feed time anyway
    in case the configured model changed mid-conversation."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_model_name(state: AppState, override: str | None) -> str:
    """Pick the model name LiteLLM should dispatch to.

    Reads ``cfg.answering.generator.model`` as the default;
    ``override`` lets a request pin a specific model. The actual
    upstream provider URL + API key are NOT resolved here — they
    live on ``state.cfg.answering.generator`` and are injected by
    the LLM proxy itself when it forwards to the upstream provider.
    """
    gen = getattr(getattr(state.cfg, "answering", None), "generator", None)
    if gen is None:
        return override or "openai/gpt-4o-mini"
    return override or gen.model


def _agent_loopback_url(request) -> str:
    """The URL the agent's bundled CLI uses to reach our LLM proxy's
    Anthropic-compat surface (``/api/v1/llm/anthropic``).

    The agent's SDK gets ``ANTHROPIC_BASE_URL`` set to this; the SDK
    appends ``/v1/messages`` per Anthropic SDK convention. The agent
    speaks the Anthropic Messages wire format; our proxy converts it
    to whichever provider ``cfg.answering.generator.model`` actually
    points at (DeepSeek / OpenAI / SiliconFlow / native Anthropic /
    ...).

    Derived from the incoming request so the loopback works whatever
    port / interface the backend is bound to. Same backend, same
    scheme + host that brought the chat request in.
    """
    host = request.url.hostname or "127.0.0.1"
    # Backend on Windows binds 127.0.0.1; reverse-tunnelled hosts
    # (smoke test pattern) get rewritten to localhost loopback so
    # the SDK subprocess can reach it.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = request.url.port or 8000
    scheme = request.url.scheme or "http"
    return f"{scheme}://{host}:{port}/api/v1/llm/anthropic"


def _get_or_create_agent_token(
    state: AppState, principal: AuthenticatedPrincipal,
) -> str:
    """Return the user's deterministic agent-loopback bearer.

    The SDK's bundled CLI calls our LLM proxy + MCP server during a
    turn; those endpoints sit behind the same auth middleware as
    everything else, and the agent's outbound HTTP from a subprocess
    can't carry the user's web session cookie. So each user gets a
    stable ``aloop_<user>_<hmac>`` token derived from a server-side
    secret — same lifecycle as Claude Code's ANTHROPIC_API_KEY but
    with no DB row.

    Why HMAC-derived instead of a minted ``AuthToken`` row:

      * No DB churn — restart-safe by construction; same user always
        gets the same bearer. The previous design re-minted on every
        backend restart and accumulated 44 rows in a few weeks of
        dev because the in-memory cache was lost on restart and the
        hash-only column couldn't surface the raw value.
      * Verifiable by HMAC alone — middleware short-circuits on the
        ``aloop_`` prefix BEFORE the AuthToken hash lookup; these
        tokens never need (and never get) a row.
      * Revokable as a class — rotate ``storage/.agent_loop_secret``
        and every agent-loop bearer becomes invalid. Coarse but
        appropriate for an internal-only credential.

    See ``api/auth/agent_loop.py`` for the secret loading + HMAC
    construction, and ``api/auth/middleware.py`` for the
    short-circuit verify path.
    """
    from api.auth.agent_loop import mint_token

    return mint_token(principal.user_id)


def _load_conversation_history(state: AppState, conv_id: str) -> list[dict]:
    """Load prior user / assistant turns for an existing conversation
    so the SDK sees the context. Tool-call detail is NOT included —
    re-running the agent from text-only history is cheap enough
    (BM25 + vector hits cache via the embedding cache layer) and
    the schema's role column is restricted to user / assistant."""
    msgs: list[dict] = []
    rows = state.store.get_messages(conv_id)
    for row in rows or []:
        role = row.get("role") if isinstance(row, dict) else getattr(row, "role", None)
        content = (
            row.get("content") if isinstance(row, dict) else getattr(row, "content", None)
        )
        if role in ("user", "assistant") and isinstance(content, str) and content:
            msgs.append({"role": role, "content": content})
    return msgs


def _persist_user_message(state: AppState, conv_id: str, content: str) -> str:
    """Store the user turn before the SSE stream opens. Same rationale
    as legacy ``_persist_user_message``: a mid-stream refresh always
    recovers the question even if the answer never lands. Returns the
    generated ``message_id`` so the caller can bind draft attachments
    onto this row."""
    message_id = uuid.uuid4().hex
    state.store.add_message(
        {
            "message_id": message_id,
            "conversation_id": conv_id,
            "role": "user",
            "content": content,
        }
    )
    return message_id


# ---------------------------------------------------------------------------
# Attachment feed — turn draft uploads into agent-visible content
# ---------------------------------------------------------------------------


def _read_capabilities(state: AppState) -> tuple[bool, bool]:
    """Mirror of attachments.py::_capabilities — kept as a private
    copy so the chat route doesn't take a dep on the attachments
    router module."""
    gen = getattr(getattr(state.cfg, "answering", None), "generator", None)
    caps = getattr(gen, "capabilities", None)
    if caps is None:
        return False, False
    return (
        bool(getattr(caps, "vision", False)),
        bool(getattr(caps, "pdf", False)),
    )


# Cap the inlined text content per attachment so a 25 MiB textfile
# upload doesn't blow the model's context window in one go. The agent
# can still ``Read`` the file from the workdir (when a sandbox is
# wired) for the full content; this prefix is just enough for the
# model to see what was attached and answer questions about it.
_INLINE_TEXT_CAP = 64 * 1024  # 64 KiB


def _build_attachment_feed(
    state: AppState,
    *,
    user_id: str,
    conv_id: str,
    attachment_ids: list[str],
) -> tuple[str, list[dict], list[str]]:
    """Read each attachment + classify into the right feed channel.

    Returns ``(text_prefix, extra_blocks, skipped)``:

      * ``text_prefix`` — concatenated UTF-8 of all ``kind=text``
        attachments, wrapped in ``--- attachment: <name> ---`` /
        ``--- end attachment ---`` markers. The route prepends this
        to the user's typed query so the agent sees the file content
        as part of the prompt.

      * ``extra_blocks`` — Anthropic-format content blocks for image
        / pdf attachments. The runtime appends these to the user's
        message ``content`` array so the model receives them as
        native multimodal input.

      * ``skipped`` — filenames that couldn't be fed (capability
        flag flipped off after upload, blob missing, decode error,
        ...). The caller doesn't surface these today; they're
        returned so future debug paths can log them.
    """
    if not attachment_ids:
        return "", [], []

    import base64
    from pathlib import Path

    cap_vision, cap_pdf = _read_capabilities(state)

    # ``Attachment.blob_path`` is stored RELATIVE to the configured
    # ``user_uploads_root`` so admin can re-point the storage tree
    # without rewriting DB rows (see attachments.py upload-route's
    # comment). Resolve against the live cfg root here, mirroring
    # how the blob-download route opens the file.
    uploads_root_cfg = (
        getattr(state.cfg.agent, "user_uploads_root", None)
        or "./storage/user-uploads"
    )
    uploads_root = Path(uploads_root_cfg).resolve()

    text_lines: list[str] = []
    extra_blocks: list[dict] = []
    skipped: list[str] = []

    for aid in attachment_ids:
        meta = state.store.get_attachment(aid)
        if not meta:
            continue
        # Belt-and-braces ownership check: the upload route already
        # gates by user, but a tampered request could pass another
        # user's attachment id. Refuse silently — the agent simply
        # doesn't see it.
        if meta.get("user_id") != user_id:
            continue
        if meta.get("conversation_id") != conv_id:
            continue

        kind = meta.get("kind") or "other"
        filename = meta.get("filename") or "attachment"
        mime = meta.get("mime") or "application/octet-stream"
        blob_path = meta.get("blob_path") or ""

        # Resolve against the uploads root + verify the resolved
        # absolute path stays UNDER the root. The stored value comes
        # from a uuid-prefixed component we control, so a traversal
        # is unlikely, but defending in depth is cheap.
        try:
            full = (uploads_root / blob_path).resolve()
            full.relative_to(uploads_root)
            data = full.read_bytes()
        except (OSError, ValueError):
            log.warning(
                "attachment feed: blob unreadable aid=%s path=%s",
                aid, blob_path,
            )
            skipped.append(filename)
            continue

        if kind == "text":
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                skipped.append(filename)
                continue
            if len(text) > _INLINE_TEXT_CAP:
                truncated = len(text) - _INLINE_TEXT_CAP
                text = (
                    text[:_INLINE_TEXT_CAP]
                    + f"\n…[+{truncated} chars truncated for prompt]"
                )
            text_lines.append(f"--- attachment: {filename} ({mime}) ---")
            text_lines.append(text)
            text_lines.append("--- end attachment ---")
            text_lines.append("")
        elif kind == "image":
            if not cap_vision:
                skipped.append(filename)
                continue
            extra_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            })
        elif kind == "pdf":
            if not cap_pdf:
                skipped.append(filename)
                continue
            extra_blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            })
        else:
            skipped.append(filename)

    text_prefix = "\n".join(text_lines) if text_lines else ""
    return text_prefix, extra_blocks, skipped


def _persist_assistant_message(
    state: AppState,
    conv_id: str,
    content: str,
    *,
    agent_trace: list | None = None,
    citations: list | None = None,
) -> None:
    """Always writes a row, even when ``content`` is empty. Empty =
    failed turn (LLM error / aborted) — the row's PRESENCE is what
    tells the frontend's poll loop to stop waiting.

    ``agent_trace`` is the chronological sequence of phase / thought /
    tool entries the runtime produced for this turn. The shape mirrors
    what ``Chat.vue`` builds live as ``streamTrace`` so on conversation
    reload, ``AgentMessageBody`` rebuilds the same step-by-step view
    the user saw during streaming. ``None`` for failed turns / older
    rows; the frontend renders just the answer body when absent.

    ``citations`` carries the per-turn citation pool the model may
    have quoted via ``[c_<id>]`` markers — landed in
    ``Message.citations_json`` so reload turns the markers back into
    clickable chips via ``Chat.vue::renderMsg``."""
    record: dict = {
        "message_id": uuid.uuid4().hex,
        "conversation_id": conv_id,
        "role": "assistant",
        "content": content,
    }
    if agent_trace:
        record["agent_trace_json"] = agent_trace
    if citations:
        record["citations_json"] = citations
    state.store.add_message(record)


class _TraceAccumulator:
    """Mirrors the frontend's ``streamTrace`` reducer (Chat.vue) so the
    persisted shape matches what the live UI builds. Without this,
    reloading a conversation drops back to the bare ``content`` string
    and loses every tool chip + intermediate narration the user saw.

    Entry shapes (kept compatible with ``AgentMessageBody.vue``):
      * phase:   {kind: 'phase', phase, text: '', elapsedSec, status}
      * thought: {kind: 'thought', phase, text, elapsedSec, status}
                 (a phase whose narration text was filled in)
      * tool:    {kind: 'tool', call_id, name, detail, elapsedMs,
                  status, summary}

    Timing is recorded as ``elapsed*`` only (no absolute timestamps)
    so the persisted blob is portable across timezones and reproducible
    by a refresh."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._turn: int = 0
        self._answer_buf: list[str] = []
        self._tool_t0: dict[str, float] = {}
        self._phase_t0: dict[int, float] = {}

    @staticmethod
    def _phase_label(turn_idx: int) -> str:
        return "planning" if turn_idx == 0 else "reviewing"

    def _last_running_phaseish(self) -> dict | None:
        for e in reversed(self.entries):
            if e.get("kind") in ("phase", "thought") and e.get("status") == "running":
                return e
        return None

    def on_turn_start(self) -> None:
        idx = self._turn
        self.entries.append(
            {
                "kind": "phase",
                "phase": self._phase_label(idx),
                "text": "",
                "elapsedSec": 0,
                "status": "running",
            }
        )
        self._phase_t0[len(self.entries) - 1] = time.time()

    def on_turn_end(self) -> None:
        last = self._last_running_phaseish()
        if last is not None:
            last["status"] = "done"
            idx = self.entries.index(last)
            t0 = self._phase_t0.pop(idx, None)
            if t0 is not None:
                last["elapsedSec"] = max(0, int(time.time() - t0))
        self._turn += 1
        self._answer_buf.clear()

    def on_thought(self, text: str) -> None:
        last = self._last_running_phaseish()
        if last is not None:
            last["kind"] = "thought"
            # If the model produces multiple thinking deltas for the
            # same phase, concatenate (matches text-streaming intent).
            last["text"] = (last.get("text") or "") + text
        else:
            self.entries.append(
                {
                    "kind": "thought",
                    "phase": self._phase_label(self._turn),
                    "text": text,
                    "elapsedSec": 0,
                    "status": "done",
                }
            )

    def on_answer_delta(self, text: str) -> None:
        # Buffered until either (a) a tool.call_start moves it into a
        # thought entry as a tool-preface narration, or (b) the turn
        # ends and the buffered text becomes the final answer body
        # (which lives in ``content``, not in the trace).
        if text:
            self._answer_buf.append(text)

    def on_tool_start(
        self, call_id: str, name: str, params: dict | None
    ) -> None:
        # Move any buffered answer-delta text into the trailing
        # phase/thought entry as preface ("Let me search for X first..."
        # — Claude Code style narration before tool use).
        last = self._last_running_phaseish()
        if last is not None:
            if self._answer_buf:
                last["kind"] = "thought"
                last["text"] = (last.get("text") or "") + "".join(
                    self._answer_buf
                )
                self._answer_buf.clear()
            last["status"] = "done"
            idx = self.entries.index(last)
            t0 = self._phase_t0.pop(idx, None)
            if t0 is not None:
                last["elapsedSec"] = max(0, int(time.time() - t0))

        detail = ""
        if isinstance(params, dict):
            for key in ("query", "chunk_id", "doc_id", "command", "path"):
                v = params.get(key)
                if isinstance(v, str) and v:
                    detail = v[:64]
                    break
        # ``input`` keeps the full params dict (Bash command body,
        # Write file path + content, search query, …) so the frontend
        # can render it verbatim inside the expanded chip. ``detail``
        # is the legacy short headline still used for the collapsed
        # one-liner.
        self.entries.append(
            {
                "kind": "tool",
                "call_id": call_id,
                "name": name,
                "detail": detail,
                "input": params if isinstance(params, dict) else {},
                "output": "",
                "elapsedMs": 0,
                "status": "running",
                "summary": "",
            }
        )
        self._tool_t0[call_id] = time.time()

    def on_tool_end(
        self,
        call_id: str,
        latency_ms: int,
        result_summary: dict | None,
        output: str = "",
        is_error: bool = False,
    ) -> None:
        for e in reversed(self.entries):
            if e.get("kind") == "tool" and e.get("call_id") == call_id:
                e["status"] = "error" if is_error else "done"
                e["elapsedMs"] = int(latency_ms or 0)
                summary = result_summary or {}
                if summary.get("hit_count") is not None:
                    e["summary"] = f"{summary['hit_count']} hits"
                elif summary.get("entity_count") is not None:
                    e["summary"] = f"{summary['entity_count']} entities"
                elif summary.get("chunk_count") is not None:
                    e["summary"] = f"{summary['chunk_count']} chunks"
                elif summary.get("error") or is_error:
                    e["summary"] = "error"
                # Capped tool response stringified by the runtime.
                # The frontend renders this verbatim inside the
                # expanded chip — Bash stdout, Read body, hit-list
                # JSON, etc. Empty when the runtime didn't return
                # anything, which is fine.
                if output:
                    e["output"] = output
                # Persist the failure flag separately so the frontend
                # has a single boolean check instead of pattern-
                # matching on summary contents.
                e["isError"] = bool(is_error)
                break
        self._tool_t0.pop(call_id, None)

    def snapshot(self) -> list[dict]:
        return [dict(e) for e in self.entries]


def _persist_agent_run(
    state: AppState,
    *,
    run_id: str,
    conv_id: str | None,
    user_id: str,
    cwd_path: str | None,
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

    ``cwd_path`` is the folder the run worked in — pinned to the row
    even if the conversation later moves to a different folder, so
    audit views ("what did this user's agent do in /sales/2025/?")
    have a stable answer.

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
                "cwd_path": cwd_path,
                "status": "error" if error else "ok",
                "final_text": final_text,
                "iterations": iterations,
                "error": error,
                "started_at": started_at,
                "finished_at": finished_at,
            }
        )
    except Exception:
        log.exception("claude_chat: agent_run persist failed run_id=%s", run_id)


# ---------------------------------------------------------------------------
# SSE event translation: ClaudeRuntime events → wire format
# ---------------------------------------------------------------------------


def _sse(type_: str, payload: dict) -> str:
    """One SSE block in the format the legacy ``/agent/chat`` route
    uses: ``data: <json>\\n\\n`` where the JSON dict carries the
    event type as its ``type`` field. Matching this exactly means
    the frontend SSE parser (``web/src/api/agent.js``) doesn't need
    to change for Wave 2.6 — only the URL changes."""
    payload = {"type": type_, **payload}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _translate(evt: dict) -> str | None:
    """Map a ClaudeRuntime event dict to a single SSE block.

    Returns ``None`` for runtime events we fold into a different
    SSE event (``error`` and ``done`` are surfaced via the outer
    layer's terminal ``done`` block, not as their own SSE events).
    """
    kind = evt.get("kind")
    if kind == "thinking":
        return _sse("agent.thought", {"text": evt.get("text", "")})
    if kind == "answer_delta":
        return _sse("answer.delta", {"text": evt.get("text", "")})
    if kind == "citations":
        # Forward the running citation pool — the frontend folds these
        # into its in-memory message so ``[c_<id>]`` markers in the
        # answer body get rendered as clickable chips. Emitted on each
        # post-tool tick + bundled into the terminal ``done`` event
        # below as a fallback for clients that only consume ``done``.
        return _sse("citations", {"items": evt.get("items") or []})
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
                "output": evt.get("output", ""),
                "is_error": bool(evt.get("is_error")),
            },
        )
    if kind == "error":
        # Error events are folded into ``done`` at the outer layer
        # so the client only sees one terminal event. Returning
        # None here lets ``_run_stream`` capture the error and emit
        # a single ``done { stop_reason: "error" }`` at the end.
        return None
    if kind == "done":
        # the SDK's ``done`` is internal — we emit our own ``done``
        # at the SSE layer so it carries total_latency_ms + the
        # final assembled text.
        return None
    return None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/chat")
async def claude_chat(
    request: Request,
    body: ChatRequest,
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    """SSE stream of SDK-driven agent events. See module docstring
    for the wire format."""

    # Resolve runtime config. Three independent things the SDK needs:
    #   * model        — what name LiteLLM dispatches to. Reads from
    #                    cfg.answering.generator.model unless the
    #                    request body overrides.
    #   * base_url     — OUR backend's Anthropic-compat proxy. The
    #                    SDK speaks Anthropic Messages format; our
    #                    /api/v1/llm/anthropic surface translates to
    #                    whichever provider the model name dispatches
    #                    to (DeepSeek / OpenAI / SiliconFlow /
    #                    native Anthropic / etc.). Going direct to
    #                    the upstream provider (e.g. api.deepseek.com)
    #                    would 401 — DeepSeek doesn't speak Anthropic
    #                    Messages.
    #   * api_key      — a short-lived Bearer that authenticates the
    #                    SDK's loopback call with our auth middleware.
    #                    NOT the upstream provider's key — that lives
    #                    on cfg.answering.generator.api_key and is
    #                    injected by the LLM proxy when it forwards
    #                    onward.
    try:
        model = _resolve_model_name(state, body.model)
        base_url = _agent_loopback_url(request)
        api_key = _get_or_create_agent_token(state, principal)
    except Exception as e:
        log.exception("claude_chat: model config resolution failed")
        raise HTTPException(status_code=500, detail=str(e))

    # Load history + resolve cwd_path + persist user message BEFORE
    # the stream opens. Idempotent for new conversations (history
    # empty).
    #
    # cwd_path resolution order:
    #   1. body.cwd_path  — explicit per-request override (UI's
    #      "switch folder" gesture). Persisted to the conversation
    #      so subsequent reloads resume in the new folder.
    #   2. Conversation.cwd_path  — what the chat was opened in.
    #   3. None  — plain Q&A, agent works at /workspace root.
    history: list[dict] = []
    cwd_path: str | None = body.cwd_path
    # Knowledge-scope chips effective for THIS turn. Populated from
    # body.path_filters (explicit) or the stored Conversation row
    # (sticky). For ad-hoc chats with no conversation_id we just use
    # whatever the request carries.
    effective_path_filters: list[str] = [
        p for p in (body.path_filters or []) if isinstance(p, str) and p.strip()
    ]
    if body.conversation_id:
        try:
            history = _load_conversation_history(state, body.conversation_id)
        except Exception:
            log.exception(
                "claude_chat: history load failed conv=%s", body.conversation_id
            )
            history = []

        # Pull stored cwd_path off the conversation row if the
        # request didn't override it; this is the path the chat
        # was opened in (UI navigated from a folder).
        try:
            existing = state.store.get_conversation(body.conversation_id)
            if existing is not None:
                stored_cwd = existing.get("cwd_path") if isinstance(existing, dict) else None
                if cwd_path is None and stored_cwd:
                    cwd_path = stored_cwd
                # If the request DID send a different cwd_path,
                # write it back so the conversation row reflects
                # the user's latest "switch folder" choice.
                if body.cwd_path and body.cwd_path != stored_cwd:
                    try:
                        state.store.update_conversation(
                            body.conversation_id, cwd_path=body.cwd_path,
                        )
                    except Exception:
                        log.exception(
                            "claude_chat: cwd_path update failed conv=%s",
                            body.conversation_id,
                        )
                # Knowledge-scope chip rail. The frontend ships the
                # current pinned set on every send; ``None`` means
                # "no change, use stored". Persist when the request
                # carries a list (even an empty one — explicit clear).
                stored_pf = (
                    existing.get("path_filters") if isinstance(existing, dict) else []
                ) or []
                if body.path_filters is not None:
                    cleaned_pf = [p for p in body.path_filters if isinstance(p, str) and p.strip()]
                    if cleaned_pf != stored_pf:
                        try:
                            state.store.update_conversation(
                                body.conversation_id,
                                path_filters_json=cleaned_pf,
                            )
                        except Exception:
                            log.exception(
                                "claude_chat: path_filters update failed conv=%s",
                                body.conversation_id,
                            )
                    effective_path_filters = cleaned_pf
                else:
                    effective_path_filters = list(stored_pf)
        except Exception:
            log.exception(
                "claude_chat: cwd_path resolution failed conv=%s",
                body.conversation_id,
            )

        try:
            user_message_id = _persist_user_message(
                state, body.conversation_id, body.query,
            )
        except Exception:
            log.exception(
                "claude_chat: user-message persist failed conv=%s",
                body.conversation_id,
            )
            user_message_id = None

        # Bind draft attachments to the freshly-persisted user message
        # so a conversation reload still sees the chip rail under the
        # user's bubble. Best-effort — a failure here doesn't block
        # the turn (the attachments stay as drafts and the user can
        # retry / clear them).
        if user_message_id and body.attachment_ids:
            try:
                state.store.bind_attachments_to_message(
                    user_message_id, list(body.attachment_ids),
                )
            except Exception:
                log.exception(
                    "claude_chat: bind_attachments failed conv=%s msg=%s",
                    body.conversation_id, user_message_id,
                )

    # Build the agent-visible attachment feed BEFORE the runtime call
    # so we can inline text content into ``user_message`` and pass the
    # image/pdf blocks alongside. We do this even for ad-hoc (no conv
    # id) chats — the upload route already requires a conv, but if a
    # client manages to send attachment_ids without conversation_id,
    # the per-conv ownership check inside _build_attachment_feed
    # rejects them and we fall through with an empty feed.
    attachment_text_prefix = ""
    extra_user_blocks: list[dict] = []
    if body.attachment_ids and body.conversation_id:
        try:
            attachment_text_prefix, extra_user_blocks, _skipped = (
                _build_attachment_feed(
                    state,
                    user_id=principal.user_id,
                    conv_id=body.conversation_id,
                    attachment_ids=list(body.attachment_ids),
                )
            )
        except Exception:
            log.exception(
                "claude_chat: attachment feed build failed conv=%s",
                body.conversation_id,
            )

    # Knowledge-scope hint: turn the chip-rail entries into a one-shot
    # note the agent reads at the top of the user message. The hint
    # is non-binding — the agent may search outside if the answer
    # clearly isn't in any pinned scope. We list paths verbatim so
    # the agent can copy them straight into ``search_vector(query,
    # path_filter=…)`` calls without further parsing.
    knowledge_scope_hint = ""
    if effective_path_filters:
        path_lines = "\n".join(f"  - {p}" for p in effective_path_filters)
        knowledge_scope_hint = (
            "[knowledge scope]\n"
            "The user has pinned the following knowledge paths for this "
            "turn — prefer searching within these scopes. Call "
            "``search_vector`` once per path if you need broad coverage; "
            "you may search outside if the answer clearly isn't in any "
            "of them.\n"
            f"{path_lines}\n"
            "[end knowledge scope]\n"
        )

    # Compose the prompt the agent actually sees: knowledge-scope
    # hint first (so the agent commits to the right tool calls before
    # processing context), then text-attachment bodies (so the model
    # has the file context before the question), then the user's
    # typed query. Mirrors the "drag a file into ChatGPT, then ask
    # about it" UX shape with an extra "by the way these folders are
    # relevant" prelude.
    parts: list[str] = []
    if knowledge_scope_hint:
        parts.append(knowledge_scope_hint)
    if attachment_text_prefix:
        parts.append(attachment_text_prefix)
    parts.append(body.query)
    composed_query = "\n".join(parts)

    # Wire the OpenCraig MCP server so the agent has real domain tools
    # (search_vector, read_chunk, list_folders, graph_explore, etc.).
    # Without this, the agent runs with zero tools and answers every
    # question from parametric knowledge — no RAG, no library lookup.
    # URL: same loopback the SDK uses for the LLM proxy, rerooted to
    # ``/api/v1/mcp``. Auth: the per-user agent-loop bearer; the MCP
    # principal-bridge middleware reads it and scopes tool calls to
    # the right user (folder grants etc.).
    proxy_root = _agent_loopback_url(request).rsplit(
        "/api/v1/llm/anthropic", 1
    )[0]
    mcp_servers = {
        "opencraig": {
            "url": f"{proxy_root}/api/v1/mcp/",
            "headers": {"Authorization": f"Bearer {api_key}"},
        }
    }

    config = ClaudeTurnConfig(
        model=model,
        base_url=base_url or "",  # empty = let openai SDK use its default
        api_key=api_key or "",
        max_iterations=90,
        system_message=body.system_prompt_override or _DEFAULT_AGENT_SYSTEM_PROMPT,
        mcp_servers=mcp_servers,
    )

    run_id = uuid.uuid4().hex
    started_at = time.time()

    # Pick which Claude SDK runtime drives this turn.
    #
    # ``container`` (preferred when available): the Claude Agent SDK runs INSIDE
    #   the user's sandbox container with full built-in toolsets
    #   (Read / Edit / Bash / Glob / Grep) operating on the
    #   bind-mounted workdir. This is the path that makes the
    #   Workspace actually useful — agent can read project files,
    #   write artifacts, run commands.
    #
    # ``in-process`` fallback: the Claude Agent SDK runs in this FastAPI worker
    #   with built-in toolsets HARD-DISABLED (would touch our fs).
    #   Only MCP-exposed domain tools (search / KG / library) are
    #   reachable. Fine for pure Q&A; Workspace work is degraded.
    #
    # The route picks ``container`` whenever a SandboxManager is
    # wired on AppState. Operators without Docker (dev / minimal
    # deployments) get the in-process path automatically.
    use_container = getattr(state, "sandbox", None) is not None

    async def _run_stream() -> AsyncIterator[bytes]:
        # Emit turn_start synchronously so the client sees activity
        # immediately even if the SDK's first network call is slow.
        yield _sse("agent.turn_start", {"turn": 1, "run_id": run_id}).encode("utf-8")

        final_text = ""
        error_message: str | None = None
        iterations = 0
        delta_buf: list[str] = []
        # Mirror the frontend's streamTrace reducer in Python so we
        # persist the same chronological phase/thought/tool sequence
        # the user saw live. Without this, ``Message.agent_trace_json``
        # stays NULL and reloading the conversation drops back to a
        # bare answer body with no inline tool chips.
        trace_acc = _TraceAccumulator()
        trace_acc.on_turn_start()

        # Build the right iterator for this turn's runtime mode.
        # Both stream sync generators on a worker thread; we pump
        # via ``asyncio.run_in_executor`` so the FastAPI event loop
        # stays free for SSE flushes + concurrent connections.
        try:
            if use_container:
                container_runner = ClaudeContainerRunner(state.sandbox)
                iter_ = stream_turn_container(
                    container_runner,
                    composed_query,
                    config=config,
                    principal_user_id=principal.user_id,
                    cwd_path=cwd_path,
                    conversation_history=history,
                    extra_user_content_blocks=extra_user_blocks,
                )
            else:
                runtime = ClaudeRuntime()
                iter_ = stream_turn(
                    runtime,
                    composed_query,
                    config=config,
                    conversation_history=history,
                    extra_user_content_blocks=extra_user_blocks,
                )
        except (ClaudeUnavailableError, SandboxUnavailableError) as e:
            error_message = f"agent runtime unavailable: {e}"
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
                cwd_path=cwd_path,
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

        # Per-turn citation pool. Mirrors the runtime's accumulator so
        # the route can ship the snapshot via the terminal ``done``
        # event (clients that drop the streamed ``citations`` ticks
        # still get the final list) and persist it onto the assistant
        # message row for reload.
        citations_pool: list[dict] = []

        try:
            while True:
                evt = await loop.run_in_executor(None, _next_or_sentinel, iter_)
                if evt is sentinel:
                    break
                if not isinstance(evt, dict):
                    continue
                kind = evt.get("kind")
                if kind == "answer_delta":
                    delta_buf.append(evt.get("text", ""))
                    trace_acc.on_answer_delta(evt.get("text", ""))
                elif kind == "thinking":
                    trace_acc.on_thought(evt.get("text", ""))
                elif kind == "tool_start":
                    trace_acc.on_tool_start(
                        evt.get("id", ""),
                        evt.get("tool", ""),
                        evt.get("params") if isinstance(evt.get("params"), dict) else None,
                    )
                elif kind == "tool_end":
                    trace_acc.on_tool_end(
                        evt.get("id", ""),
                        int(evt.get("latency_ms") or 0),
                        evt.get("result_summary")
                        if isinstance(evt.get("result_summary"), dict)
                        else None,
                        str(evt.get("output") or ""),
                        is_error=bool(evt.get("is_error")),
                    )
                elif kind == "citations":
                    items = evt.get("items") or []
                    # Each tick replaces the running pool with the
                    # runtime's deduped snapshot — we trust the
                    # runtime to handle dedup so the route's only
                    # responsibility is to keep the latest list
                    # around for the ``done`` event.
                    if isinstance(items, list):
                        citations_pool = items
                elif kind == "error":
                    error_message = (
                        f"{evt.get('type', 'RuntimeError')}: "
                        f"{evt.get('message', 'agent failed')}"
                    )
                elif kind == "done":
                    iterations = int(evt.get("iterations") or 0)
                    # If the SDK's final dict already had a final_text
                    # (extracted by the runtime), prefer that —
                    # delta_buf may have lost partial chunks.
                    ft = evt.get("final_text") or ""
                    if ft:
                        final_text = ft
                line = _translate(evt)
                if line:
                    yield line.encode("utf-8")
        except Exception:
            log.exception("claude_chat: stream pump raised")
            error_message = error_message or "agent failed: stream pump"

        # ``final_text`` falls back to the assembled deltas if the
        # ``done`` event didn't carry one.
        if not final_text:
            final_text = "".join(delta_buf)

        trace_acc.on_turn_end()
        yield _sse("agent.turn_end", {"turn": 1, "run_id": run_id}).encode("utf-8")
        yield _emit_done(
            error_message=error_message,
            final_text=final_text,
            started_at=started_at,
            run_id=run_id,
            citations=citations_pool,
        )

        # Post-stream persistence — never block the response on this
        if body.conversation_id:
            try:
                _persist_assistant_message(
                    state,
                    body.conversation_id,
                    final_text,
                    agent_trace=trace_acc.snapshot() if trace_acc.entries else None,
                    citations=citations_pool,
                )
            except Exception:
                log.exception(
                    "claude_chat: assistant persist failed conv=%s",
                    body.conversation_id,
                )

        _persist_agent_run(
            state,
            run_id=run_id,
            conv_id=body.conversation_id,
            user_id=principal.user_id,
            cwd_path=cwd_path,
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
    citations: list[dict] | None = None,
) -> bytes:
    """Single ``done`` SSE block — always the final event the client
    sees, in success or error case. ``citations`` carries the
    accumulated per-turn pool the model may have referenced via
    ``[c_<id>]`` markers; clients that drop the streamed
    ``citations`` ticks can fall back to this."""
    payload: dict[str, Any] = {
        "stop_reason": "error" if error_message else "end_turn",
        "total_latency_ms": int((time.time() - started_at) * 1000),
        "final_text": final_text,
        "run_id": run_id,
    }
    if citations:
        payload["citations"] = citations
    if error_message:
        payload["error"] = error_message
    return _sse("done", payload).encode("utf-8")
