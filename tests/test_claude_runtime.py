"""
Unit tests for ``api.agent.claude_runtime``.

The SDK's event-driven async generator is mocked so we test our
mapping layer (SDK message types → standardised event dicts) without
invoking the bundled CLI binary or hitting any network. Real-SDK
integration is exercised in tests/test_sandbox_image.py (opt-in).

Coverage:
  - AssistantMessage with TextBlock / ThinkingBlock content blocks
  - StreamEvent with content_block_delta / thinking_delta
  - PreToolUse / PostToolUse hooks fire as tool_start / tool_end
    with computed latency
  - ResultMessage populates final_text + iterations + raw
  - CLINotFoundError surfaces as ClaudeUnavailableError
  - mcp_servers config is converted to the SDK's tagged-union shape
  - allowed_tools defaults to MCP-only when not overridden
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from api.agent.claude_runtime import (
    ClaudeRuntime,
    ClaudeTurnConfig,
    ClaudeUnavailableError,
    stream_turn,
)


# ---------------------------------------------------------------------------
# Fake claude_agent_sdk module — enough surface for the runtime to
# exercise its mapping layer.
# ---------------------------------------------------------------------------


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class _FakeToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class _FakeAssistantMessage:
    content: list[Any]
    model: str = "test-model"
    parent_tool_use_id: str | None = None
    error: str | None = None
    usage: dict | None = None
    message_id: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    uuid: str | None = None


@dataclass
class _FakeUserMessage:
    content: list[Any]


@dataclass
class _FakeSystemMessage:
    subtype: str = "init"
    data: dict = field(default_factory=dict)


@dataclass
class _FakeResultMessage:
    subtype: str = "success"
    duration_ms: int = 1234
    duration_api_ms: int = 1000
    is_error: bool = False
    num_turns: int = 3
    session_id: str = "sess_x"
    stop_reason: str | None = "end_turn"
    total_cost_usd: float | None = 0.001
    usage: dict | None = None
    result: str | None = "Final answer text."
    structured_output: Any = None
    model_usage: dict | None = None
    permission_denials: list | None = None
    deferred_tool_use: Any = None
    errors: list | None = None
    api_error_status: int | None = None
    uuid: str | None = None


@dataclass
class _FakeStreamEvent:
    uuid: str = ""
    session_id: str = ""
    event: dict = field(default_factory=dict)
    parent_tool_use_id: str | None = None


class _FakeCLINotFoundError(Exception):
    pass


@dataclass
class _FakeHookMatcher:
    matcher: str
    hooks: list


@dataclass
class _CapturedOptions:
    model: str | None = None
    system_prompt: str | None = None
    mcp_servers: dict | None = None
    allowed_tools: list | None = None
    cwd: str | None = None
    permission_mode: str | None = None
    max_turns: int | None = None
    include_partial_messages: bool = False
    env: dict | None = None
    hooks: dict | None = None
    setting_sources: Any = None


def _make_fake_sdk(
    *,
    yields: list[Any] | None = None,
    raises: Exception | None = None,
    fire_pre_tool: dict | None = None,
    fire_post_tool: dict | None = None,
):
    """Build a fake ``claude_agent_sdk`` module that yields the given
    sequence of messages.

    ``fire_pre_tool`` / ``fire_post_tool`` simulate the SDK invoking
    our PreToolUse / PostToolUse hooks mid-stream — passed through as
    ``input_data`` dicts.
    """

    captured: dict = {"options": None, "prompt_chunks": []}

    class _FakeSDK:
        # Re-expose the message types so isinstance() in the SUT works
        AssistantMessage = _FakeAssistantMessage
        UserMessage = _FakeUserMessage
        SystemMessage = _FakeSystemMessage
        ResultMessage = _FakeResultMessage
        StreamEvent = _FakeStreamEvent
        TextBlock = _FakeTextBlock
        ThinkingBlock = _FakeThinkingBlock
        ToolUseBlock = _FakeToolUseBlock
        CLINotFoundError = _FakeCLINotFoundError
        HookMatcher = _FakeHookMatcher

        @staticmethod
        def ClaudeAgentOptions(**kwargs):
            captured["options"] = _CapturedOptions(**{
                k: v for k, v in kwargs.items()
                if k in _CapturedOptions.__dataclass_fields__
            })
            return captured["options"]

        @staticmethod
        async def query(*, prompt, options):
            # Drain the prompt iterator so the SUT's history
            # composition runs through.
            if hasattr(prompt, "__aiter__"):
                async for chunk in prompt:
                    captured["prompt_chunks"].append(chunk)
            else:
                captured["prompt_chunks"].append(prompt)

            if raises is not None:
                raise raises

            # Fire hooks if the test asked for it. Locate the hook
            # callables off the captured options.
            if fire_pre_tool is not None:
                for matcher in (options.hooks or {}).get("PreToolUse", []):
                    for hook in matcher.hooks:
                        hook(fire_pre_tool["input_data"],
                             fire_pre_tool["tool_use_id"], None)

            for msg in (yields or []):
                yield msg

            if fire_post_tool is not None:
                for matcher in (options.hooks or {}).get("PostToolUse", []):
                    for hook in matcher.hooks:
                        hook(fire_post_tool["input_data"],
                             fire_post_tool["tool_use_id"], None)

    return _FakeSDK, captured


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides) -> ClaudeTurnConfig:
    base = dict(
        model="claude-sonnet-4",
        base_url="http://localhost:8000/api/v1/llm/anthropic",
        api_key="sk-test",
        max_iterations=10,
    )
    base.update(overrides)
    return ClaudeTurnConfig(**base)


def _drive(sdk_class, *, history=None, user_msg="hello", config=None):
    runtime = ClaudeRuntime(sdk_module=sdk_class)
    events: list[dict] = []
    runtime.run_turn(
        user_msg,
        config=config or _config(),
        conversation_history=history,
        on_event=events.append,
    )
    return events


# ---------------------------------------------------------------------------
# Result + streaming + hook event mapping
# ---------------------------------------------------------------------------


def test_result_message_yields_done_with_final_text():
    sdk, _ = _make_fake_sdk(yields=[
        _FakeResultMessage(result="Hi there.", num_turns=2),
    ])
    events = _drive(sdk)
    done = [e for e in events if e["kind"] == "done"]
    assert len(done) == 1
    assert done[0]["final_text"] == "Hi there."
    assert done[0]["iterations"] == 2


def test_text_delta_stream_event_yields_answer_delta():
    sdk, _ = _make_fake_sdk(yields=[
        _FakeStreamEvent(event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hel"},
        }),
        _FakeStreamEvent(event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "lo"},
        }),
        _FakeResultMessage(result="Hello"),
    ])
    events = _drive(sdk)
    deltas = [e for e in events if e["kind"] == "answer_delta"]
    assert [e["text"] for e in deltas] == ["Hel", "lo"]


def test_thinking_delta_stream_event_yields_thinking():
    sdk, _ = _make_fake_sdk(yields=[
        _FakeStreamEvent(event={
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "Let me think..."},
        }),
        _FakeResultMessage(result=""),
    ])
    events = _drive(sdk)
    thoughts = [e for e in events if e["kind"] == "thinking"]
    assert [e["text"] for e in thoughts] == ["Let me think..."]


def test_assistant_message_thinking_block_yields_thinking():
    """Even without partial-stream events, full-message ThinkingBlock
    content surfaces as a thinking event."""
    sdk, _ = _make_fake_sdk(yields=[
        _FakeAssistantMessage(content=[_FakeThinkingBlock(thinking="reasoning")]),
        _FakeResultMessage(result=""),
    ])
    events = _drive(sdk)
    assert any(e["kind"] == "thinking" and e["text"] == "reasoning" for e in events)


def test_pre_post_tool_hooks_fire_tool_start_and_tool_end():
    sdk, _ = _make_fake_sdk(
        yields=[_FakeResultMessage(result="ok")],
        fire_pre_tool={
            "input_data": {
                "tool_name": "search_vector",
                "tool_input": {"query": "topic", "top_k": 5},
            },
            "tool_use_id": "tu_1",
        },
        fire_post_tool={
            "input_data": {
                "tool_name": "search_vector",
                "tool_response": {"hit_count": 7},
            },
            "tool_use_id": "tu_1",
        },
    )
    events = _drive(sdk)
    starts = [e for e in events if e["kind"] == "tool_start"]
    ends = [e for e in events if e["kind"] == "tool_end"]
    assert len(starts) == 1
    assert starts[0]["tool"] == "search_vector"
    assert starts[0]["params"] == {"query": "topic", "top_k": 5}
    assert starts[0]["id"] == "tu_1"
    assert len(ends) == 1
    assert ends[0]["id"] == "tu_1"
    assert ends[0]["tool"] == "search_vector"
    assert ends[0]["result_summary"] == {"hit_count": 7}
    # Latency tracked between hooks (hooks fire essentially back-to-
    # back here, but the field is computed and >= 0).
    assert ends[0]["latency_ms"] >= 0


# ---------------------------------------------------------------------------
# Options translation
# ---------------------------------------------------------------------------


def test_mcp_servers_translated_to_http_config():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    cfg = _config(
        mcp_servers={
            "opencraig": {
                "url": "http://backend:8000/api/v1/mcp",
                "headers": {"Authorization": "Bearer xyz"},
            }
        }
    )
    _drive(sdk, config=cfg)
    opts = captured["options"]
    assert opts.mcp_servers == {
        "opencraig": {
            "type": "http",
            "url": "http://backend:8000/api/v1/mcp",
            "headers": {"Authorization": "Bearer xyz"},
        }
    }


def test_allowed_tools_defaults_to_mcp_only_when_not_set():
    """In-process safety: built-in Read/Edit/Bash etc. would touch
    the BACKEND filesystem if allowed; default policy keeps only
    MCP-prefixed tools."""
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    cfg = _config(mcp_servers={"opencraig": {"url": "http://x", "headers": {}}})
    _drive(sdk, config=cfg)
    assert captured["options"].allowed_tools == ["mcp__opencraig__"]


def test_allowed_tools_explicit_override_passes_through():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    cfg = _config(allowed_tools=["Read", "Edit", "Bash", "mcp__opencraig__search"])
    _drive(sdk, config=cfg)
    assert captured["options"].allowed_tools == [
        "Read", "Edit", "Bash", "mcp__opencraig__search",
    ]


def test_base_url_and_api_key_threaded_to_env():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    _drive(sdk, config=_config(
        base_url="http://litellm:4000/anthropic", api_key="sk-litellm-xyz",
    ))
    assert captured["options"].env == {
        "ANTHROPIC_BASE_URL": "http://litellm:4000/anthropic",
        "ANTHROPIC_API_KEY": "sk-litellm-xyz",
    }


def test_max_iterations_maps_to_max_turns():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    _drive(sdk, config=_config(max_iterations=42))
    assert captured["options"].max_turns == 42


def test_permission_mode_locked_to_bypass():
    """Mid-loop interactive prompts would deadlock the SSE stream —
    we always pre-approve so the loop runs to completion. Authz is
    enforced at the MCP boundary, not by the SDK's UI prompt."""
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    _drive(sdk)
    assert captured["options"].permission_mode == "bypassPermissions"


def test_include_partial_messages_enabled():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    _drive(sdk)
    assert captured["options"].include_partial_messages is True


def test_system_message_threaded_to_system_prompt():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    _drive(sdk, config=_config(system_message="You are helpful."))
    assert captured["options"].system_prompt == "You are helpful."


def test_cwd_threaded_to_options_cwd():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    _drive(sdk, config=_config(cwd="/workdir/sales/2025"))
    assert captured["options"].cwd == "/workdir/sales/2025"


# ---------------------------------------------------------------------------
# Conversation history is composed into the streaming-input prompt
# ---------------------------------------------------------------------------


def test_conversation_history_threaded_into_prompt_stream():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    history = [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
    ]
    _drive(sdk, history=history, user_msg="Second question")
    chunks = captured["prompt_chunks"]
    # Three chunks: two history + one new user message
    assert len(chunks) == 3
    assert chunks[0]["message"] == {"role": "user", "content": "First question"}
    assert chunks[1]["message"] == {"role": "assistant", "content": "First answer"}
    assert chunks[2]["message"] == {"role": "user", "content": "Second question"}


def test_history_with_invalid_roles_skipped():
    sdk, captured = _make_fake_sdk(yields=[_FakeResultMessage(result="ok")])
    history = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "kept"},
        {"role": "tool", "content": "ignored"},
    ]
    _drive(sdk, history=history, user_msg="last")
    chunks = captured["prompt_chunks"]
    assert len(chunks) == 2  # one valid history + the new user msg
    assert chunks[0]["message"] == {"role": "user", "content": "kept"}


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def test_cli_not_found_raises_claude_unavailable():
    sdk, _ = _make_fake_sdk(raises=_FakeCLINotFoundError("missing"))
    runtime = ClaudeRuntime(sdk_module=sdk)
    with pytest.raises(ClaudeUnavailableError):
        runtime.run_turn(
            "hi",
            config=_config(),
            on_event=lambda _: None,
        )


def test_query_failure_emits_error_event_and_reraises():
    sdk, _ = _make_fake_sdk(raises=RuntimeError("boom"))
    runtime = ClaudeRuntime(sdk_module=sdk)
    events: list[dict] = []
    with pytest.raises(RuntimeError):
        runtime.run_turn(
            "hi",
            config=_config(),
            on_event=events.append,
        )
    errors = [e for e in events if e["kind"] == "error"]
    assert len(errors) == 1
    assert "boom" in errors[0]["message"]


# ---------------------------------------------------------------------------
# stream_turn helper — worker thread + queue handoff
# ---------------------------------------------------------------------------


def test_stream_turn_yields_events_and_terminates():
    sdk, _ = _make_fake_sdk(yields=[
        _FakeStreamEvent(event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "x"},
        }),
        _FakeResultMessage(result="x", num_turns=1),
    ])
    runtime = ClaudeRuntime(sdk_module=sdk)
    events = list(stream_turn(runtime, "hi", config=_config()))
    kinds = [e["kind"] for e in events]
    assert "answer_delta" in kinds
    assert "done" in kinds
    # Generator ends after done — no events after.
    assert kinds[-1] == "done"


def test_stream_turn_emits_error_event_when_worker_explodes():
    sdk, _ = _make_fake_sdk(raises=RuntimeError("kaboom"))
    runtime = ClaudeRuntime(sdk_module=sdk)
    events = list(stream_turn(runtime, "hi", config=_config()))
    # At least one error event lands; the stream still terminates.
    assert any(e["kind"] == "error" for e in events)
