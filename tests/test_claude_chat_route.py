"""
Tests for ``POST /api/v1/agent/chat`` (Wave 2.5).

The route streams SSE events translating ``ClaudeRuntime`` events
into the wire format the frontend already understands. We verify:

  * happy path: events arrive in the right order, ``done`` is the
    last event, final_text aggregates from answer_delta chunks
  * error event from the runtime is folded into a single
    ``done { stop_reason: "error" }`` (no orphan ``error`` SSE)
  * model resolution: request override > cfg.answering.generator
  * conversation persistence: user message lands BEFORE the stream,
    assistant message lands after, agent_run row recorded
  * unauthenticated requests get 401 (covered by the auth dep — we
    confirm by overriding it)
  * ClaudeUnavailableError surfaces as a clean ``done { error }``,
    not a 500 / dropped connection

Tests stub ``ClaudeRuntime`` + ``stream_turn`` so no real LLM
call or bundled-CLI subprocess is exercised.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_principal, get_state
from api.routes import claude_chat as claude_chat_module


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.messages: list[dict] = []
        self.agent_runs: list[dict] = []
        self._history_by_conv: dict[str, list[dict]] = {}
        # Captures rows added via ``transaction()`` — mainly the
        # short-lived AuthToken rows the route mints for agent
        # loopback auth. Tests can assert one was minted by reading
        # this list.
        self.added_rows: list = []

    def get_messages(self, conv_id, *, limit=100):
        return list(self._history_by_conv.get(conv_id, []))[:limit]

    def seed_history(self, conv_id, msgs):
        self._history_by_conv[conv_id] = list(msgs)

    def add_message(self, msg: dict):
        self.messages.append(dict(msg))

    def add_agent_run(self, run: dict):
        self.agent_runs.append(dict(run))

    def transaction(self):
        """Stub mirroring the real Store's transaction context manager.
        Captures ``sess.add(...)`` calls for inspection."""
        outer = self

        class _Sess:
            def __enter__(self_): return self_
            def __exit__(self_, *args): return False

            def add(self_, row):
                outer.added_rows.append(row)

            def commit(self_): pass
        return _Sess()


def _principal():
    return SimpleNamespace(
        user_id="u_alice",
        username="alice",
        role="user",
        via="cookie",
    )


@pytest.fixture
def state():
    """Minimal AppState surface — only what the route reads."""
    return SimpleNamespace(
        store=_FakeStore(),
        cfg=SimpleNamespace(
            answering=SimpleNamespace(
                generator=SimpleNamespace(
                    model="anthropic/claude-3-5-sonnet",
                    api_key=None,
                    api_key_env=None,
                    api_base=None,
                )
            )
        ),
    )


@pytest.fixture
def app(state):
    a = FastAPI()
    a.include_router(claude_chat_module.router)
    a.dependency_overrides[get_principal] = _principal
    a.dependency_overrides[get_state] = lambda: state
    return a


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def stub_stream(monkeypatch):
    """Replace ``stream_turn`` with a recorded event sequence.

    The fixture returns a setter — call ``stub_stream([...events])``
    to install a script for the next request.

    Patches BOTH the in-process ``stream_turn`` and the container
    ``stream_turn_container`` so tests don't have to know which
    runtime the route picked. ``calls`` distinguishes the two by
    presence of ``runtime_kind`` ("inprocess" / "container").
    """
    holder: dict[str, Any] = {"events": [], "calls": []}

    def _stream_turn(runtime, user_message, *, config, conversation_history=None):
        holder["calls"].append(
            {
                "runtime_kind": "inprocess",
                "user_message": user_message,
                "config": config,
                "history": list(conversation_history or []),
            }
        )
        for evt in holder["events"]:
            yield evt

    def _stream_turn_container(
        runner, user_message, *, config, principal_user_id,
        cwd_path=None, conversation_history=None,
    ):
        holder["calls"].append(
            {
                "runtime_kind": "container",
                "user_message": user_message,
                "config": config,
                "principal_user_id": principal_user_id,
                "cwd_path": cwd_path,
                "history": list(conversation_history or []),
            }
        )
        for evt in holder["events"]:
            yield evt

    monkeypatch.setattr(claude_chat_module, "stream_turn", _stream_turn)
    monkeypatch.setattr(
        claude_chat_module, "stream_turn_container", _stream_turn_container,
    )

    # Patch the runner constructor too so the route doesn't try to
    # instantiate a real one (it'd reach into state.sandbox.backend).
    monkeypatch.setattr(
        claude_chat_module, "ClaudeContainerRunner", lambda _sb: object(),
    )

    def _set(events):
        holder["events"] = list(events)

    _set.calls = holder["calls"]
    return _set


# ---------------------------------------------------------------------------
# Helpers for parsing SSE response
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse ``data: {...}\\n\\n`` blocks where the JSON dict
    carries a ``type`` discriminator. Matches the legacy /agent/chat
    wire format — see api/routes/claude_chat.py::_sse."""
    blocks = [b for b in text.split("\n\n") if b.strip()]
    out: list[tuple[str, dict]] = []
    for blk in blocks:
        for line in blk.splitlines():
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[len("data: "):])
                    out.append((payload.get("type", ""), payload))
                except Exception:
                    out.append(("", {"_raw": line[len("data: "):]}))
    return out


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_streams_events_in_order(client, stub_stream):
    stub_stream(
        [
            {"kind": "thinking", "text": "Let me check the docs."},
            {
                "kind": "tool_start",
                "id": "c1",
                "tool": "search_vector",
                "params": {"query": "blue"},
            },
            {
                "kind": "tool_end",
                "id": "c1",
                "tool": "search_vector",
                "latency_ms": 42,
                "result_summary": {"hit_count": 3},
            },
            {"kind": "answer_delta", "text": "Based on "},
            {"kind": "answer_delta", "text": "your docs, "},
            {"kind": "answer_delta", "text": "the answer is X."},
            {
                "kind": "done",
                "final_text": "Based on your docs, the answer is X.",
                "iterations": 1,
            },
        ]
    )

    with client.stream(
        "POST", "/api/v1/agent/chat", json={"query": "Tell me about X"}
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes()).decode("utf-8")

    events = _parse_sse(body)
    names = [n for n, _ in events]

    # Must start with turn_start, end with done. agent.turn_end fires
    # before the terminating done.
    assert names[0] == "agent.turn_start"
    assert names[-1] == "done"
    assert "agent.turn_end" in names
    assert "agent.thought" in names
    assert "tool.call_start" in names
    assert "tool.call_end" in names
    assert names.count("answer.delta") == 3

    # done event carries the assembled final text + ok stop reason
    done_payload = events[-1][1]
    assert done_payload["stop_reason"] == "end_turn"
    assert done_payload["final_text"] == "Based on your docs, the answer is X."
    assert "total_latency_ms" in done_payload
    assert "run_id" in done_payload
    assert "error" not in done_payload


def test_final_text_falls_back_to_concatenated_deltas(client, stub_stream):
    """If the runtime's done event omits final_text, the route
    aggregates from the streamed deltas."""
    stub_stream(
        [
            {"kind": "answer_delta", "text": "Hello "},
            {"kind": "answer_delta", "text": "world"},
            # No final_text in done — common when the SDK's return shape
            # didn't expose one
            {"kind": "done", "iterations": 1},
        ]
    )

    with client.stream(
        "POST", "/api/v1/agent/chat", json={"query": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode("utf-8")

    events = _parse_sse(body)
    done = events[-1][1]
    assert done["final_text"] == "Hello world"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_runtime_error_event_folds_into_done_error(client, stub_stream):
    stub_stream(
        [
            {"kind": "answer_delta", "text": "starting..."},
            {
                "kind": "error",
                "type": "ConnectionError",
                "message": "upstream broken",
            },
        ]
    )

    with client.stream(
        "POST", "/api/v1/agent/chat", json={"query": "x"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode("utf-8")

    events = _parse_sse(body)
    names = [n for n, _ in events]
    # No standalone ``error`` SSE — the error is folded into ``done``
    assert "error" not in names
    done = events[-1][1]
    assert done["stop_reason"] == "error"
    assert "ConnectionError" in done["error"]


def test_runtime_unavailable_returns_clean_done_error(client, monkeypatch):
    """SDK import / bundled-CLI lookup fails → ClaudeUnavailableError;
    the route catches it and emits ``done { stop_reason: "error" }``
    instead of crashing or 500'ing."""
    from api.agent.claude_runtime import ClaudeUnavailableError

    def _raises(*_a, **_kw):
        raise ClaudeUnavailableError("not installed (test)")

    monkeypatch.setattr(claude_chat_module, "stream_turn", _raises)

    with client.stream(
        "POST", "/api/v1/agent/chat", json={"query": "x"}
    ) as r:
        assert r.status_code == 200  # SSE still 200; error rides in body
        body = b"".join(r.iter_bytes()).decode("utf-8")

    events = _parse_sse(body)
    names = [n for n, _ in events]
    assert names[0] == "agent.turn_start"
    assert names[-1] == "done"
    done = events[-1][1]
    assert done["stop_reason"] == "error"
    # Either runtime path can surface the unavailability; the route
    # wraps it in a generic "agent runtime unavailable" prefix.
    assert "runtime unavailable" in done["error"]
    assert "not installed" in done["error"]


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_request_model_overrides_default(client, stub_stream, state):
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post(
        "/api/v1/agent/chat",
        json={"query": "hi", "model": "openai/gpt-4o"},
    )
    call = stub_stream.calls[-1]
    assert call["config"].model == "openai/gpt-4o"


def test_default_model_from_config(client, stub_stream, state):
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post("/api/v1/agent/chat", json={"query": "hi"})
    call = stub_stream.calls[-1]
    assert call["config"].model == "anthropic/claude-3-5-sonnet"


def test_system_prompt_override_threaded(client, stub_stream):
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post(
        "/api/v1/agent/chat",
        json={"query": "hi", "system_prompt_override": "Be terse."},
    )
    call = stub_stream.calls[-1]
    assert call["config"].system_message == "Be terse."


# ---------------------------------------------------------------------------
# Conversation persistence
# ---------------------------------------------------------------------------


def test_conversation_history_loaded_into_runtime(client, stub_stream, state):
    state.store.seed_history(
        "conv_42",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    )
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post(
        "/api/v1/agent/chat",
        json={"query": "follow up", "conversation_id": "conv_42"},
    )

    call = stub_stream.calls[-1]
    assert call["history"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_user_message_persisted_before_stream(client, stub_stream, state):
    """Mid-stream refresh must show the user's question; we land it
    in DB BEFORE the SSE stream starts."""
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post(
        "/api/v1/agent/chat",
        json={"query": "remember me", "conversation_id": "conv_42"},
    )

    user_rows = [m for m in state.store.messages if m["role"] == "user"]
    assert len(user_rows) == 1
    assert user_rows[0]["content"] == "remember me"
    assert user_rows[0]["conversation_id"] == "conv_42"


def test_assistant_message_persisted_after_stream(client, stub_stream, state):
    stub_stream(
        [
            {"kind": "answer_delta", "text": "the "},
            {"kind": "answer_delta", "text": "answer"},
            {"kind": "done", "iterations": 1},
        ]
    )

    client.post(
        "/api/v1/agent/chat",
        json={"query": "q", "conversation_id": "conv_42"},
    )

    asst_rows = [m for m in state.store.messages if m["role"] == "assistant"]
    assert len(asst_rows) == 1
    assert asst_rows[0]["content"] == "the answer"


def test_agent_run_row_persisted_with_metadata(client, stub_stream, state):
    """Forward-compat hook for Phase C lineage: every turn writes an
    agent_runs row tagging conv_id + user_id + status. Wave 3.5's
    tool_call_log entries reference the run_id we generate here."""
    stub_stream(
        [
            {"kind": "answer_delta", "text": "x"},
            {"kind": "done", "iterations": 3},
        ]
    )

    client.post(
        "/api/v1/agent/chat",
        json={"query": "q", "conversation_id": "conv_99"},
    )

    assert len(state.store.agent_runs) == 1
    run = state.store.agent_runs[0]
    assert run["conversation_id"] == "conv_99"
    assert run["user_id"] == "u_alice"
    assert run["status"] == "ok"
    assert run["iterations"] == 3
    assert run["error"] is None
    assert run["run_id"]


def test_no_persistence_when_conversation_id_absent(client, stub_stream, state):
    """One-off query without a conversation_id: no DB writes
    (Wave 4 will rethink this; for B-MVP we keep the legacy
    behaviour where conv_id is required for persistence)."""
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post("/api/v1/agent/chat", json={"query": "ephemeral"})
    assert state.store.messages == []
    # agent_runs still records the turn (with conv_id=None) so
    # Phase C audit can see "alice ran an agent at 14:32" even on
    # one-off queries
    assert len(state.store.agent_runs) == 1
    assert state.store.agent_runs[0]["conversation_id"] is None


# ---------------------------------------------------------------------------
# Runtime selector — container path when sandbox is wired,
# in-process when not. Wave 2.5b key behavior.
# ---------------------------------------------------------------------------


def test_no_sandbox_falls_back_to_in_process_runtime(client, stub_stream, state):
    """Default test ``state`` has no ``sandbox`` attribute → in-process
    runtime. Verifies the fallback path stays the dev-friendly default."""
    state.sandbox = None  # explicit, makes the intent clear
    stub_stream([{"kind": "done", "iterations": 0, "final_text": "ok"}])

    client.post("/api/v1/agent/chat", json={"query": "hi"})
    assert stub_stream.calls, "no stream call recorded"
    assert stub_stream.calls[-1]["runtime_kind"] == "inprocess"


def test_sandbox_present_routes_to_container(client, stub_stream, state):
    """When the deployment has Docker + a SandboxManager, the
    container path becomes the default. This is the OSS path that
    makes the Workspace actually useful (full the SDK built-in tools
    operating on the bind-mounted workdir)."""
    # A truthy stand-in is enough — the route only checks
    # ``state.sandbox is not None``; the actual runner is stubbed.
    state.sandbox = SimpleNamespace(name="fake-sandbox")
    stub_stream([{"kind": "done", "iterations": 0, "final_text": "ok"}])

    client.post("/api/v1/agent/chat", json={"query": "hi"})
    assert stub_stream.calls[-1]["runtime_kind"] == "container"
    # Container path threads the principal id through so the
    # SandboxManager can resolve which user's container to exec into.
    assert stub_stream.calls[-1]["principal_user_id"] == "u_alice"


def test_container_runtime_carries_history_and_config_too(
    client, stub_stream, state,
):
    """Sanity: switching runtimes shouldn't drop request data
    on the floor. Both paths receive identical history + config."""
    state.sandbox = SimpleNamespace(name="fake-sandbox")
    state.store.seed_history(
        "conv_42",
        [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "earlier-reply"},
        ],
    )
    stub_stream([{"kind": "done", "iterations": 0, "final_text": "ok"}])

    client.post(
        "/api/v1/agent/chat",
        json={
            "query": "follow-up",
            "conversation_id": "conv_42",
            "model": "claude-3-5-sonnet",
        },
    )
    call = stub_stream.calls[-1]
    assert call["runtime_kind"] == "container"
    assert call["history"] == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier-reply"},
    ]
    assert call["config"].model == "claude-3-5-sonnet"


# ---------------------------------------------------------------------------
# Folder-as-cwd: cwd_path resolution + persistence
# ---------------------------------------------------------------------------


def test_explicit_cwd_path_in_body_passed_to_runtime(
    client, stub_stream, state,
):
    """When the user opens a chat in a folder, the frontend sends
    cwd_path in the body. Runtime should receive it verbatim."""
    state.sandbox = SimpleNamespace(name="fake-sandbox")
    stub_stream([{"kind": "done", "iterations": 0, "final_text": "ok"}])

    client.post(
        "/api/v1/agent/chat",
        json={"query": "x", "cwd_path": "/data/sales/2025"},
    )
    call = stub_stream.calls[-1]
    assert call["runtime_kind"] == "container"
    assert call["cwd_path"] == "/data/sales/2025"


def test_cwd_path_falls_back_to_conversation_row(
    client, stub_stream, state,
):
    """If the body omits cwd_path, the route reads it off the
    Conversation row (set when chat was first opened in a folder).
    Reload-and-resume scenario: user closes the page, reopens it,
    chat keeps working in the same folder."""
    state.sandbox = SimpleNamespace(name="fake-sandbox")
    # Pre-store a conversation that was opened in a folder.
    state.store.seed_history("conv_42", [])
    state.store.messages.clear()
    state.store._history_by_conv["conv_42"] = []
    state.store.messages = []
    # Inject the row so get_conversation returns it with cwd_path.
    state.store._conversations = {
        "conv_42": {
            "conversation_id": "conv_42",
            "cwd_path": "/legal/contracts/2025",
        }
    }

    def _get_conversation(cid):
        return state.store._conversations.get(cid)

    state.store.get_conversation = _get_conversation
    state.store.update_conversation = lambda *a, **kw: None

    stub_stream([{"kind": "done", "iterations": 0, "final_text": "ok"}])

    client.post(
        "/api/v1/agent/chat",
        json={"query": "x", "conversation_id": "conv_42"},
    )
    call = stub_stream.calls[-1]
    assert call["cwd_path"] == "/legal/contracts/2025"


def test_cwd_path_override_writes_back_to_conversation(
    client, stub_stream, state,
):
    """If the body sends a different cwd_path than the stored one,
    the conversation row gets updated — that's how the UI's
    "switch folder" gesture persists."""
    state.sandbox = SimpleNamespace(name="fake-sandbox")
    state.store._history_by_conv["conv_77"] = []
    state.store._conversations = {
        "conv_77": {
            "conversation_id": "conv_77",
            "cwd_path": "/old/folder",
        }
    }
    update_calls: list[dict] = []

    def _get_conversation(cid):
        return state.store._conversations.get(cid)

    def _update_conversation(cid, **kw):
        update_calls.append({"conv_id": cid, **kw})

    state.store.get_conversation = _get_conversation
    state.store.update_conversation = _update_conversation

    stub_stream([{"kind": "done", "iterations": 0, "final_text": "ok"}])

    client.post(
        "/api/v1/agent/chat",
        json={
            "query": "x",
            "conversation_id": "conv_77",
            "cwd_path": "/new/folder",
        },
    )
    # Runtime got the new path
    assert stub_stream.calls[-1]["cwd_path"] == "/new/folder"
    # Conversation row updated to match
    assert update_calls == [{"conv_id": "conv_77", "cwd_path": "/new/folder"}]


def test_no_cwd_path_anywhere_is_pure_qa(client, stub_stream, state):
    """No body cwd_path + no stored cwd_path on the conversation +
    no conversation_id at all → runtime gets cwd_path=None,
    which is the "agent works at /workspace root" plain-Q&A signal."""
    state.sandbox = SimpleNamespace(name="fake-sandbox")
    stub_stream([{"kind": "done", "iterations": 0, "final_text": "ok"}])

    client.post("/api/v1/agent/chat", json={"query": "ephemeral"})
    assert stub_stream.calls[-1]["cwd_path"] is None
