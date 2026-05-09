"""
OpenCraig agent-turn entrypoint, lives inside the sandbox container.

The backend's ``HermesContainerRunner`` invokes this script via
``docker exec <user-container> python /opt/opencraig/opencraig_run_turn.py``
once per chat turn. The script:

  1. Reads the user message + conversation history + agent config
     from environment variables.
  2. Chdirs into the cwd_path subfolder of /workdir before importing
     the SDK so the agent's Read / Edit / Bash / Glob / Grep tools
     start at the right place (they capture os.getcwd() at startup).
  3. Drives one turn of the Claude Agent SDK loop. Built-in tools
     are ENABLED here because this IS the sandbox — the bind-mount
     limits filesystem reach to /workdir = the user's private
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
    OPENCRAIG_CWD           — folder path INSIDE /workdir/ to chdir
                              into before invoking the agent (e.g.
                              ``/sales/2025``). Empty / unset = stay
                              at /workdir root.
    OPENCRAIG_MCP_URL       — backend MCP server URL (e.g.
                              ``http://backend:8000/api/v1/mcp``).
                              Optional; if absent the agent runs
                              without MCP tools (built-ins still
                              available).
    OPENCRAIG_MCP_TOKEN     — bearer token for the MCP server.
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


def _chdir_to_cwd_or_workdir() -> None:
    """Move into the agent's working directory before importing the
    SDK. The bundled CLI captures os.getcwd() for its built-in shell
    tools' starting point; setting it here makes OPENCRAIG_CWD the
    agent's "current folder" without any further wiring inside the
    SDK itself.

    Order of preference:
        1. ``/workdir`` + OPENCRAIG_CWD  (folder-as-cwd path)
        2. ``/workdir``                  (no folder bound)
        3. fall back to wherever we are  (dev / test outside container)
    """
    cwd_rel = (os.environ.get("OPENCRAIG_CWD") or "").strip()
    if cwd_rel and not cwd_rel.startswith("/"):
        cwd_rel = "/" + cwd_rel
    cwd_rel = cwd_rel.rstrip("/")

    base = "/workdir"
    target = base + cwd_rel if cwd_rel else base

    if not os.path.isdir(target):
        # Auto-create the cwd folder if the user gave us one that
        # doesn't yet exist on disk. Same affordance the Workspace
        # UI's "create folder" gives, but here driven by the chat
        # opening in a not-yet-materialised path. Falls back to
        # base if even ``/workdir`` is missing (test / dev contexts).
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

    return sdk.ClaudeAgentOptions(
        model=_read_str("OPENCRAIG_MODEL") or None,
        system_prompt=_read_str("OPENCRAIG_SYSTEM_PROMPT") or None,
        mcp_servers=mcp_servers,
        # Inside the sandbox we WANT Read / Edit / Bash / Glob / Grep —
        # they operate on /workdir which is the bind-mounted user
        # workspace. ``allowed_tools`` left default so the SDK's full
        # built-in set is available; MCP tools fan in as ``mcp__name__*``.
        cwd=os.getcwd(),
        permission_mode="bypassPermissions",
        max_turns=_read_int("OPENCRAIG_MAX_TURNS", 90),
        include_partial_messages=True,
        # Don't bleed the operator's filesystem skills / CLAUDE.md
        # into tenant chats (the container is per-user but the SDK's
        # default ``setting_sources`` would still load /root/.claude/
        # if it existed in the image).
        setting_sources=None,
    )


def _emit_message_events(msg, sdk, tool_t0: dict, tool_name: dict) -> str | None:
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
        for block in msg.content:
            if isinstance(block, sdk.ThinkingBlock):
                # Whole-message thinking blocks. Token-level thinking
                # also surfaces via StreamEvent below — partial-stream
                # de-dup is handled by skipping TextBlock when its
                # content was already streamed (see seen_partial).
                emit({"kind": "thinking", "text": block.thinking})
            elif isinstance(block, sdk.ToolUseBlock):
                tool_t0[block.id] = time.time()
                tool_name[block.id] = block.name
                emit({
                    "kind": "tool_start",
                    "id": block.id,
                    "tool": block.name,
                    "params": dict(block.input or {}),
                })
            elif isinstance(block, sdk.TextBlock):
                # Whole-text fall-through emitted only when partial
                # streaming didn't produce deltas for this block. The
                # caller marks _seen_partial[id(block)]=True when a
                # delta event lands. Without that signal we'd
                # double-emit the answer body.
                if block.text and not msg.uuid:  # uuid set on stream-paired block
                    emit({"kind": "answer_delta", "text": block.text})
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
                    emit({
                        "kind": "tool_end",
                        "id": cid,
                        "tool": name,
                        "latency_ms": latency_ms,
                        "result_summary": summary,
                    })
    elif isinstance(msg, sdk.StreamEvent):
        ev = msg.event or {}
        if ev.get("type") == "content_block_delta":
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
        final_text = msg.result or ""

    return final_text


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


async def _drive(user_message: str, history: list[dict], sdk) -> tuple[str, int]:
    """Run one turn through the SDK's async generator, emitting
    JSONL events. Returns (final_text, num_turns)."""
    options = _build_options(
        sdk,
        mcp_url=_read_str("OPENCRAIG_MCP_URL"),
        mcp_token=_read_str("OPENCRAIG_MCP_TOKEN"),
    )

    async def _prompt_stream():
        for m in history or []:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content:
                yield {
                    "type": "user" if role == "user" else "assistant",
                    "message": {"role": role, "content": content},
                    "parent_tool_use_id": None,
                    "session_id": "",
                }
        yield {
            "type": "user",
            "message": {"role": "user", "content": user_message},
            "parent_tool_use_id": None,
            "session_id": "",
        }

    final_text = ""
    iterations = 0
    tool_t0: dict = {}
    tool_name: dict = {}

    async for msg in sdk.query(prompt=_prompt_stream(), options=options):
        ft = _emit_message_events(msg, sdk, tool_t0, tool_name)
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

    try:
        final_text, iterations = asyncio.run(_drive(user_message, history, sdk))
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
