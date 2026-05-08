"""
Tests for ``api.agent.hermes_runtime`` (Wave 2.4).

Verifies the wrapper:
    * passes the right kwargs to ``AIAgent`` (built-in toolsets
      hard-disabled, session persistence off, our LLM proxy URL +
      key, model, max_iterations)
    * forwards Hermes' per-event callbacks to a standardised
      ``on_event`` shape (thinking / answer_delta / tool_start /
      tool_end / done)
    * wraps the final return in ``HermesTurnResult`` with the
      final_text extracted from common keys
    * surfaces ``HermesUnavailableError`` cleanly when run_agent
      isn't importable
    * the ``stream_turn`` helper drains events in order via a
      worker thread and emits an error event if the agent raises
      mid-turn

These tests inject a fake AIAgent class via the ``agent_factory``
hook — no real LLM calls, no hermes-agent install required to run.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from api.agent.hermes_runtime import (
    HermesRuntime,
    HermesTurnConfig,
    HermesTurnResult,
    HermesUnavailableError,
    stream_turn,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeAIAgentCallScript:
    """Describes what a fake AIAgent should do in run_conversation:
    fire these callbacks (in order), then return this final dict."""

    callbacks: list  # list[(event_kind, args, kwargs)]
    final: dict


class _FakeAIAgent:
    """Captures init kwargs, then plays a pre-recorded script of
    callback invocations during run_conversation."""

    def __init__(self, **kwargs):
        self._init_kwargs = dict(kwargs)
        # ``_capture`` is a list shared via the factory closure;
        # appending init_kwargs here lets tests inspect what
        # arguments the runtime passed without a class-attr trick
        # that breaks across subclasses.
        self._capture.append(self._init_kwargs)

    def run_conversation(self, user_message, *, conversation_history=None, **kwargs):
        script = self._script
        for kind, args, kwargs in script.callbacks:
            cb_name = f"{kind}_callback"
            cb = self._init_kwargs.get(cb_name)
            if cb is not None:
                cb(*args, **kwargs)
        return script.final


def _factory_for(script: _FakeAIAgentCallScript):
    """Helper: build a fresh AIAgent class with the script bound +
    a shared capture list. Returns ``(factory, capture_list)``;
    ``capture_list[-1]`` is the most recent ``__init__`` kwargs."""
    capture: list[dict] = []

    class _Agent(_FakeAIAgent):
        _script = script
        _capture = capture

    return (lambda: _Agent), capture


def _config(**overrides) -> HermesTurnConfig:
    cfg = dict(
        model="gpt-4o",
        base_url="http://localhost:8000/api/v1/llm/v1",
        api_key="bearer-token-test",
        max_iterations=10,
    )
    cfg.update(overrides)
    return HermesTurnConfig(**cfg)


# ---------------------------------------------------------------------------
# AIAgent kwargs verification
# ---------------------------------------------------------------------------


def test_aiagent_init_disables_built_in_toolsets():
    """Critical safety property: backend-process Hermes must NOT be
    able to use Read / Edit / Bash etc. on our filesystem. The runtime
    hard-disables them via ``enabled_toolsets=[]``."""
    script = _FakeAIAgentCallScript(callbacks=[], final={"response": ""})
    factory, capture = _factory_for(script)
    runtime = HermesRuntime(agent_factory=factory)

    runtime.run_turn("hi", config=_config())

    assert capture, "AIAgent was never constructed"
    assert capture[-1]["enabled_toolsets"] == []


def test_aiagent_init_disables_session_persistence_and_user_context():
    """OpenCraig owns conversation history (Conversation table); Hermes'
    ~/.hermes/ session/memory storage would create a parallel source of
    truth and leak operator config into tenant chats."""
    script = _FakeAIAgentCallScript(callbacks=[], final={"response": ""})
    factory, capture = _factory_for(script)
    runtime = HermesRuntime(agent_factory=factory)

    runtime.run_turn("hi", config=_config())

    init = capture[-1]
    assert init["persist_session"] is False
    assert init["skip_context_files"] is True
    assert init["skip_memory"] is True


def test_aiagent_init_threads_llm_proxy_config():
    script = _FakeAIAgentCallScript(callbacks=[], final={"response": ""})
    factory, capture = _factory_for(script)
    runtime = HermesRuntime(agent_factory=factory)

    runtime.run_turn(
        "hi",
        config=_config(
            base_url="http://gateway:7000/llm",
            api_key="bk-abc",
            model="claude-3-5-sonnet",
            max_iterations=42,
        ),
    )

    init = capture[-1]
    assert init["base_url"] == "http://gateway:7000/llm"
    assert init["api_key"] == "bk-abc"
    assert init["model"] == "claude-3-5-sonnet"
    assert init["max_iterations"] == 42
    assert init["quiet_mode"] is True


def test_system_message_threaded_when_provided():
    script = _FakeAIAgentCallScript(callbacks=[], final={"response": ""})
    factory, capture = _factory_for(script)
    runtime = HermesRuntime(agent_factory=factory)

    runtime.run_turn(
        "hi",
        config=_config(system_message="You are a helpful research assistant."),
    )
    assert (
        capture[-1]["ephemeral_system_prompt"]
        == "You are a helpful research assistant."
    )


def test_system_message_omitted_when_none():
    script = _FakeAIAgentCallScript(callbacks=[], final={"response": ""})
    factory, capture = _factory_for(script)
    runtime = HermesRuntime(agent_factory=factory)

    runtime.run_turn("hi", config=_config())
    # Don't pass an empty ephemeral_system_prompt — let AIAgent use
    # its default.
    assert "ephemeral_system_prompt" not in capture[-1]


# ---------------------------------------------------------------------------
# Callback → event mapping
# ---------------------------------------------------------------------------


def test_callbacks_fan_out_to_standardised_events():
    script = _FakeAIAgentCallScript(
        callbacks=[
            ("thinking", (), {"text": "Let me search the corpus."}),
            ("tool_start", (), {
                "tool": "search_vector",
                "params": {"query": "blue", "top_k": 5},
                "call_id": "c1",
            }),
            ("tool_complete", (), {
                "tool": "search_vector",
                "call_id": "c1",
                "latency_ms": 42,
                "result_summary": {"hit_count": 3},
            }),
            ("stream_delta", (), {"text": "Based on "}),
            ("stream_delta", (), {"text": "your docs, "}),
        ],
        final={"response": "Based on your docs, …", "iterations": 1},
    )
    factory, _capture = _factory_for(script)
    runtime = HermesRuntime(agent_factory=factory)

    events: list[dict] = []
    result = runtime.run_turn("hi", config=_config(), on_event=events.append)

    kinds = [e["kind"] for e in events]
    assert kinds == [
        "thinking",
        "tool_start",
        "tool_end",
        "answer_delta",
        "answer_delta",
        "done",
    ]
    assert events[0]["text"] == "Let me search the corpus."
    assert events[1] == {
        "kind": "tool_start",
        "id": "c1",
        "tool": "search_vector",
        "params": {"query": "blue", "top_k": 5},
    }
    assert events[2]["latency_ms"] == 42
    assert events[2]["result_summary"] == {"hit_count": 3}
    assert events[3]["text"] == "Based on "
    assert events[4]["text"] == "your docs, "
    assert events[5]["kind"] == "done"
    assert events[5]["final_text"] == "Based on your docs, …"
    assert isinstance(result, HermesTurnResult)
    assert result.final_text == "Based on your docs, …"


def test_callback_with_unexpected_arg_shape_doesnt_crash():
    """Hermes versions may add positional args; our adapter mustn't
    raise on shape drift — best-effort extraction + log."""
    script = _FakeAIAgentCallScript(
        callbacks=[
            # positional rather than kwargs
            ("tool_start", ("search_vector", {"q": "x"}, "c-pos"), {}),
            ("stream_delta", ("Hello",), {}),
        ],
        final={"response": "ok"},
    )
    factory, _capture = _factory_for(script)
    runtime = HermesRuntime(agent_factory=factory)
    events: list[dict] = []
    runtime.run_turn("hi", config=_config(), on_event=events.append)

    assert events[0]["kind"] == "tool_start"
    assert events[0]["tool"] == "search_vector"
    assert events[1]["text"] == "Hello"


def test_final_text_extracted_from_common_keys():
    """Hermes' return dict shape varies — try ``response`` then
    ``final_response`` / ``text`` / ``answer`` / ``content``."""
    for key in ("response", "final_response", "text", "answer", "content"):
        script = _FakeAIAgentCallScript(callbacks=[], final={key: f"value-{key}"})
        factory, _capture = _factory_for(script)
        runtime = HermesRuntime(agent_factory=factory)
        result = runtime.run_turn("hi", config=_config())
        assert result.final_text == f"value-{key}"


def test_history_extracted_from_messages_or_history_keys():
    expected = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
    for key in ("messages", "history"):
        script = _FakeAIAgentCallScript(
            callbacks=[],
            final={"response": "x", key: list(expected)},
        )
        factory, _capture = _factory_for(script)
        runtime = HermesRuntime(agent_factory=factory)
        result = runtime.run_turn("hi", config=_config())
        assert result.history == expected


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unavailable_when_hermes_not_installed():
    """run_agent missing → HermesUnavailableError, not bare
    ImportError. Lets the chat route 503 cleanly."""
    def _factory():
        raise HermesUnavailableError("not installed (test)")

    runtime = HermesRuntime(agent_factory=_factory)
    with pytest.raises(HermesUnavailableError):
        runtime.run_turn("hi", config=_config())


def test_run_conversation_raises_emits_error_event_then_propagates():
    class _ExplodingAgent(_FakeAIAgent):
        _capture: list = []
        _script = _FakeAIAgentCallScript(callbacks=[], final={})

        def run_conversation(self, *_a, **_kw):
            raise RuntimeError("boom")

    runtime = HermesRuntime(agent_factory=lambda: _ExplodingAgent)

    events: list[dict] = []
    with pytest.raises(RuntimeError, match="boom"):
        runtime.run_turn("hi", config=_config(), on_event=events.append)
    # An error event should have fired before the raise propagates
    assert any(e["kind"] == "error" for e in events)


# ---------------------------------------------------------------------------
# stream_turn helper
# ---------------------------------------------------------------------------


def test_stream_turn_yields_events_in_order():
    script = _FakeAIAgentCallScript(
        callbacks=[
            ("thinking", (), {"text": "thinking..."}),
            ("stream_delta", (), {"text": "ans"}),
        ],
        final={"response": "ans"},
    )
    factory, _capture = _factory_for(script)
    runtime = HermesRuntime(agent_factory=factory)

    out = list(stream_turn(runtime, "hi", config=_config()))
    kinds = [e["kind"] for e in out]
    # answer_delta + done at minimum; thinking is also there
    assert "thinking" in kinds
    assert "answer_delta" in kinds
    assert kinds[-1] == "done"


def test_stream_turn_emits_error_event_when_agent_raises():
    class _ExplodingAgent(_FakeAIAgent):
        _capture: list = []
        _script = _FakeAIAgentCallScript(callbacks=[], final={})

        def run_conversation(self, *_a, **_kw):
            raise ValueError("kaboom")

    runtime = HermesRuntime(agent_factory=lambda: _ExplodingAgent)
    out = list(stream_turn(runtime, "hi", config=_config()))
    assert any(e["kind"] == "error" and e["type"] == "ValueError" for e in out)


def test_stream_turn_runs_in_separate_thread():
    """Sanity: the worker is its own daemon thread so the route's
    async generator can interleave SSE writes with Hermes work."""
    main_tid = threading.get_ident()
    seen_tid: list[int] = []

    class _Agent(_FakeAIAgent):
        _capture: list = []
        _script = _FakeAIAgentCallScript(callbacks=[], final={"response": "ok"})

        def run_conversation(self, *_a, **_kw):
            seen_tid.append(threading.get_ident())
            return {"response": "ok"}

    runtime = HermesRuntime(agent_factory=lambda: _Agent)
    list(stream_turn(runtime, "hi", config=_config()))

    assert seen_tid, "agent never ran"
    assert seen_tid[0] != main_tid, (
        "AIAgent ran on main thread — should be in worker"
    )
