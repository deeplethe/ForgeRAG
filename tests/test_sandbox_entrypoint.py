"""
Unit tests for ``docker/sandbox/opencraig_run_turn.py``.

The entrypoint runs INSIDE the sandbox container — its end-to-end
behaviour (claude binary spawning, network MCP calls, real LLM)
needs the actual image. Here we exercise the pure-function pieces:
event mapping (SDK message → JSONL line), tool-result
summarisation, env-var → SDK options translation, cwd resolution.
The entrypoint module is imported by file path because it lives
under ``docker/sandbox/`` rather than as part of an installable
package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Module loader — pull the script in by path
# ---------------------------------------------------------------------------


def _load_entrypoint():
    p = (
        Path(__file__).resolve().parent.parent
        / "docker" / "sandbox" / "opencraig_run_turn.py"
    )
    spec = importlib.util.spec_from_file_location("opencraig_run_turn", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


entrypoint = _load_entrypoint()


# ---------------------------------------------------------------------------
# Fake claude_agent_sdk module — dataclasses mirroring the real SDK's
# message types, no actual loop / binary involved.
# ---------------------------------------------------------------------------


@dataclass
class _TextBlock:
    text: str


@dataclass
class _ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class _ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool | None = None


@dataclass
class _AssistantMessage:
    content: list[Any]
    uuid: str | None = None


@dataclass
class _UserMessage:
    content: list[Any]


@dataclass
class _StreamEvent:
    event: dict = field(default_factory=dict)
    uuid: str = ""
    session_id: str = ""
    parent_tool_use_id: str | None = None


@dataclass
class _ResultMessage:
    result: str | None
    num_turns: int = 0


class _CLINotFoundError(Exception):
    pass


class _SDK:
    AssistantMessage = _AssistantMessage
    UserMessage = _UserMessage
    StreamEvent = _StreamEvent
    ResultMessage = _ResultMessage
    TextBlock = _TextBlock
    ThinkingBlock = _ThinkingBlock
    ToolUseBlock = _ToolUseBlock
    ToolResultBlock = _ToolResultBlock
    CLINotFoundError = _CLINotFoundError


# ---------------------------------------------------------------------------
# Helpers — capture emit() output as a list of parsed dicts
# ---------------------------------------------------------------------------


def _lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# emit() smoke + JSONL framing
# ---------------------------------------------------------------------------


def test_emit_writes_one_line_per_payload(capsys):
    entrypoint.emit({"kind": "answer_delta", "text": "Hello"})
    entrypoint.emit({"kind": "done", "final_text": "Hello", "iterations": 1})
    out = _lines(capsys.readouterr().out)
    assert out == [
        {"kind": "answer_delta", "text": "Hello"},
        {"kind": "done", "final_text": "Hello", "iterations": 1},
    ]


def test_emit_handles_unicode(capsys):
    entrypoint.emit({"kind": "answer_delta", "text": "你好 — agent"})
    out = _lines(capsys.readouterr().out)
    assert out[0]["text"] == "你好 — agent"


# ---------------------------------------------------------------------------
# _emit_message_events — SDK message → JSONL events
# ---------------------------------------------------------------------------


def test_assistant_text_block_emits_answer_delta(capsys):
    msg = _AssistantMessage(content=[_TextBlock(text="Hello world")])
    entrypoint._emit_message_events(msg, _SDK, {}, {})
    out = _lines(capsys.readouterr().out)
    assert out == [{"kind": "answer_delta", "text": "Hello world"}]


def test_assistant_thinking_block_emits_thinking(capsys):
    msg = _AssistantMessage(content=[_ThinkingBlock(thinking="Reasoning...")])
    entrypoint._emit_message_events(msg, _SDK, {}, {})
    out = _lines(capsys.readouterr().out)
    assert out == [{"kind": "thinking", "text": "Reasoning..."}]


def test_assistant_tool_use_block_emits_tool_start_and_records_t0(capsys):
    tool_t0: dict = {}
    tool_name: dict = {}
    msg = _AssistantMessage(content=[
        _ToolUseBlock(id="tu_1", name="search_vector", input={"query": "topic"}),
    ])
    entrypoint._emit_message_events(msg, _SDK, tool_t0, tool_name)
    out = _lines(capsys.readouterr().out)
    assert out == [{
        "kind": "tool_start",
        "id": "tu_1",
        "tool": "search_vector",
        "params": {"query": "topic"},
    }]
    assert "tu_1" in tool_t0
    assert tool_name["tu_1"] == "search_vector"


def test_user_tool_result_block_emits_tool_end_with_latency(capsys):
    # Prime the timing ledger as if a tool_use already started
    tool_t0 = {"tu_1": 0.0}  # epoch zero → big latency, OK for test
    tool_name = {"tu_1": "search_vector"}
    msg = _UserMessage(content=[
        _ToolResultBlock(tool_use_id="tu_1", content="some output"),
    ])
    entrypoint._emit_message_events(msg, _SDK, tool_t0, tool_name)
    out = _lines(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["kind"] == "tool_end"
    assert out[0]["id"] == "tu_1"
    assert out[0]["tool"] == "search_vector"
    assert out[0]["latency_ms"] > 0
    assert out[0]["result_summary"] == {"text": "some output"}
    # Timing ledger drained
    assert "tu_1" not in tool_t0
    assert "tu_1" not in tool_name


def test_stream_event_text_delta_emits_answer_delta(capsys):
    msg = _StreamEvent(event={
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "Hel"},
    })
    entrypoint._emit_message_events(msg, _SDK, {}, {})
    out = _lines(capsys.readouterr().out)
    assert out == [{"kind": "answer_delta", "text": "Hel"}]


def test_stream_event_thinking_delta_emits_thinking(capsys):
    msg = _StreamEvent(event={
        "type": "content_block_delta",
        "delta": {"type": "thinking_delta", "thinking": "thinking..."},
    })
    entrypoint._emit_message_events(msg, _SDK, {}, {})
    out = _lines(capsys.readouterr().out)
    assert out == [{"kind": "thinking", "text": "thinking..."}]


def test_stream_event_unknown_type_is_ignored(capsys):
    msg = _StreamEvent(event={"type": "message_start"})
    entrypoint._emit_message_events(msg, _SDK, {}, {})
    assert capsys.readouterr().out == ""


def test_result_message_returns_final_text_no_emit(capsys):
    msg = _ResultMessage(result="Final answer.", num_turns=3)
    final = entrypoint._emit_message_events(msg, _SDK, {}, {})
    assert final == "Final answer."
    # ResultMessage does NOT emit on its own — main() handles the
    # done event with iteration count.
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# _summarise_tool_result — boil down ToolResultBlock.content
# ---------------------------------------------------------------------------


def test_summarise_none_returns_empty():
    assert entrypoint._summarise_tool_result(None) == {}


def test_summarise_string_truncates_to_200():
    long = "x" * 500
    out = entrypoint._summarise_tool_result(long)
    assert out == {"text": "x" * 200}


def test_summarise_dict_passes_known_keys():
    inp = {"hit_count": 5, "extra": "ignored"}
    out = entrypoint._summarise_tool_result(inp)
    assert out == {"hit_count": 5}


def test_summarise_dict_falls_back_to_text_when_no_known_keys():
    inp = {"some_other_key": "value"}
    out = entrypoint._summarise_tool_result(inp)
    assert "text" in out


def test_summarise_list_extracts_json_summary_from_text_blocks():
    """MCP tool results come as a list of {type:text, text:<json>}
    blocks. Parse the JSON and prefer the structured summary keys."""
    inp = [
        {"type": "text",
         "text": '{"hit_count": 7, "results": [{"chunk_id": "c1"}]}'},
    ]
    out = entrypoint._summarise_tool_result(inp)
    assert out == {"hit_count": 7}


def test_summarise_list_falls_back_to_joined_text():
    inp = [
        {"type": "text", "text": "Plain prose result, no JSON."},
    ]
    out = entrypoint._summarise_tool_result(inp)
    assert out == {"text": "Plain prose result, no JSON."}


# ---------------------------------------------------------------------------
# _build_options — env vars → ClaudeAgentOptions
# ---------------------------------------------------------------------------


@dataclass
class _Captured:
    model: str | None = None
    system_prompt: str | None = None
    mcp_servers: dict | None = None
    cwd: str | None = None
    permission_mode: str | None = None
    max_turns: int | None = None
    include_partial_messages: bool = False
    setting_sources: Any = None


class _OptionsCapturingSDK:
    @staticmethod
    def ClaudeAgentOptions(**kwargs):
        return _Captured(**{
            k: v for k, v in kwargs.items()
            if k in _Captured.__dataclass_fields__
        })


def test_build_options_threads_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCRAIG_MODEL", "claude-sonnet-4")
    monkeypatch.setenv("OPENCRAIG_SYSTEM_PROMPT", "Be helpful.")
    monkeypatch.setenv("OPENCRAIG_MAX_TURNS", "30")
    monkeypatch.chdir(tmp_path)
    opts = entrypoint._build_options(
        _OptionsCapturingSDK,
        mcp_url="http://backend:8000/api/v1/mcp",
        mcp_token="bearer-xyz",
    )
    assert opts.model == "claude-sonnet-4"
    assert opts.system_prompt == "Be helpful."
    assert opts.max_turns == 30
    assert opts.include_partial_messages is True
    assert opts.permission_mode == "bypassPermissions"
    assert opts.mcp_servers == {
        "opencraig": {
            "type": "http",
            "url": "http://backend:8000/api/v1/mcp",
            "headers": {"Authorization": "Bearer bearer-xyz"},
        }
    }
    # cwd is captured at build time as the entrypoint's current cwd
    assert opts.cwd == str(tmp_path)


def test_build_options_no_mcp_url_omits_server(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENCRAIG_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    opts = entrypoint._build_options(
        _OptionsCapturingSDK, mcp_url="", mcp_token=""
    )
    assert opts.mcp_servers == {}
    # When OPENCRAIG_MODEL not set, model defaults to None (SDK then
    # picks its built-in default; the agent path doesn't need to know).
    assert opts.model is None


def test_build_options_mcp_without_token_omits_authorization(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    opts = entrypoint._build_options(
        _OptionsCapturingSDK,
        mcp_url="http://backend:8000/api/v1/mcp",
        mcp_token="",
    )
    assert opts.mcp_servers["opencraig"]["headers"] == {}


# ---------------------------------------------------------------------------
# _chdir_to_cwd_or_workdir — folder-as-cwd behaviour
# ---------------------------------------------------------------------------


def test_chdir_falls_back_to_current_when_no_workdir(monkeypatch, tmp_path):
    """Outside the container /workdir doesn't exist; the function
    should fall back gracefully without raising."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENCRAIG_CWD", raising=False)
    # Make sure /workdir doesn't exist on the host
    if Path("/workdir").exists():
        pytest.skip("/workdir exists locally; can't exercise fallback path")
    entrypoint._chdir_to_cwd_or_workdir()
    # Didn't raise, didn't change cwd to nowhere
    assert Path.cwd().exists()


def test_chdir_normalises_cwd_path(monkeypatch, tmp_path):
    """Even on hosts without /workdir, the function should accept
    user-supplied cwd values without crashing — it auto-creates,
    then chdirs (or falls back if even creation fails)."""
    fake_workdir = tmp_path / "workdir"
    fake_workdir.mkdir()
    monkeypatch.setattr(entrypoint.os.path, "isdir", lambda p: (
        p == str(fake_workdir)
        or p == str(fake_workdir / "sales/2025")
    ))
    monkeypatch.setattr(entrypoint.os, "chdir", lambda p: None)
    monkeypatch.setattr(entrypoint.os, "makedirs", lambda p, **kw: None)
    monkeypatch.setenv("OPENCRAIG_CWD", "sales/2025")  # no leading slash
    # Just confirm it runs without raising
    entrypoint._chdir_to_cwd_or_workdir()


# ---------------------------------------------------------------------------
# main() — bail on missing input + bail on missing SDK
# ---------------------------------------------------------------------------


def test_main_missing_user_message_emits_error_and_exits_2(capsys, monkeypatch):
    monkeypatch.delenv("OPENCRAIG_USER_MESSAGE", raising=False)
    rc = entrypoint.main()
    assert rc == 2
    out = _lines(capsys.readouterr().out)
    assert any(e["kind"] == "error" and e["type"] == "MissingInput" for e in out)


def test_main_missing_sdk_emits_error_and_exits_3(capsys, monkeypatch):
    monkeypatch.setenv("OPENCRAIG_USER_MESSAGE", "hello")
    # Make the SDK import fail by hiding it from sys.modules + sys.path
    with patch.dict(sys.modules, {"claude_agent_sdk": None}):
        rc = entrypoint.main()
    assert rc == 3
    out = _lines(capsys.readouterr().out)
    assert any(e["kind"] == "error" and e["type"] == "SDKNotInstalled" for e in out)
