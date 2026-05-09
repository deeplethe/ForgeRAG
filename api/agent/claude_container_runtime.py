"""
Container-side Claude SDK runtime — counterpart to ``ClaudeRuntime``.

Where ``ClaudeRuntime`` (api/agent/claude_runtime.py) drives the SDK
in-process inside the FastAPI worker, this runner spawns the
entrypoint script (``/opt/opencraig/opencraig_run_turn.py``) inside
the user's sandbox container via ``docker exec`` and streams the
JSONL events the entrypoint emits on stdout. Same public API
shape (``ClaudeTurnConfig`` in, ``ClaudeTurnResult`` out, per-event
callback fan-out) so the chat route can switch transparently.

Why a container-side runner exists at all:

  In-process the Claude Agent SDK runs with ``enabled_toolsets=[]`` — its
  built-in Read / Edit / Bash / Glob / Grep would otherwise touch
  the BACKEND's filesystem, which is a hard escape risk. With those
  disabled the agent can only use MCP tools (search / KG / library
  / artifacts) — fine for pure Q&A, useless when the user opens a
  Workspace folder and expects the agent to actually read / edit
  files there.

  In-container the Claude Agent SDK runs with full toolsets ENABLED — its bash /
  edit / grep operate on ``/workdir/`` which the SandboxManager
  bind-mounts to the user's project folder. That's the sandbox.
  The cost is per-turn ``docker exec`` overhead (~50–200 ms on a
  warm container) and a network hop for MCP calls; the win is a
  Workspace that actually does work.

Selection logic lives in ``api/routes/claude_chat.py`` — when a
chat is bound to a project AND a SandboxManager is available, use
this runner; otherwise fall back to ``ClaudeRuntime`` (in-process,
toolsets disabled, MCP-only).

Wire format (the JSONL the entrypoint emits → the events this
runner fans out via ``on_event``):

    {"kind": "thinking",     "text": "..."}
    {"kind": "tool_start",   "id": "...", "tool": "...",
                              "params": {...}}
    {"kind": "tool_end",     "id": "...", "tool": "...",
                              "latency_ms": N,
                              "result_summary": {...}}
    {"kind": "answer_delta", "text": "..."}
    {"kind": "done",         "final_text": "...",
                              "iterations": N}
    {"kind": "error",        "type": "...", "message": "..."}

These are identical to ``ClaudeRuntime``'s event vocabulary so the
chat route's SSE translation layer doesn't need a second mapping.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Iterator
from typing import Any

from .claude_runtime import (
    ClaudeTurnConfig,
    ClaudeTurnResult,
    _evt_done,
    _evt_error,
)

log = logging.getLogger(__name__)


# Stable path the Day 1 Dockerfile copies the entrypoint to. The
# backend hardcodes this; if the Dockerfile changes the path,
# Day 1's structural test catches it.
ENTRYPOINT_PATH = "/opt/opencraig/opencraig_run_turn.py"


class SandboxUnavailableError(RuntimeError):
    """Raised when the runner is constructed without a usable
    SandboxManager. The chat route catches this and falls back to
    the in-process runtime — better than 5xx-ing the user."""


class ClaudeContainerRunner:
    """Drive a the SDK turn inside the user's sandbox container.

    Stateless across turns: instantiate once per chat route, reuse
    across requests. Conversation history travels through ``run_turn``
    args, not stored on the instance.

    Construction:
        ``sandbox`` is a live ``SandboxManager``. The runner uses it
        for two things: ``ensure_container_for_user`` (so the user
        has a running container), and ``touch`` (so the idle reaper
        doesn't kill an actively-used container mid-conversation).
    """

    def __init__(self, sandbox):
        if sandbox is None:
            raise SandboxUnavailableError(
                "ClaudeContainerRunner requires a live SandboxManager. "
                "If you're running in a deployment without Docker, the "
                "chat route should have selected the in-process "
                "ClaudeRuntime instead."
            )
        self.sandbox = sandbox

    def run_turn(
        self,
        user_message: str,
        *,
        config: ClaudeTurnConfig,
        principal_user_id: str,
        cwd_path: str | None = None,
        conversation_history: list[dict] | None = None,
        on_event: callable | None = None,
    ) -> ClaudeTurnResult:
        """Spawn the entrypoint inside ``principal_user_id``'s
        container, parse JSONL from stdout, fan events out via
        ``on_event``, return a ClaudeTurnResult.

        ``cwd_path``: folder path WITHIN the user's workdir tree
        (e.g. ``"/sales/2025"``) the agent should chdir into
        before working. Mapped to the in-container path
        ``/workdir<cwd_path>``. Empty / None → agent works at
        ``/workdir`` root (no folder context — pure Q&A).

        Synchronous: blocks until the entrypoint exits. The chat
        route uses ``stream_turn_container`` instead so the SSE
        generator can interleave waiting on docker stdout with
        flushing events to the client.
        """
        emit = on_event or (lambda _evt: None)

        try:
            container = self.sandbox.ensure_container_for_user(
                principal_user_id,
                # Folder-as-cwd: no per-project mounts. The user's
                # entire workdir tree is mounted at /workdir/ once;
                # the entrypoint chdirs into the cwd_path subfolder
                # before invoking AIAgent.
                owned_project_ids=(),
            )
        except Exception as e:
            log.exception(
                "claude_container: ensure_container_for_user failed user=%s",
                principal_user_id,
            )
            emit(_evt_error(f"sandbox start failed: {type(e).__name__}",
                            type_=type(e).__name__))
            raise

        env = self._build_env(
            user_message=user_message,
            config=config,
            conversation_history=conversation_history,
            cwd_path=cwd_path,
        )

        final_text = ""
        iterations = 0
        try:
            for evt in self._iter_jsonl_events(container.container_id, env):
                emit(evt)
                if evt.get("kind") == "done":
                    final_text = evt.get("final_text", "") or final_text
                    iter_v = evt.get("iterations")
                    if isinstance(iter_v, int):
                        iterations = iter_v
        except Exception as e:
            log.exception(
                "claude_container: stream pump raised user=%s",
                principal_user_id,
            )
            emit(_evt_error(f"agent failed: {type(e).__name__}",
                            type_=type(e).__name__))
            raise
        finally:
            # Reset idle timer regardless of success — the user just
            # interacted, even a failed turn means "this user is
            # still active, don't kill their container".
            try:
                self.sandbox.touch(principal_user_id)
            except Exception:
                log.exception("claude_container: touch failed")

        return ClaudeTurnResult(
            final_text=final_text,
            history=[],  # container doesn't ship updated history;
                          # the route reconstructs from its DB
            iterations=iterations,
            raw={},
        )

    # ---------------------------------------------------------------
    # Internals — env construction + stdout iteration
    # ---------------------------------------------------------------

    @staticmethod
    def _build_env(
        *,
        user_message: str,
        config: ClaudeTurnConfig,
        conversation_history: list[dict] | None,
        cwd_path: str | None = None,
    ) -> dict[str, str]:
        """Pack everything the entrypoint reads from environment.

        We use env vars rather than CLI args because:
          * env values are arbitrary length (CLI has shell-quote
            quirks, especially for multi-line user messages with
            quotes / newlines)
          * docker exec env injection is one well-understood path
          * the entrypoint's ``_read_str`` / ``_read_history``
            helpers expect this exact shape
        """
        env: dict[str, str] = {
            "OPENCRAIG_USER_MESSAGE": user_message,
            "OPENCRAIG_HISTORY": json.dumps(
                conversation_history or [], ensure_ascii=False,
            ),
            "OPENCRAIG_MODEL": config.model,
            "OPENCRAIG_MAX_TURNS": str(config.max_iterations),
        }
        if config.base_url:
            env["OPENAI_BASE_URL"] = config.base_url
        if config.api_key:
            env["OPENAI_API_KEY"] = config.api_key
        if config.system_message:
            env["OPENCRAIG_SYSTEM_PROMPT"] = config.system_message
        # Normalise cwd_path: leading "/", no trailing. Empty →
        # don't pass the env var at all so the entrypoint stays
        # at ``/workdir`` root.
        if cwd_path:
            normalised = cwd_path.strip()
            if normalised and not normalised.startswith("/"):
                normalised = "/" + normalised
            normalised = normalised.rstrip("/")
            if normalised:
                env["OPENCRAIG_CWD"] = normalised
        return env

    def _iter_jsonl_events(
        self,
        container_id: str,
        env: dict[str, str],
    ) -> Iterator[dict]:
        """Run the entrypoint via docker exec, parse stdout as
        line-delimited JSON, yield each event dict.

        Bypasses ``SandboxBackend.exec`` (which buffers the entire
        output before returning) by going through the docker SDK's
        low-level ``exec_create`` + ``exec_start(stream=True)``
        path directly. The Protocol abstraction is preserved for
        future backends; this single call site is the documented
        exception. Once a non-Docker backend (BoxLite / k8s) ships,
        it grows its own ``exec_streaming`` method and the runner
        chooses by isinstance check.

        Lines that fail to JSON-parse are logged + skipped — a
        single corrupt line shouldn't kill a whole turn.
        """
        client = self.sandbox.backend._docker()
        api = client.api  # low-level API client

        cmd = ["python", ENTRYPOINT_PATH]
        exec_id = api.exec_create(
            container_id,
            cmd,
            environment=env,
            stdout=True,
            stderr=True,
            tty=False,
        )["Id"]
        stream = api.exec_start(exec_id, stream=True, demux=False)

        buffer = b""
        for chunk in stream:
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                evt = self._parse_line(line)
                if evt is not None:
                    yield evt

        # Drain any trailing line that didn't end in \n (entrypoint
        # always flushes with \n, but be defensive).
        if buffer.strip():
            evt = self._parse_line(buffer)
            if evt is not None:
                yield evt

    @staticmethod
    def _parse_line(line: bytes) -> dict | None:
        """Decode + JSON-parse one stdout line. Returns ``None`` on
        malformed input (logged at warning level — could be the
        entrypoint printing diagnostics to stdout instead of
        emitting events, which we want to notice but not crash on)."""
        try:
            decoded = line.decode("utf-8", errors="replace").strip()
        except Exception:
            log.exception("claude_container: line decode failed")
            return None
        if not decoded:
            return None
        try:
            evt = json.loads(decoded)
        except Exception:
            log.warning(
                "claude_container: non-JSON line on stdout (skipping): %r",
                decoded[:200],
            )
            return None
        if not isinstance(evt, dict):
            return None
        return evt


# ---------------------------------------------------------------------------
# Streaming helper — the chat route's actual entry point
# ---------------------------------------------------------------------------


def stream_turn_container(
    runner: ClaudeContainerRunner,
    user_message: str,
    *,
    config: ClaudeTurnConfig,
    principal_user_id: str,
    cwd_path: str | None = None,
    conversation_history: list[dict] | None = None,
) -> Iterator[dict]:
    """Sync generator the chat route iterates to push SSE events.

    Same thread + queue pattern as ``ClaudeRuntime.stream_turn`` —
    keep the wire format identical so the route's translation
    layer doesn't need a second mapping.
    """
    q: queue.Queue = queue.Queue()
    SENTINEL = object()

    def _on_event(evt: dict) -> None:
        try:
            q.put_nowait(evt)
        except Exception:
            log.exception("claude_container: queue put failed")

    def _worker() -> None:
        try:
            runner.run_turn(
                user_message,
                config=config,
                principal_user_id=principal_user_id,
                cwd_path=cwd_path,
                conversation_history=conversation_history,
                on_event=_on_event,
            )
        except Exception as e:
            try:
                q.put_nowait(
                    _evt_error(
                        f"agent failed: {type(e).__name__}",
                        type_=type(e).__name__,
                    )
                )
            except Exception:
                log.exception("claude_container: error-event put failed")
        finally:
            q.put(SENTINEL)

    t = threading.Thread(
        target=_worker, daemon=True, name="claude-container-turn"
    )
    t.start()
    while True:
        evt = q.get()
        if evt is SENTINEL:
            break
        yield evt
    t.join(timeout=5.0)
