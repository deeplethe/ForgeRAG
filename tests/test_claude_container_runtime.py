"""
Tests for ``ClaudeContainerRunner`` (Wave 2.5b Day 3-5).

Doesn't require Docker. Mocks the SandboxManager + the docker SDK
client so we can exercise:

  * env-var packing — every config field lands on the right
    OPENCRAIG_* / OPENAI_* env key for the entrypoint to read
  * JSONL stream parsing — multi-line stdout chunks split on
    ``\\n``, malformed lines logged + skipped, trailing partial
    lines drained
  * event fan-out via on_event — caller sees each parsed dict
  * SandboxManager touch on completion (idle reaper plumbing)
  * ``stream_turn_container`` worker-thread + queue pattern
  * SandboxUnavailableError when constructed without a sandbox

Day 8-9 will run the actual end-to-end exercise (real docker exec
into a built image hitting a real LLM); these tests pin the
contract the runner has with both sides of the wire.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.agent.claude_container_runtime import (
    ENTRYPOINT_PATH,
    ClaudeContainerRunner,
    SandboxUnavailableError,
    stream_turn_container,
)
from api.agent.claude_runtime import ClaudeTurnConfig


# ---------------------------------------------------------------------------
# Fake sandbox + fake docker SDK
# ---------------------------------------------------------------------------


def _config(**overrides) -> ClaudeTurnConfig:
    cfg = dict(
        model="gpt-4o",
        base_url="http://backend:8000/api/v1/llm/v1",
        api_key="bk-session-token",
        max_iterations=42,
    )
    cfg.update(overrides)
    return ClaudeTurnConfig(**cfg)


class _FakeAPI:
    """Stub for docker.APIClient — what's at ``client.api``."""

    def __init__(self, stream_chunks):
        self.stream_chunks = list(stream_chunks)
        self.exec_creates: list[dict] = []
        self.exec_starts: list[dict] = []

    def exec_create(self, container_id, cmd, *, environment, stdout, stderr, tty):
        self.exec_creates.append(
            {
                "container_id": container_id,
                "cmd": list(cmd),
                "environment": dict(environment),
                "stdout": stdout,
                "stderr": stderr,
                "tty": tty,
            }
        )
        return {"Id": "exec-fake-id"}

    def exec_start(self, exec_id, *, stream, demux):
        self.exec_starts.append(
            {"exec_id": exec_id, "stream": stream, "demux": demux}
        )
        # Return chunks one at a time as the SDK would.
        return iter(self.stream_chunks)


class _FakeBackend:
    def __init__(self, stream_chunks):
        self._client = SimpleNamespace(api=_FakeAPI(stream_chunks))

    def _docker(self):
        return self._client


class _FakeSandbox:
    """Stand-in for SandboxManager. Only the methods the runner
    actually calls — ensure_container_for_user / touch / backend.
    """

    def __init__(self, stream_chunks=()):
        self.backend = _FakeBackend(stream_chunks)
        self.ensure_calls: list[dict] = []
        self.touch_calls: list[str] = []
        self._container = SimpleNamespace(
            user_id="",
            container_id="container-fake-id",
            image="opencraig/sandbox:py3.13",
            name="fake-container",
            mounts=(),
        )

    def ensure_container_for_user(self, user_id, *, owned_project_ids):
        self.ensure_calls.append(
            {"user_id": user_id, "owned_project_ids": tuple(owned_project_ids)}
        )
        self._container.user_id = user_id
        return self._container

    def touch(self, user_id):
        self.touch_calls.append(user_id)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_runner_requires_sandbox():
    with pytest.raises(SandboxUnavailableError):
        ClaudeContainerRunner(None)


# ---------------------------------------------------------------------------
# Env packing — what the entrypoint reads on the other side
# ---------------------------------------------------------------------------


def test_env_includes_every_config_field():
    sb = _FakeSandbox(
        stream_chunks=[b'{"kind": "done", "final_text": "ok", "iterations": 1}\n']
    )
    runner = ClaudeContainerRunner(sb)
    runner.run_turn(
        "Hello agent",
        config=_config(
            model="claude-3-5-sonnet",
            base_url="http://gateway/llm",
            api_key="bk-abc",
            max_iterations=12,
            system_message="Be terse.",
        ),
        principal_user_id="u_alice",
        conversation_history=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
        ],
    )

    assert sb.backend._client.api.exec_creates, "exec_create was never called"
    env = sb.backend._client.api.exec_creates[0]["environment"]
    assert env["OPENCRAIG_USER_MESSAGE"] == "Hello agent"
    assert env["OPENCRAIG_MODEL"] == "claude-3-5-sonnet"
    assert env["OPENCRAIG_MAX_TURNS"] == "12"
    assert env["OPENCRAIG_SYSTEM_PROMPT"] == "Be terse."
    assert env["OPENAI_BASE_URL"] == "http://gateway/llm"
    assert env["OPENAI_API_KEY"] == "bk-abc"
    history = json.loads(env["OPENCRAIG_HISTORY"])
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey"},
    ]


def test_env_omits_optional_fields_when_unset():
    """Empty config fields shouldn't be passed as empty-string env
    vars — that subtly differs from "unset" for some readers
    (especially the AIAgent constructor's default-fallback logic
    which only kicks in when the env var isn't present at all)."""
    sb = _FakeSandbox(
        stream_chunks=[b'{"kind": "done", "final_text": "ok", "iterations": 0}\n']
    )
    runner = ClaudeContainerRunner(sb)
    runner.run_turn(
        "x",
        config=_config(base_url="", api_key="", system_message=None),
        principal_user_id="u_x",
    )
    env = sb.backend._client.api.exec_creates[0]["environment"]
    assert "OPENAI_BASE_URL" not in env
    assert "OPENAI_API_KEY" not in env
    assert "OPENCRAIG_SYSTEM_PROMPT" not in env


def test_history_default_is_empty_array():
    sb = _FakeSandbox(
        stream_chunks=[b'{"kind": "done", "final_text": "ok", "iterations": 0}\n']
    )
    runner = ClaudeContainerRunner(sb)
    runner.run_turn("x", config=_config(), principal_user_id="u_x")
    env = sb.backend._client.api.exec_creates[0]["environment"]
    assert env["OPENCRAIG_HISTORY"] == "[]"


def test_cwd_path_env_var_normalised():
    """The agent's chdir target is OPENCRAIG_CWD; the runner is
    the right place to enforce a normalised shape so the entrypoint
    can blindly trust the value (one source of truth for path
    handling)."""
    cases = [
        # input            expected env value (or absent)
        ("/sales/2025",     "/sales/2025"),
        ("sales/2025",      "/sales/2025"),     # auto-prefix slash
        ("/sales/2025/",    "/sales/2025"),     # strip trailing
        ("/",               None),              # collapses to empty → unset
        ("",                None),              # explicitly empty → unset
        (None,              None),              # not provided → unset
    ]
    for input_, expected in cases:
        sb = _FakeSandbox(
            stream_chunks=[
                b'{"kind": "done", "final_text": "", "iterations": 0}\n'
            ]
        )
        runner = ClaudeContainerRunner(sb)
        runner.run_turn(
            "x",
            config=_config(),
            principal_user_id="u_x",
            cwd_path=input_,
        )
        env = sb.backend._client.api.exec_creates[0]["environment"]
        if expected is None:
            assert "OPENCRAIG_CWD" not in env, (
                f"input={input_!r}: expected no OPENCRAIG_CWD, got {env.get('OPENCRAIG_CWD')!r}"
            )
        else:
            assert env.get("OPENCRAIG_CWD") == expected, (
                f"input={input_!r}: expected {expected!r}, got {env.get('OPENCRAIG_CWD')!r}"
            )


def test_uses_hardcoded_entrypoint_path():
    """The path matches what Day 1's Dockerfile COPY puts there.
    Drift would silently fail (``no such file``) at exec time, so
    pin it on both sides."""
    sb = _FakeSandbox(
        stream_chunks=[b'{"kind": "done", "final_text": "", "iterations": 0}\n']
    )
    runner = ClaudeContainerRunner(sb)
    runner.run_turn("x", config=_config(), principal_user_id="u_x")
    cmd = sb.backend._client.api.exec_creates[0]["cmd"]
    assert cmd == ["python", "/opt/opencraig/opencraig_run_turn.py"]
    assert ENTRYPOINT_PATH == "/opt/opencraig/opencraig_run_turn.py"


# ---------------------------------------------------------------------------
# JSONL stream parsing
# ---------------------------------------------------------------------------


def test_events_arrive_in_order():
    chunks = [
        b'{"kind": "thinking", "text": "Let me check the docs."}\n',
        b'{"kind": "tool_start", "id": "c1", "tool": "search_vector",'
        b' "params": {"query": "x"}}\n',
        b'{"kind": "tool_end", "id": "c1", "tool": "search_vector",'
        b' "latency_ms": 42, "result_summary": {"hit_count": 3}}\n',
        b'{"kind": "answer_delta", "text": "Based on "}\n',
        b'{"kind": "answer_delta", "text": "your docs..."}\n',
        b'{"kind": "done", "final_text": "Based on your docs...",'
        b' "iterations": 1}\n',
    ]
    sb = _FakeSandbox(stream_chunks=chunks)
    runner = ClaudeContainerRunner(sb)
    seen: list[dict] = []
    result = runner.run_turn(
        "tell me",
        config=_config(),
        principal_user_id="u_alice",
        on_event=seen.append,
    )

    kinds = [e["kind"] for e in seen]
    assert kinds == [
        "thinking",
        "tool_start",
        "tool_end",
        "answer_delta",
        "answer_delta",
        "done",
    ]
    assert result.final_text == "Based on your docs..."
    assert result.iterations == 1


def test_chunks_split_across_newlines():
    """The docker SDK doesn't guarantee chunks align with line
    boundaries — one chunk can hold multiple JSONL lines, or one
    line can span multiple chunks. Both must reassemble correctly."""
    # Three events split arbitrarily across chunks
    chunks = [
        b'{"kind": "thinking", "text": "first"}\n{"kind": ',
        b'"answer_delta", ',
        b'"text": "delta1"}\n',
        b'{"kind": "done", ',
        b'"final_text": "delta1", "iterations": 0}\n',
    ]
    sb = _FakeSandbox(stream_chunks=chunks)
    runner = ClaudeContainerRunner(sb)
    seen: list[dict] = []
    runner.run_turn(
        "x",
        config=_config(),
        principal_user_id="u_x",
        on_event=seen.append,
    )
    kinds = [e["kind"] for e in seen]
    assert kinds == ["thinking", "answer_delta", "done"]
    assert seen[1]["text"] == "delta1"


def test_malformed_line_skipped_not_fatal():
    """A line that isn't valid JSON shouldn't kill the turn —
    just log + skip. Could be the entrypoint accidentally
    printing a diagnostic to stdout."""
    chunks = [
        b"not json at all, ignore me\n",
        b'{"kind": "answer_delta", "text": "hi"}\n',
        b'{"kind": "done", "final_text": "hi", "iterations": 0}\n',
    ]
    sb = _FakeSandbox(stream_chunks=chunks)
    runner = ClaudeContainerRunner(sb)
    seen: list[dict] = []
    runner.run_turn(
        "x",
        config=_config(),
        principal_user_id="u_x",
        on_event=seen.append,
    )
    kinds = [e["kind"] for e in seen]
    assert kinds == ["answer_delta", "done"]


def test_trailing_partial_line_drained():
    """If the entrypoint exits with a buffered line that didn't
    end in \\n (rare but possible on early termination), drain it."""
    chunks = [
        b'{"kind": "answer_delta", "text": "x"}\n',
        # No trailing newline on the done line
        b'{"kind": "done", "final_text": "x", "iterations": 0}',
    ]
    sb = _FakeSandbox(stream_chunks=chunks)
    runner = ClaudeContainerRunner(sb)
    seen: list[dict] = []
    runner.run_turn(
        "x",
        config=_config(),
        principal_user_id="u_x",
        on_event=seen.append,
    )
    assert seen[-1]["kind"] == "done"


def test_non_dict_json_lines_skipped():
    """``json.loads`` on a bare string / number / array succeeds
    but isn't an event dict — skip it."""
    chunks = [
        b'"just a string"\n',
        b"42\n",
        b'{"kind": "done", "final_text": "ok", "iterations": 0}\n',
    ]
    sb = _FakeSandbox(stream_chunks=chunks)
    runner = ClaudeContainerRunner(sb)
    seen: list[dict] = []
    runner.run_turn(
        "x",
        config=_config(),
        principal_user_id="u_x",
        on_event=seen.append,
    )
    assert [e["kind"] for e in seen] == ["done"]


# ---------------------------------------------------------------------------
# SandboxManager interactions
# ---------------------------------------------------------------------------


def test_ensure_container_passes_empty_owned_project_ids():
    """Folder-as-cwd: container is per-USER, not per-project. The
    runner always passes owned_project_ids=() — SandboxManager
    mounts the user's whole workdir tree at /workdir/, the agent
    chdirs inside via the cwd_path env var. Locks in that we don't
    accidentally regress to per-project mounts."""
    sb = _FakeSandbox(
        stream_chunks=[b'{"kind": "done", "final_text": "", "iterations": 0}\n']
    )
    runner = ClaudeContainerRunner(sb)
    runner.run_turn(
        "x",
        config=_config(),
        principal_user_id="u_alice",
        cwd_path="/sales/2025",
    )
    assert sb.ensure_calls == [
        {"user_id": "u_alice", "owned_project_ids": ()}
    ]


def test_touch_called_after_successful_turn():
    sb = _FakeSandbox(
        stream_chunks=[b'{"kind": "done", "final_text": "", "iterations": 0}\n']
    )
    runner = ClaudeContainerRunner(sb)
    runner.run_turn("x", config=_config(), principal_user_id="u_alice")
    assert sb.touch_calls == ["u_alice"]


def test_touch_called_even_after_stream_failure():
    """touch resets the idle reaper; should fire whether or not the
    turn succeeded — a failed turn still means "the user just
    interacted, don't kill their container"."""

    class _FailingAPI(_FakeAPI):
        def exec_start(self, *_a, **_kw):
            raise RuntimeError("docker daemon hung up")

    sb = _FakeSandbox()
    sb.backend._client.api = _FailingAPI([])

    runner = ClaudeContainerRunner(sb)
    with pytest.raises(RuntimeError):
        runner.run_turn("x", config=_config(), principal_user_id="u_alice")
    assert sb.touch_calls == ["u_alice"]


def test_ensure_failure_propagates_with_error_event(monkeypatch):
    class _BrokenSandbox(_FakeSandbox):
        def ensure_container_for_user(self, *_a, **_kw):
            raise RuntimeError("docker daemon down")

    sb = _BrokenSandbox()
    runner = ClaudeContainerRunner(sb)
    seen: list[dict] = []
    with pytest.raises(RuntimeError):
        runner.run_turn(
            "x",
            config=_config(),
            principal_user_id="u_alice",
            on_event=seen.append,
        )
    # An error event should have fired before the raise propagates,
    # so the route's SSE wrapper can fold it into the terminal
    # ``done(stop_reason="error")`` instead of dropping the
    # connection silently.
    assert any(e["kind"] == "error" for e in seen)


# ---------------------------------------------------------------------------
# stream_turn_container helper (worker thread + queue)
# ---------------------------------------------------------------------------


def test_stream_turn_container_yields_events_in_order():
    chunks = [
        b'{"kind": "thinking", "text": "..."}\n',
        b'{"kind": "answer_delta", "text": "ans"}\n',
        b'{"kind": "done", "final_text": "ans", "iterations": 0}\n',
    ]
    sb = _FakeSandbox(stream_chunks=chunks)
    runner = ClaudeContainerRunner(sb)
    out = list(
        stream_turn_container(
            runner, "x", config=_config(), principal_user_id="u_alice"
        )
    )
    kinds = [e["kind"] for e in out]
    assert "thinking" in kinds
    assert "answer_delta" in kinds
    assert kinds[-1] == "done"


def test_stream_turn_container_emits_error_when_runner_raises():
    class _BrokenSandbox(_FakeSandbox):
        def ensure_container_for_user(self, *_a, **_kw):
            raise ValueError("kaboom")

    sb = _BrokenSandbox()
    runner = ClaudeContainerRunner(sb)
    out = list(
        stream_turn_container(
            runner, "x", config=_config(), principal_user_id="u_x"
        )
    )
    assert any(e["kind"] == "error" and e["type"] == "ValueError" for e in out)


def test_stream_turn_container_runs_in_worker_thread():
    """The route's async SSE generator must be able to flush events
    while waiting on docker stdout. That requires the runner's
    blocking iteration to happen on a separate thread."""
    main_tid = threading.get_ident()
    seen_tid: list[int] = []

    chunks = [
        b'{"kind": "done", "final_text": "ok", "iterations": 0}\n',
    ]

    class _ThreadObservingAPI(_FakeAPI):
        def exec_start(self, *a, **kw):
            seen_tid.append(threading.get_ident())
            return super().exec_start(*a, **kw)

    sb = _FakeSandbox(stream_chunks=chunks)
    sb.backend._client.api = _ThreadObservingAPI(chunks)
    runner = ClaudeContainerRunner(sb)

    list(
        stream_turn_container(
            runner, "x", config=_config(), principal_user_id="u_x"
        )
    )
    assert seen_tid, "exec_start was never called"
    assert seen_tid[0] != main_tid, (
        "exec_start ran on the main thread — should be in the worker"
    )
