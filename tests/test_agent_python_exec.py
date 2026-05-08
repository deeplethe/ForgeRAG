"""
Unit tests for Phase 2.4 — ``python_exec`` agent tool.

Covers:
- Tool registration: ``python_exec`` is in TOOL_REGISTRY
- ``tools_for(ctx)`` filters python_exec out when project_id /
  kernel_manager are missing
- Dispatch happy path: kernel_manager.execute called with the right
  (user_id, project_id, code, timeout, owned_project_ids)
- Dispatch errors: missing project_id, missing kernel_manager,
  non-owner user, kernel exception, missing/empty ``code``
- Output truncation: long stdout cropped for the LLM view
- Error envelope: kernel-side traceback compacted to last frames
- Timeout: timed_out=True surfaces in the result dict
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from api.agent.dispatch import ToolContext, dispatch, tools_for
from api.agent.tools import TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeExecutionResult:
    """Mimics persistence.kernel_manager.ExecutionResult enough for
    the python_exec dispatch path to consume it."""

    stdout: str = ""
    stderr: str = ""
    error: dict[str, Any] | None = None
    execution_count: int | None = None
    timed_out: bool = False
    wall_ms: int = 0
    rich_outputs: list[dict[str, Any]] = field(default_factory=list)


class FakeKernelManager:
    def __init__(self, *, result: FakeExecutionResult | None = None,
                 raises: Exception | None = None):
        self.result = result or FakeExecutionResult(stdout="ok\n")
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def execute(self, user_id, project_id, code, *, timeout, owned_project_ids):
        self.calls.append({
            "user_id": user_id,
            "project_id": project_id,
            "code": code,
            "timeout": timeout,
            "owned_project_ids": tuple(owned_project_ids),
        })
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeStore:
    """The transaction context manager + a select-able list of
    Project rows. Just enough for ``_list_owned_project_ids``."""

    class _ProjectRow:
        def __init__(self, project_id, owner_user_id, trashed=False):
            self.project_id = project_id
            self.owner_user_id = owner_user_id
            self.trashed_metadata = {"x": 1} if trashed else None

    def __init__(self, projects: list[_ProjectRow]):
        self.projects = projects

    def transaction(self):
        store = self

        class _Sess:
            def __enter__(self_): return self_
            def __exit__(self_, *args): return False
            def execute(self_, _stmt):
                # Return an object with .scalars() returning the list.
                # The handler iterates over those rows.
                class _Sc:
                    def __init__(self_, rows): self_.rows = rows
                    def scalars(self_): return iter(self_.rows)
                # _stmt encodes user_id we want to filter by — pull it
                # from the where-clause's right-hand-side. Keeping the
                # fake simple: return everything; handler filters by
                # owner_user_id field implicitly via the SQL where in
                # production. For tests, callers supply only the
                # right user's projects.
                return _Sc(store.projects)
        return _Sess()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    *,
    user_id: str = "u_alice",
    project_id: str | None = "p_a",
    kernel_manager: Any = None,
    owned_projects: list[FakeStore._ProjectRow] | None = None,
    projects_root: Path | None = None,
) -> ToolContext:
    if owned_projects is None:
        owned_projects = [FakeStore._ProjectRow(project_id, user_id)] if project_id else []
    # cfg.agent.projects_root is read by the rich-output persister
    # (Phase 2.5). Tests that don't care just get a tmp-ish default;
    # tests that DO care pass an explicit Path so they can verify
    # files landed.
    if projects_root is None:
        import tempfile
        projects_root = Path(tempfile.mkdtemp(prefix="pyexec-test-"))
    state = SimpleNamespace(
        store=FakeStore(owned_projects),
        kernel_manager=kernel_manager,
        cfg=SimpleNamespace(
            agent=SimpleNamespace(projects_root=str(projects_root)),
        ),
    )
    principal = SimpleNamespace(
        user_id=user_id,
        username=user_id.removeprefix("u_"),
        role="user",
        via="cookie",
    )
    return ToolContext(
        state=state,
        principal=principal,
        accessible=set(),
        path_filters=None,
        allowed_doc_ids=None,
        project_id=project_id,
        kernel_manager=kernel_manager,
    )


# ---------------------------------------------------------------------------
# Registration + tools_for filtering
# ---------------------------------------------------------------------------


def test_python_exec_registered():
    assert "python_exec" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["python_exec"]
    assert "code" in spec.params_schema["properties"]
    assert spec.params_schema["required"] == ["code"]


def test_tools_for_filters_python_exec_when_no_project():
    ctx = _ctx(project_id=None, kernel_manager=FakeKernelManager())
    names = {s.name for s in tools_for(ctx)}
    assert "python_exec" not in names


def test_tools_for_filters_python_exec_when_no_kernel_manager():
    ctx = _ctx(project_id="p_a", kernel_manager=None)
    names = {s.name for s in tools_for(ctx)}
    assert "python_exec" not in names


def test_tools_for_includes_python_exec_when_both_present():
    ctx = _ctx(project_id="p_a", kernel_manager=FakeKernelManager())
    names = {s.name for s in tools_for(ctx)}
    assert "python_exec" in names


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_dispatch_happy_path():
    km = FakeKernelManager(result=FakeExecutionResult(
        stdout="hello world\n",
        stderr="",
        execution_count=3,
        wall_ms=42,
    ))
    ctx = _ctx(kernel_manager=km)
    result = dispatch("python_exec", {"code": "print('hello world')"}, ctx)
    assert result["stdout"] == "hello world\n"
    assert result["execution_count"] == 3
    assert result["wall_ms"] == 42
    assert result["timed_out"] is False
    assert result["rich_outputs_count"] == 0
    assert "error" not in result
    # KernelManager called with the right args
    assert len(km.calls) == 1
    call = km.calls[0]
    assert call["user_id"] == "u_alice"
    assert call["project_id"] == "p_a"
    assert call["code"] == "print('hello world')"
    assert call["timeout"] == 30.0  # default
    assert call["owned_project_ids"] == ("p_a",)


def test_dispatch_passes_through_timeout_param():
    km = FakeKernelManager()
    ctx = _ctx(kernel_manager=km)
    dispatch(
        "python_exec",
        {"code": "1+1", "timeout": 60},
        ctx,
    )
    assert km.calls[0]["timeout"] == 60.0


def test_dispatch_clamps_huge_timeout():
    km = FakeKernelManager()
    ctx = _ctx(kernel_manager=km)
    dispatch(
        "python_exec",
        {"code": "1+1", "timeout": 9999},
        ctx,
    )
    # Capped at _PYTHON_EXEC_MAX_TIMEOUT (120)
    assert km.calls[0]["timeout"] == 120.0


# ---------------------------------------------------------------------------
# Error envelopes the LLM sees
# ---------------------------------------------------------------------------


def test_dispatch_no_project_id_returns_clean_error():
    ctx = _ctx(project_id=None, kernel_manager=FakeKernelManager())
    result = dispatch("python_exec", {"code": "1"}, ctx)
    assert "error" in result
    assert "bound to a project" in result["error"]
    # And the kernel was never touched
    assert ctx.kernel_manager.calls == []


def test_dispatch_no_kernel_manager_returns_clean_error():
    ctx = _ctx(kernel_manager=None)
    result = dispatch("python_exec", {"code": "1"}, ctx)
    assert "error" in result
    assert "operator hasn't enabled" in result["error"] or \
           "agent sandbox" in result["error"]


def test_dispatch_viewer_cannot_run_python_exec():
    """Bob is a viewer of alice's project (chat-binding allowed in
    Phase 1.5) but not an owner — python_exec must refuse him."""
    km = FakeKernelManager()
    # Owned-projects list belongs to alice; bob is the principal.
    ctx = _ctx(
        user_id="u_bob",
        project_id="p_alice_only",
        kernel_manager=km,
        owned_projects=[],  # bob owns nothing
    )
    result = dispatch("python_exec", {"code": "1"}, ctx)
    assert "error" in result
    assert "owner" in result["error"]
    assert km.calls == []


def test_dispatch_empty_code_returns_clean_error():
    ctx = _ctx(kernel_manager=FakeKernelManager())
    result = dispatch("python_exec", {"code": "   "}, ctx)
    assert "error" in result
    assert "non-empty" in result["error"]


def test_dispatch_missing_code_field_caught_by_validator():
    ctx = _ctx(kernel_manager=FakeKernelManager())
    result = dispatch("python_exec", {}, ctx)
    assert "error" in result
    # Schema validator surfaces "missing required param: 'code'"
    assert "code" in result["error"]


def test_dispatch_kernel_raises_returns_friendly_error():
    km = FakeKernelManager(raises=RuntimeError("daemon down"))
    ctx = _ctx(kernel_manager=km)
    result = dispatch("python_exec", {"code": "1"}, ctx)
    assert "error" in result
    assert "RuntimeError" in result["error"]
    # Don't leak the raw message ("daemon down" might be sensitive);
    # just the type name + recovery hint.
    assert "daemon down" not in result["error"]


# ---------------------------------------------------------------------------
# Output trimming + traceback compaction
# ---------------------------------------------------------------------------


def test_dispatch_truncates_long_stdout_for_llm_view():
    long = "x" * 10000
    km = FakeKernelManager(result=FakeExecutionResult(stdout=long))
    ctx = _ctx(kernel_manager=km)
    result = dispatch("python_exec", {"code": "x"}, ctx)
    # Cap is 5000 chars; we get the head plus the truncation marker
    assert len(result["stdout"]) < len(long)
    assert "truncated" in result["stdout"]
    # And the original length is reported in the marker
    assert "10,000" in result["stdout"] or "10000" in result["stdout"]


def test_dispatch_compacts_traceback_to_last_frames():
    long_tb = [f"Frame {i}: line {i}" for i in range(20)]
    km = FakeKernelManager(result=FakeExecutionResult(
        error={
            "ename": "NameError",
            "evalue": "name 'foo' is not defined",
            "traceback": long_tb,
        },
    ))
    ctx = _ctx(kernel_manager=km)
    result = dispatch("python_exec", {"code": "foo"}, ctx)
    err = result["error"]
    assert err["ename"] == "NameError"
    assert "foo" in err["evalue"]
    # Compact view: last 6 frames present, earlier ones dropped
    assert "Frame 19" in err["traceback_short"]
    assert "Frame 0" not in err["traceback_short"]


def test_dispatch_surfaces_timed_out_flag():
    km = FakeKernelManager(result=FakeExecutionResult(
        stdout="started\n",
        timed_out=True,
        wall_ms=30000,
    ))
    ctx = _ctx(kernel_manager=km)
    result = dispatch("python_exec", {"code": "while True: pass"}, ctx)
    assert result["timed_out"] is True
    assert result["wall_ms"] == 30000


def test_dispatch_surfaces_rich_outputs_count():
    km = FakeKernelManager(result=FakeExecutionResult(
        stdout="",
        rich_outputs=[
            {"kind": "display_data", "data": {"image/png": _PNG_B64}},
            {"kind": "execute_result", "data": {"text/html": "<table>..."}},
        ],
    ))
    ctx = _ctx(kernel_manager=km)
    result = dispatch("python_exec", {"code": "df.head()"}, ctx)
    assert result["rich_outputs_count"] == 2


# ---------------------------------------------------------------------------
# Phase 2.5: rich outputs persisted to workdir + LLM-stripped
# ---------------------------------------------------------------------------


_PNG_B64 = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000005000100"
        "0d0a2db40000000049454e44ae426082"
    )
).decode("ascii")


def test_rich_outputs_persisted_to_workdir(tmp_path):
    """Phase 2.5: outputs land at scratch/_rich_outputs/<file> in the
    project workdir, NOT inline in the LLM result."""
    km = FakeKernelManager(result=FakeExecutionResult(
        stdout="ok\n",
        rich_outputs=[
            {"kind": "display_data", "data": {"image/png": _PNG_B64}},
        ],
    ))
    ctx = _ctx(kernel_manager=km, project_id="p_a", projects_root=tmp_path)
    result = dispatch("python_exec", {"code": "plt.show()"}, ctx)

    # File on disk
    workdir = tmp_path / "p_a"
    rich_dir = workdir / "scratch" / "_rich_outputs"
    assert rich_dir.exists()
    pngs = list(rich_dir.glob("*.png"))
    assert len(pngs) == 1
    # Decoded properly (not stored as base64 string)
    assert pngs[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # Result has the internal-channel field for the trace
    assert "_rich_outputs" in result
    refs = result["_rich_outputs"]
    assert len(refs) == 1
    assert refs[0]["mime"] == "image/png"
    assert refs[0]["path"].startswith("scratch/_rich_outputs/")
    assert refs[0]["path"].endswith(".png")
    assert refs[0]["project_id"] == "p_a"
    # Plus the public count for the LLM
    assert result["rich_outputs_count"] == 1


def test_rich_outputs_stripped_from_llm_view():
    """The internal ``_rich_outputs`` channel must NOT reach the
    LLM (file paths would tempt it to reference them; we want clean
    text-shaped tool results)."""
    from api.agent.loop import _strip_internal_keys

    result = {
        "stdout": "ok",
        "rich_outputs_count": 2,
        "_rich_outputs": [
            {"kind": "display_data", "mime": "image/png",
             "path": "scratch/_rich_outputs/abc-01.png",
             "size_bytes": 100, "project_id": "p_a"},
        ],
        "_internal_telemetry": "anything",
    }
    cleaned = _strip_internal_keys(result)
    assert "_rich_outputs" not in cleaned
    assert "_internal_telemetry" not in cleaned
    # Public fields preserved
    assert cleaned["stdout"] == "ok"
    assert cleaned["rich_outputs_count"] == 2


def test_rich_outputs_in_summary_for_trace_event():
    """The SSE event's ``result_summary`` exposes rich_outputs (sans
    underscore) so the frontend trace renders them."""
    from api.agent.loop import _summarise_result

    result = {
        "stdout": "x",
        "execution_count": 5,
        "rich_outputs_count": 1,
        "_rich_outputs": [
            {"kind": "display_data", "mime": "image/png",
             "path": "scratch/_rich_outputs/abc-01.png",
             "size_bytes": 100, "project_id": "p_a"},
        ],
    }
    summary = _summarise_result(result)
    assert summary.get("rich_outputs") == result["_rich_outputs"]
    assert summary.get("execution_count") == 5


def test_persist_failure_doesnt_break_python_exec(tmp_path, monkeypatch):
    """If the persister explodes (disk full / permission denied / ...),
    python_exec still returns useful stdout/stderr. The agent must
    not get an opaque "tool failed" because we couldn't save a chart."""
    from persistence import rich_output_persister

    def boom(*a, **kw):
        raise OSError("simulated disk full")

    monkeypatch.setattr(
        rich_output_persister, "persist_rich_outputs", boom,
    )
    km = FakeKernelManager(result=FakeExecutionResult(
        stdout="data: 42\n",
        rich_outputs=[
            {"kind": "display_data", "data": {"image/png": _PNG_B64}},
        ],
    ))
    ctx = _ctx(kernel_manager=km, projects_root=tmp_path)
    result = dispatch("python_exec", {"code": "x"}, ctx)
    # No crash; LLM still sees stdout. rich_outputs_count is 0
    # because nothing got persisted.
    assert result["stdout"] == "data: 42\n"
    assert result.get("rich_outputs_count") == 0
    assert "_rich_outputs" not in result
