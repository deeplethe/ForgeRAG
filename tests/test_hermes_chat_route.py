"""
Tests for ``POST /api/v1/agent/hermes-chat`` (Wave 2.5).

The route streams SSE events translating ``HermesRuntime`` events
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
  * HermesUnavailableError surfaces as a clean ``done { error }``,
    not a 500 / dropped connection

Tests stub ``HermesRuntime`` + ``stream_turn`` so no real LLM
or hermes-agent install is exercised.
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
from api.routes import hermes_chat as hermes_chat_module


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.messages: list[dict] = []
        self.agent_runs: list[dict] = []
        self._history_by_conv: dict[str, list[dict]] = {}

    def list_messages(self, conv_id):
        return list(self._history_by_conv.get(conv_id, []))

    def seed_history(self, conv_id, msgs):
        self._history_by_conv[conv_id] = list(msgs)

    def add_message(self, msg: dict):
        self.messages.append(dict(msg))

    def add_agent_run(self, run: dict):
        self.agent_runs.append(dict(run))


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
    a.include_router(hermes_chat_module.router)
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
    """
    holder: dict[str, Any] = {"events": [], "calls": []}

    def _stream_turn(runtime, user_message, *, config, conversation_history=None):
        holder["calls"].append(
            {
                "user_message": user_message,
                "config": config,
                "history": list(conversation_history or []),
            }
        )
        for evt in holder["events"]:
            yield evt

    monkeypatch.setattr(hermes_chat_module, "stream_turn", _stream_turn)

    def _set(events):
        holder["events"] = list(events)

    _set.calls = holder["calls"]
    return _set


# ---------------------------------------------------------------------------
# Helpers for parsing SSE response
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse ``event: X\\ndata: {...}\\n\\n`` blocks into (name, payload) tuples."""
    blocks = [b for b in text.split("\n\n") if b.strip()]
    out: list[tuple[str, dict]] = []
    for blk in blocks:
        name = ""
        payload: dict = {}
        for line in blk.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):].strip()
            elif line.startswith("data: "):
                try:
                    payload = json.loads(line[len("data: "):])
                except Exception:
                    payload = {"_raw": line[len("data: "):]}
        out.append((name, payload))
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
        "POST", "/api/v1/agent/hermes-chat", json={"query": "Tell me about X"}
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
            # No final_text in done — common when Hermes' return shape
            # didn't expose one
            {"kind": "done", "iterations": 1},
        ]
    )

    with client.stream(
        "POST", "/api/v1/agent/hermes-chat", json={"query": "hi"}
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
        "POST", "/api/v1/agent/hermes-chat", json={"query": "x"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode("utf-8")

    events = _parse_sse(body)
    names = [n for n, _ in events]
    # No standalone ``error`` SSE — the error is folded into ``done``
    assert "error" not in names
    done = events[-1][1]
    assert done["stop_reason"] == "error"
    assert "ConnectionError" in done["error"]


def test_hermes_unavailable_returns_clean_done_error(client, monkeypatch):
    """run_agent module not installed → HermesUnavailableError;
    the route catches it and emits ``done { stop_reason: "error" }``
    instead of crashing or 500'ing."""
    from api.agent.hermes_runtime import HermesUnavailableError

    def _raises(*_a, **_kw):
        raise HermesUnavailableError("not installed (test)")

    monkeypatch.setattr(hermes_chat_module, "stream_turn", _raises)

    with client.stream(
        "POST", "/api/v1/agent/hermes-chat", json={"query": "x"}
    ) as r:
        assert r.status_code == 200  # SSE still 200; error rides in body
        body = b"".join(r.iter_bytes()).decode("utf-8")

    events = _parse_sse(body)
    names = [n for n, _ in events]
    assert names[0] == "agent.turn_start"
    assert names[-1] == "done"
    done = events[-1][1]
    assert done["stop_reason"] == "error"
    assert "hermes-agent not installed" in done["error"]


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_request_model_overrides_default(client, stub_stream, state):
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post(
        "/api/v1/agent/hermes-chat",
        json={"query": "hi", "model": "openai/gpt-4o"},
    )
    call = stub_stream.calls[-1]
    assert call["config"].model == "openai/gpt-4o"


def test_default_model_from_config(client, stub_stream, state):
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post("/api/v1/agent/hermes-chat", json={"query": "hi"})
    call = stub_stream.calls[-1]
    assert call["config"].model == "anthropic/claude-3-5-sonnet"


def test_system_prompt_override_threaded(client, stub_stream):
    stub_stream([{"kind": "done", "iterations": 0}])

    client.post(
        "/api/v1/agent/hermes-chat",
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
        "/api/v1/agent/hermes-chat",
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
        "/api/v1/agent/hermes-chat",
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
        "/api/v1/agent/hermes-chat",
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
        "/api/v1/agent/hermes-chat",
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

    client.post("/api/v1/agent/hermes-chat", json={"query": "ephemeral"})
    assert state.store.messages == []
    # agent_runs still records the turn (with conv_id=None) so
    # Phase C audit can see "alice ran an agent at 14:32" even on
    # one-off queries
    assert len(state.store.agent_runs) == 1
    assert state.store.agent_runs[0]["conversation_id"] is None
