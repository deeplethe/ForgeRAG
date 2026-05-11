"""
OpenCraig agent-turn entrypoint, lives inside the sandbox container.

The backend's ``ClaudeContainerRunner`` invokes this script via
``docker exec <user-container> python /opt/opencraig/opencraig_run_turn.py``
once per chat turn. The script:

  1. Reads the user message + conversation history + agent config
     from environment variables.
  2. Chdirs into the cwd_path subfolder of /workspace before importing
     the SDK so the agent's Read / Edit / Bash / Glob / Grep tools
     start at the right place (they capture os.getcwd() at startup).
  3. Drives one turn of the Claude Agent SDK loop. Built-in tools
     are ENABLED here because this IS the sandbox — the bind-mount
     limits filesystem reach to /workspace = the user's private
     workspace.
  4. Translates each SDK message into a single JSONL line on stdout.
     The backend reads stdout line-by-line and converts to SSE
     events for the frontend.

Event vocabulary (one JSON object per stdout line, terminated by
``\\n``; flushed after each emit):

    { "kind": "thinking",     "text": "..." }
    { "kind": "tool_start",   "id": "...",  "tool": "...",
                              "params": {...} }
    { "kind": "tool_end",     "id": "...",  "tool": "...",
                              "latency_ms": 42,
                              "result_summary": {...} }
    { "kind": "answer_delta", "text": "..." }
    { "kind": "done",         "final_text": "...",
                              "iterations": 3 }
    { "kind": "error",        "type": "...", "message": "..." }

Why an explicit Python wrapper (vs. invoking the bundled ``claude``
binary directly):
    The wrapper handles env-var → SDK config translation cleanly,
    composes conversation history into the SDK's streaming-input
    prompt format without shell-escaping pain, and re-uses our own
    tool-event mapping logic so backend / in-process / in-container
    paths all emit the same JSONL schema.

Env vars the backend sets per turn:
    OPENCRAIG_USER_MESSAGE  — the user's question (required)
    OPENCRAIG_HISTORY       — JSON array of prior {role, content}
                              messages (default: [])
    OPENCRAIG_MODEL         — model name (e.g.
                              ``claude-sonnet-4-5-20250929``)
    OPENCRAIG_MAX_TURNS     — agent iteration cap (default 90)
    OPENCRAIG_SYSTEM_PROMPT — optional ephemeral system prompt
    OPENCRAIG_CWD           — folder path INSIDE /workspace/ to chdir
                              into before invoking the agent (e.g.
                              ``/sales/2025``). Empty / unset = stay
                              at /workspace root.
    OPENCRAIG_MCP_URL       — backend MCP server URL (e.g.
                              ``http://backend:8000/api/v1/mcp``).
                              Optional; if absent the agent runs
                              without MCP tools (built-ins still
                              available).
    OPENCRAIG_MCP_TOKEN     — bearer token for the MCP server.
    OPENCRAIG_EXTRA_USER_BLOCKS
                            — JSON array of Anthropic content blocks
                              (image / document) the user attached this
                              turn. Prepended to the user message body
                              ahead of the text block so the model
                              sees the visual context first. Optional;
                              omit for text-only turns.
    ANTHROPIC_BASE_URL      — points at backend's LiteLLM proxy
                              Anthropic-compat surface.
    ANTHROPIC_API_KEY       — session token (LiteLLM resolves real
                              provider key on the backend).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback


def emit(payload: dict) -> None:
    """Write one JSONL event to stdout + flush. The backend tails
    this stream live; flushing is what makes the SSE stream feel
    interactive instead of arriving all at once at the end."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _read_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _read_history() -> list[dict]:
    raw = os.environ.get("OPENCRAIG_HISTORY")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [m for m in parsed if isinstance(m, dict)]


def _read_extra_user_blocks() -> list[dict]:
    """Decode the multimodal content blocks the backend stuffed into
    ``OPENCRAIG_EXTRA_USER_BLOCKS`` for this turn. Returns an empty
    list when the env var is missing or unparseable so a malformed
    payload silently falls back to text-only — the agent still gets
    the user's typed query, just without the attached media."""
    raw = os.environ.get("OPENCRAIG_EXTRA_USER_BLOCKS")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [b for b in parsed if isinstance(b, dict) and b.get("type")]


def _chdir_to_cwd_or_workdir() -> None:
    """Move into the agent's working directory before importing the
    SDK. The bundled CLI captures os.getcwd() for its built-in shell
    tools' starting point; setting it here makes OPENCRAIG_CWD the
    agent's "current folder" without any further wiring inside the
    SDK itself.

    Order of preference:
        1. ``/workspace`` + OPENCRAIG_CWD  (folder-as-cwd path)
        2. ``/workspace``                  (no folder bound)
        3. fall back to wherever we are  (dev / test outside container)
    """
    cwd_rel = (os.environ.get("OPENCRAIG_CWD") or "").strip()
    if cwd_rel and not cwd_rel.startswith("/"):
        cwd_rel = "/" + cwd_rel
    cwd_rel = cwd_rel.rstrip("/")

    base = "/workspace"
    target = base + cwd_rel if cwd_rel else base

    if not os.path.isdir(target):
        # Auto-create the cwd folder if the user gave us one that
        # doesn't yet exist on disk. Same affordance the Workspace
        # UI's "create folder" gives, but here driven by the chat
        # opening in a not-yet-materialised path. Falls back to
        # base if even ``/workspace`` is missing (test / dev contexts).
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            target = base if os.path.isdir(base) else os.getcwd()

    try:
        os.chdir(target)
    except OSError:
        traceback.print_exc(file=sys.stderr)


def _build_options(sdk, *, mcp_url: str, mcp_token: str):
    """Compose the SDK's per-turn options. Built-in toolsets are
    ENABLED here (this IS the sandbox); MCP servers carry our
    domain tools (search / KG / library / workdir) over HTTP back
    to the backend."""
    mcp_servers: dict = {}
    if mcp_url:
        headers = {"Authorization": f"Bearer {mcp_token}"} if mcp_token else {}
        mcp_servers["opencraig"] = {
            "type": "http",
            "url": mcp_url,
            "headers": headers,
        }

    # Thinking disabled by default. Non-Anthropic providers (DeepSeek
    # / OpenAI / etc.) reached through our LiteLLM Anthropic-compat
    # proxy don't emit ThinkingBlock with signatures, and the bundled
    # claude CLI's strict Anthropic-API parser rejects unsigned
    # thinking content with "Claude Code returned an error result:
    # success". Disabling thinking at the SDK level avoids the round
    # trip entirely. Operator can opt back in by setting
    # OPENCRAIG_THINKING=enabled (only safe with native Anthropic).
    #
    # ThinkingConfigDisabled / Enabled are TypedDicts in the SDK —
    # constructed as plain dicts with the ``type`` discriminator the
    # subprocess_cli transport reads when assembling CLI args.
    thinking_cfg: dict = {"type": "disabled"}
    if _read_str("OPENCRAIG_THINKING").lower() in ("enabled", "on", "1", "true"):
        thinking_cfg = {"type": "enabled", "budget_tokens": 1024}

    # PreToolUse hook → HTTP callback to backend.
    #
    # SDK-builtin tools (Bash / Edit / Write / Delete / ...) execute
    # inside this sandbox without round-tripping through the backend's
    # MCP server, so MCP-layer approval (round-7 Bug 11 fix) doesn't
    # cover them. The only signal channel we have into a sandboxed
    # agent is the HTTP gateways it already calls (LLM proxy + MCP).
    # We add one more: POST /api/v1/agent/internal/pre_tool_use to
    # ask the backend "should this tool fire?", await the verdict,
    # translate it into the SDK's PreToolUseHookSpecificOutput shape.
    # The backend looks up the active run, applies approval_policy,
    # emits ``approval_request`` over SSE, awaits ``/feedback approve|
    # deny``, and returns the decision — same plumbing as the MCP
    # route, just reached via HTTP rather than the in-process call.
    backend_url = _read_str("OPENCRAIG_BACKEND_URL")
    backend_token = _read_str("OPENCRAIG_API_TOKEN") or mcp_token

    hooks_cfg: dict = {}
    if backend_url and backend_token:
        try:
            import httpx as _httpx  # type: ignore[import-not-found]
        except ImportError:
            _httpx = None  # type: ignore[assignment]

        async def _on_pre_tool(input_data, tool_use_id, context):
            if _httpx is None:
                return {}
            tool_name = input_data.get("tool_name") or ""
            tool_input = input_data.get("tool_input") or {}
            url = f"{backend_url.rstrip('/')}/api/v1/agent/internal/pre_tool_use"
            try:
                async with _httpx.AsyncClient(timeout=620.0) as client:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {backend_token}"},
                        json={
                            "tool_name": tool_name,
                            "tool_input": tool_input,
                            "tool_use_id": str(tool_use_id or ""),
                        },
                    )
                if resp.status_code != 200:
                    # Fail-open on backend errors — denying every tool
                    # because the approval endpoint had a hiccup would
                    # be the worst kind of broken. Operators see the
                    # 5xx in backend logs.
                    return {}
                body = resp.json()
                decision = body.get("decision", "allow")
                if decision == "deny":
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                body.get("reason") or "denied by user"
                            ),
                        }
                    }
                return {}
            except Exception:
                # Network blip / timeout / unparseable response — same
                # fail-open rationale as above.
                return {}

        hooks_cfg = {
            "PreToolUse": [
                sdk.HookMatcher(matcher=".*", hooks=[_on_pre_tool])
            ],
        }

    return sdk.ClaudeAgentOptions(
        model=_read_str("OPENCRAIG_MODEL") or None,
        system_prompt=_read_str("OPENCRAIG_SYSTEM_PROMPT") or None,
        mcp_servers=mcp_servers,
        # Inside the sandbox we WANT Read / Edit / Bash / Glob / Grep —
        # they operate on /workspace which is the bind-mounted user
        # workspace. ``allowed_tools`` left default so the SDK's full
        # built-in set is available; MCP tools fan in as ``mcp__name__*``.
        cwd=os.getcwd(),
        # Stay on ``bypassPermissions`` — the SDK's own ``default``
        # permission flow has its own ideas about which paths the
        # agent may touch and refused mkdir under /workspace in
        # round-7 Task R. Per the Claude Code hooks docs, PreToolUse
        # hooks fire under ``bypassPermissions`` too — they're the
        # operator's gate, the SDK's built-in flow is a separate
        # layer that we don't want active.
        permission_mode="bypassPermissions",
        hooks=hooks_cfg or None,
        max_turns=_read_int("OPENCRAIG_MAX_TURNS", 90),
        include_partial_messages=True,
        thinking=thinking_cfg,
        # Don't bleed the operator's filesystem skills / CLAUDE.md
        # into tenant chats (the container is per-user but the SDK's
        # default ``setting_sources`` would still load /root/.claude/
        # if it existed in the image).
        setting_sources=None,
    )


def _emit_message_events(
    msg, sdk, tool_t0: dict, tool_name: dict, streamed_indices: set[int]
) -> str | None:
    """Translate one SDK message into zero-or-more JSONL events on
    stdout. Returns the final answer text if the message is a
    ResultMessage; ``None`` otherwise.

    Tool events are extracted from message content (ToolUseBlock in
    AssistantMessage, ToolResultBlock in UserMessage) so the same
    mapping works for both this in-container path and the in-process
    backend runtime — no PreToolUse / PostToolUse hooks needed.
    """
    final_text: str | None = None

    if isinstance(msg, sdk.AssistantMessage):
        # Whole-message blocks land here AFTER per-token StreamEvent
        # deltas. Without dedup, the answer body emits twice (once
        # per delta, once on the wrap-up TextBlock). Skip blocks at
        # indices we already streamed via StreamEvent; emit only
        # blocks that arrived without partial deltas (non-streaming
        # providers). Mirror of api/agent/claude_runtime.py.
        for i, block in enumerate(msg.content):
            if isinstance(block, sdk.ToolUseBlock):
                # Tool-use blocks always emit — they're not the
                # streamed-text shape (a tool call doesn't reach us
                # via content_block_delta).
                tool_t0[block.id] = time.time()
                tool_name[block.id] = block.name
                emit({
                    "kind": "tool_start",
                    "id": block.id,
                    "tool": block.name,
                    "params": dict(block.input or {}),
                })
                continue
            if i in streamed_indices:
                continue
            if isinstance(block, sdk.ThinkingBlock):
                if block.thinking:
                    emit({"kind": "thinking", "text": block.thinking})
            elif isinstance(block, sdk.TextBlock):
                if block.text:
                    emit({"kind": "answer_delta", "text": block.text})
        # Reset for the next assistant message — each turn opens a
        # fresh content_block sequence.
        streamed_indices.clear()
        # Per-turn usage emit (round-6 Bug 12 fix). AssistantMessage.usage
        # carries this turn's input/output tokens. The previous design
        # only emitted usage at ResultMessage (session end), which let
        # a single agent run blow well past its budget before the
        # backend's budget check could see the cumulative total. Emit
        # per turn with ``incremental=True`` so the backend accumulator
        # picks them up and the soft/hard warnings can actually trip
        # before the next tool call goes out.
        msg_usage = getattr(msg, "usage", None)
        if isinstance(msg_usage, dict):
            tin = int(msg_usage.get("input_tokens") or 0)
            tout = int(msg_usage.get("output_tokens") or 0)
            if tin or tout:
                emit({
                    "kind": "usage",
                    "input_tokens": tin,
                    "output_tokens": tout,
                    "incremental": True,
                })
    elif isinstance(msg, sdk.UserMessage):
        # Tool results land here. The SDK delivers ToolResultBlock
        # in the user-side replay of the conversation; we fan it
        # out as tool_end.
        content = getattr(msg, "content", None) or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, sdk.ToolResultBlock):
                    cid = block.tool_use_id or ""
                    t0 = tool_t0.pop(cid, None)
                    latency_ms = (
                        int((time.time() - t0) * 1000) if t0 is not None else 0
                    )
                    name = tool_name.pop(cid, "") or ""
                    summary = _summarise_tool_result(block.content)
                    # Full stringified output (capped) — without it the
                    # frontend's expandable ToolChip shows the input
                    # parameters but no Output / Matches / Body block,
                    # which made tool calls feel useless to expand.
                    # Mirror of api/agent/claude_runtime.py's
                    # PostToolUse hook so both runtime paths ship the
                    # same wire shape.
                    output = _stringify_tool_result(block.content)
                    # Failure flag — SDK populates ``is_error`` on
                    # ToolResultBlock when the tool's result block
                    # represents a failure (non-zero Bash exit, file
                    # not found, MCP tool raised, …). Drives the
                    # red-headline state on the frontend ToolChip.
                    is_error = bool(getattr(block, "is_error", False))
                    if not is_error and isinstance(summary, dict) and summary.get("error"):
                        is_error = True
                    emit({
                        "kind": "tool_end",
                        "id": cid,
                        "tool": name,
                        "latency_ms": latency_ms,
                        "result_summary": summary,
                        "output": output,
                        "is_error": is_error,
                    })
    elif isinstance(msg, sdk.StreamEvent):
        ev = msg.event or {}
        if ev.get("type") == "content_block_delta":
            # Track which content_block indices were emitted via
            # StreamEvent so the AssistantMessage wrap-up handler
            # can skip them (otherwise the answer body emits twice).
            idx = ev.get("index")
            if isinstance(idx, int):
                streamed_indices.add(idx)
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                t = delta.get("text") or ""
                if t:
                    emit({"kind": "answer_delta", "text": t})
            elif delta.get("type") == "thinking_delta":
                t = delta.get("thinking") or ""
                if t:
                    emit({"kind": "thinking", "text": t})
    elif isinstance(msg, sdk.ResultMessage):
        # Final turn boundary — emit usage so the chat route can
        # persist input/output_tokens on the assistant Message
        # (powers the frontend's context-window ring). ``usage``
        # is an Anthropic-shaped dict on the SDK's ResultMessage;
        # may be None for non-Anthropic providers, in which case
        # we just don't emit (frontend falls back to "no data").
        final_text = msg.result or ""
        usage = getattr(msg, "usage", None) or {}
        if isinstance(usage, dict) and (usage.get("input_tokens") or usage.get("output_tokens")):
            emit({
                "kind": "usage",
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            })

    return final_text


# Per-call tool-output cap. Keeps the JSONL line + downstream SSE
# event small enough to land in DB without bloating ``agent_trace_json``.
# Mirror of api/agent/claude_runtime.py::_TOOL_OUTPUT_MAX.
_TOOL_OUTPUT_MAX = 8192


def _stringify_tool_result(content) -> str:
    """Coerce a ToolResultBlock.content (str | list[ContentBlock] | None)
    into a length-capped string the frontend ToolChip can render
    verbatim (Bash stdout, Glob match list, search hit JSON, …).

    Lists of content-blocks (the MCP shape: ``[{type:"text", text:"..."}]``)
    get their text concatenated; dicts go through ``json.dumps``;
    everything longer than ``_TOOL_OUTPUT_MAX`` truncates with a
    chars-truncated marker so the frontend can display a useful
    preview without exceeding the trace JSON's practical ceiling.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        s = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    # Non-text blocks (image / json) — stringify the
                    # whole block so the user can see what came back.
                    try:
                        parts.append(json.dumps(block, ensure_ascii=False))
                    except Exception:
                        parts.append(str(block))
            else:
                parts.append(str(block))
        s = "\n".join(parts)
    elif isinstance(content, dict):
        try:
            s = json.dumps(content, ensure_ascii=False, default=str, indent=2)
        except Exception:
            s = str(content)
    else:
        s = str(content)
    if len(s) > _TOOL_OUTPUT_MAX:
        s = s[:_TOOL_OUTPUT_MAX] + f"\n…[+{len(s) - _TOOL_OUTPUT_MAX} chars truncated]"
    return s


def _summarise_tool_result(content) -> dict:
    """Boil down a ToolResultBlock.content (str | list[dict] | None)
    into the small summary dict the SSE consumer expects. Keys we
    know about (hit_count / entity_count / chunk_count / error) are
    preserved; everything else gets a truncated text fallback."""
    if content is None:
        return {}
    if isinstance(content, str):
        return {"text": content[:200]}
    if isinstance(content, list):
        # MCP tool results come as a list of content blocks; flatten
        # text from any text-type blocks for the summary.
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        if parts:
            joined = " ".join(parts)
            # Try to extract a structured summary from a JSON-shaped
            # result (our MCP tools return JSON in text blocks).
            for p in parts:
                try:
                    parsed = json.loads(p)
                    if isinstance(parsed, dict):
                        keys = ("hit_count", "entity_count",
                                "chunk_count", "error")
                        sub = {k: parsed[k] for k in keys if k in parsed}
                        if sub:
                            return sub
                except Exception:
                    pass
            return {"text": joined[:200]}
    if isinstance(content, dict):
        keys = ("hit_count", "entity_count", "chunk_count", "error")
        sub = {k: content[k] for k in keys if k in content}
        if sub:
            return sub
        return {"text": str(content)[:200]}
    return {"text": str(content)[:200]}


async def _drive(
    user_message: str,
    history: list[dict],
    sdk,
    extra_user_blocks: list[dict] | None = None,
) -> tuple[str, int]:
    """Run one turn through the SDK's async generator, emitting
    JSONL events. Returns (final_text, num_turns).

    ``extra_user_blocks`` carries pre-built Anthropic content blocks
    (image / document) the user attached this turn; the prompt
    builder prepends them to the user message body so the model
    sees the visual context before the text question."""
    options = _build_options(
        sdk,
        mcp_url=_read_str("OPENCRAIG_MCP_URL"),
        mcp_token=_read_str("OPENCRAIG_MCP_TOKEN"),
    )

    # The SDK's streaming-input mode treats EVERY ``type:"user"``
    # dict as a fresh prompt the agent must respond to. Replaying
    # history as a series of user/assistant pairs causes the SDK
    # to re-answer each historical user turn (observed: the
    # bundled CLI raises "Stream closed" / "tool_use_id in J"
    # under that mode). Fold history into a single ``type:"user"``
    # whose text body includes the prior turns as plain context —
    # SDK then makes exactly one LLM call.
    #
    # Also: the bundled claude binary iterates ``message.content``
    # looking for tool-use blocks. A plain string makes JS iterate
    # characters and the ``in`` operator throws "J is not an
    # Object". Wrap as a single-element content-block array.
    #
    # Mirror of api/agent/claude_runtime.py — keep the two in sync
    # whenever either changes.
    async def _prompt_stream():
        if history:
            lines = ["[prior conversation]"]
            for m in history:
                role = m.get("role")
                content = m.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content:
                    speaker = "User" if role == "user" else "Assistant"
                    lines.append(f"{speaker}: {content}")
            lines.append("[end prior conversation]")
            lines.append("")
            lines.append(user_message)
            composed = "\n".join(lines)
        else:
            composed = user_message
        # Multimodal blocks come BEFORE the text — Anthropic's docs
        # recommend image/document content live above the question
        # the model is supposed to answer with them as context.
        content_blocks: list[dict] = []
        if extra_user_blocks:
            content_blocks.extend(extra_user_blocks)
        content_blocks.append({"type": "text", "text": composed})
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": content_blocks,
            },
            "parent_tool_use_id": None,
            "session_id": "",
        }

    final_text = ""
    iterations = 0
    tool_t0: dict = {}
    tool_name: dict = {}
    # Track which content_block indices were streamed via
    # StreamEvent — see ``_emit_message_events``.
    streamed_indices: set[int] = set()

    async for msg in sdk.query(prompt=_prompt_stream(), options=options):
        ft = _emit_message_events(msg, sdk, tool_t0, tool_name, streamed_indices)
        if ft is not None:
            final_text = ft
            iterations = getattr(msg, "num_turns", 0) or 0

    return final_text, iterations


def main() -> int:
    user_message = _read_str("OPENCRAIG_USER_MESSAGE")
    if not user_message:
        emit({
            "kind": "error",
            "type": "MissingInput",
            "message": "OPENCRAIG_USER_MESSAGE env var is required",
        })
        return 2

    # Chdir BEFORE importing the SDK so the bundled CLI sees the
    # right cwd at startup.
    _chdir_to_cwd_or_workdir()

    try:
        import claude_agent_sdk as sdk  # type: ignore
    except ImportError as e:
        emit({
            "kind": "error",
            "type": "SDKNotInstalled",
            "message": f"claude-agent-sdk not importable in container: {e}",
        })
        return 3

    history = _read_history()
    extra_user_blocks = _read_extra_user_blocks()

    try:
        final_text, iterations = asyncio.run(
            _drive(user_message, history, sdk, extra_user_blocks)
        )
    except sdk.CLINotFoundError as e:
        traceback.print_exc(file=sys.stderr)
        emit({
            "kind": "error",
            "type": "CLINotFound",
            "message": f"claude binary not found: {e}",
        })
        return 4
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        emit({
            "kind": "error",
            "type": type(e).__name__,
            "message": f"agent failed: {e}",
        })
        return 5

    emit({
        "kind": "done",
        "final_text": final_text,
        "iterations": iterations,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
