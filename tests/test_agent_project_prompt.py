"""
Tests for the Phase-1.6 project-context system prompt augmentation.

Covers the pure formatting function ``_format_project_block`` and the
``build_system_prompt`` public hook in api/agent/prompts.py. The
end-to-end "agent route picks up the prompt" path is covered
indirectly by chat-project-binding tests + manual smoke (the actual
LLM call is deliberately not mocked here — that's a Phase 2+
integration test once we have a fake LLM in the loop).
"""

from __future__ import annotations

from api.agent.prompts import SYSTEM_PROMPT, build_system_prompt
from api.routes.agent import _fmt_size, _format_project_block


def test_build_system_prompt_unbound_returns_base():
    assert build_system_prompt() == SYSTEM_PROMPT
    assert build_system_prompt(None) == SYSTEM_PROMPT
    assert build_system_prompt("") == SYSTEM_PROMPT


def test_build_system_prompt_prepends_project_block():
    block = "PROJECT CONTEXT:\nproject: Test"
    out = build_system_prompt(block)
    assert out.startswith("PROJECT CONTEXT:")
    assert SYSTEM_PROMPT in out
    # The base prompt is preserved verbatim
    assert out.endswith(SYSTEM_PROMPT)
    # Separator between block and base prompt
    assert "\n\n---\n\n" in out


def test_format_project_block_with_files():
    block = _format_project_block(
        name="Q3 Sales",
        description="Quarterly review",
        files=[
            ("inputs/sales.csv", 12345),
            ("outputs/summary.md", 2048),
        ],
        truncated=False,
    )
    assert "Q3 Sales" in block
    assert "Quarterly review" in block
    assert "inputs/sales.csv" in block
    assert "12.1 KB" in block  # 12345 / 1024 rounded
    assert "outputs/summary.md" in block
    # Phase-2 caveat must be present
    assert "Phase 1" in block
    assert "python_exec" in block


def test_format_project_block_empty_workdir():
    block = _format_project_block(
        name="Brand new",
        description=None,
        files=[],
        truncated=False,
    )
    assert "Brand new" in block
    assert "currently empty" in block
    # No file enumeration line when there are zero files
    assert "Project workdir contains:" not in block


def test_format_project_block_truncated():
    files = [("inputs/" + f"f{i}.txt", 100) for i in range(30)]
    block = _format_project_block(
        name="Many files",
        description=None,
        files=files,
        truncated=True,
    )
    # All 30 files listed
    for i in range(30):
        assert f"f{i}.txt" in block
    # Truncation marker present
    assert "plus more files" in block


def test_fmt_size_units():
    assert _fmt_size(0) == "0 B"
    assert _fmt_size(500) == "500 B"
    assert _fmt_size(1024) == "1.0 KB"
    assert _fmt_size(int(1.5 * 1024 * 1024)) == "1.5 MB"
    assert _fmt_size(int(2.5 * 1024 * 1024 * 1024)) == "2.5 GB"


def test_format_project_block_no_description():
    block = _format_project_block(
        name="Anon",
        description=None,
        files=[("inputs/x.txt", 50)],
        truncated=False,
    )
    # When description is None we just skip that line — no empty
    # "description: " noise in the prompt.
    assert "description:" not in block.lower()
    assert "Anon" in block
