"""
OpenCraig agent-turn entrypoint, lives inside the sandbox container.

The backend's ``HermesContainerRunner`` invokes this script via
``docker exec <user-container> python /opt/opencraig/opencraig_run_turn.py``
once per chat turn. The script:

  1. Reads the user message + conversation history + agent config
     from environment variables / stdin (the backend chooses).
  2. Constructs a Hermes ``AIAgent`` with full built-in toolsets
     enabled (this IS the sandbox — bash / edit / grep operate on
     the bind-mounted ``/workdir/`` so the agent can actually
     touch project files).
  3. Translates each per-event callback into a single JSONL line
     on stdout. The backend reads stdout line-by-line and converts
     to SSE events for the frontend.

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

Why an explicit script (vs. ``hermes chat -q``):
    The CLI's quiet mode emits only the final text; everything
    interesting (tool calls, deltas) is suppressed. We need
    structured events for the SSE trace. Library mode + this
    thin script + JSONL stdout is the cleanest path.

Env vars the backend sets per turn:
    OPENCRAIG_USER_MESSAGE  — the user's question (required)
    OPENCRAIG_HISTORY       — JSON array of prior {role, content}
                              messages (default: [])
    OPENCRAIG_MODEL         — model name (e.g. ``gpt-4o``,
                              ``claude-3-5-sonnet-...``)
    OPENCRAIG_MAX_TURNS     — agent iteration cap (default 90)
    OPENCRAIG_SYSTEM_PROMPT — optional ephemeral system prompt
    OPENCRAIG_CWD           — folder path INSIDE /workdir/ to chdir
                              into before invoking the agent (e.g.
                              ``/sales/2025``). Empty / unset = stay
                              at /workdir root. The agent's bash /
                              edit / grep tools then operate inside
                              this folder, which is also the default
                              destination for any artifacts it writes.
    OPENAI_BASE_URL         — points at backend's LLM proxy
    OPENAI_API_KEY          — session token (LLM proxy resolves
                              real provider key on the backend)
"""
from __future__ import annotations

import json
import os
import sys
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


def _make_callbacks() -> dict:
    """Build the per-event callback bundle AIAgent expects.

    Each callback is best-effort about argument shape: AIAgent
    versions may add positional args, so we accept ``*args, **kwargs``
    and pull what we need defensively. Failures inside a callback
    log to stderr but never raise — a malformed event must not kill
    the whole turn.
    """

    def _on_tool_start(*args, **kwargs):
        try:
            tool = kwargs.get("tool") or (args[0] if args else "")
            params = kwargs.get("params") or (args[1] if len(args) > 1 else {})
            call_id = kwargs.get("call_id") or kwargs.get("id") or ""
            emit({
                "kind": "tool_start",
                "id": str(call_id),
                "tool": str(tool),
                "params": params if isinstance(params, dict) else {},
            })
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _on_tool_complete(*args, **kwargs):
        try:
            tool = kwargs.get("tool") or (args[0] if args else "")
            call_id = kwargs.get("call_id") or kwargs.get("id") or ""
            latency_ms = int(kwargs.get("latency_ms") or 0)
            summary = kwargs.get("result_summary") or kwargs.get("result") or {}
            if not isinstance(summary, dict):
                summary = {"text": str(summary)[:200]}
            emit({
                "kind": "tool_end",
                "id": str(call_id),
                "tool": str(tool),
                "latency_ms": latency_ms,
                "result_summary": summary,
            })
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _on_thinking(*args, **kwargs):
        try:
            text = kwargs.get("text") or (args[0] if args else "")
            if text:
                emit({"kind": "thinking", "text": str(text)})
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _on_stream_delta(*args, **kwargs):
        try:
            text = kwargs.get("text") or kwargs.get("delta") or (
                args[0] if args else ""
            )
            if text:
                emit({"kind": "answer_delta", "text": str(text)})
        except Exception:
            traceback.print_exc(file=sys.stderr)

    return {
        "tool_start_callback": _on_tool_start,
        "tool_complete_callback": _on_tool_complete,
        "thinking_callback": _on_thinking,
        "stream_delta_callback": _on_stream_delta,
    }


def _chdir_to_cwd_or_workdir() -> None:
    """Move into the agent's working directory before importing /
    instantiating AIAgent. Hermes' built-in bash / edit / grep
    tools use ``os.getcwd()`` as their starting point; setting it
    here makes the cwd_path the agent's "current folder" without
    any further wiring inside Hermes itself.

    Order of preference:
        1. ``/workdir`` + OPENCRAIG_CWD  (folder-as-cwd path)
        2. ``/workdir``                  (no folder bound)
        3. fall back to wherever we are  (dev / test outside container)
    """
    cwd_rel = (os.environ.get("OPENCRAIG_CWD") or "").strip()
    # Normalise: ensure leading "/", drop trailing
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
        # Best-effort — log and continue at the original cwd. The
        # agent will still work; just from the wrong directory.
        traceback.print_exc(file=sys.stderr)


def main() -> int:
    user_message = _read_str("OPENCRAIG_USER_MESSAGE")
    if not user_message:
        emit({
            "kind": "error",
            "type": "MissingInput",
            "message": "OPENCRAIG_USER_MESSAGE env var is required",
        })
        return 2

    # Chdir BEFORE importing run_agent / AIAgent — the agent
    # captures CWD at construction time for some of its built-in
    # tool initialisers, so changing it later is too late.
    _chdir_to_cwd_or_workdir()

    try:
        from run_agent import AIAgent
    except ImportError as e:
        emit({
            "kind": "error",
            "type": "HermesNotInstalled",
            "message": f"hermes-agent not importable in container: {e}",
        })
        return 3

    agent_kwargs: dict = {
        "base_url": _read_str("OPENAI_BASE_URL") or None,
        "api_key": _read_str("OPENAI_API_KEY") or None,
        "model": _read_str("OPENCRAIG_MODEL", "gpt-4o-mini"),
        "max_iterations": _read_int("OPENCRAIG_MAX_TURNS", 90),
        # Quiet for programmatic use — no banner / spinner / TUI.
        "quiet_mode": True,
        # All built-in toolsets ENABLED. This script runs INSIDE the
        # sandbox container, so Read / Edit / Bash / Glob / Grep are
        # safe + actually useful (they operate on /workdir/ which
        # the backend bind-mounts to the user's project folder).
        # The backend's in-process Hermes wrapper takes the OPPOSITE
        # stance (toolsets=[]) because there it'd touch the
        # backend's filesystem.
        "enabled_toolsets": None,
        # OpenCraig owns conversation history (Conversation table);
        # don't let Hermes write its own session storage to
        # ~/.hermes/sessions/ inside the container.
        "persist_session": False,
        "skip_context_files": True,
        "skip_memory": True,
    }
    sys_prompt = _read_str("OPENCRAIG_SYSTEM_PROMPT")
    if sys_prompt:
        agent_kwargs["ephemeral_system_prompt"] = sys_prompt

    agent_kwargs.update(_make_callbacks())

    try:
        agent = AIAgent(**agent_kwargs)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        emit({
            "kind": "error",
            "type": type(e).__name__,
            "message": f"AIAgent init failed: {e}",
        })
        return 4

    history = _read_history()

    try:
        result = agent.run_conversation(
            user_message,
            conversation_history=history,
        )
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        emit({
            "kind": "error",
            "type": type(e).__name__,
            "message": f"agent failed: {e}",
        })
        return 5

    final_text = ""
    iterations = 0
    if isinstance(result, dict):
        for k in ("response", "final_response", "text", "answer", "content"):
            v = result.get(k)
            if isinstance(v, str) and v:
                final_text = v
                break
        iter_v = result.get("iterations")
        if isinstance(iter_v, int):
            iterations = iter_v

    emit({
        "kind": "done",
        "final_text": final_text,
        "iterations": iterations,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
