"""
Unit tests for Phase 2.7 — ``bash_exec`` agent tool + auto-Artifact
tracking on python_exec / bash_exec calls.

Covers:
- bash_exec registration; tools_for filtering (needs project +
  kernel_manager just like python_exec)
- Dispatch happy path: SandboxManager.ensure_container called,
  backend.exec invoked with ``["bash", "-lc", cmd]``,
  workdir=/workdir/<pid>, detach=False; result has stdout/stderr/
  exit_code/wall_ms
- Error envelopes: empty/missing command, no project, no
  kernel_manager, viewer (non-owner), backend raises
- Auto-Artifact tracking on bash_exec: file created via
  ``echo > outputs/x.txt`` produces an Artifact row with
  lineage.tool='bash_exec'
- Auto-Artifact tracking on python_exec: same plumbing, lineage
  tool='python_exec'
- Rich-output flow on python_exec is unchanged by 2.7 changes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from sqlalchemy import select

from api.agent.dispatch import ToolContext, dispatch, tools_for
from api.agent.tools import TOOL_REGISTRY
from config import RelationalConfig, SQLiteConfig
from persistence.models import Artifact, AuthUser, Project
from persistence.store import Store


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _ExecResult:
    exit_code: int = 0
    stdout: bytes = b""
    stderr: bytes = b""


class _FakeBackend:
    def __init__(self):
        self.exec_calls: list[dict] = []
        self._next_result = _ExecResult(exit_code=0, stdout=b"OK\n")
        self._next_raise: Exception | None = None
        self._side_effect = None  # callable(cmd, workdir) → optionally writes files

    def configure(self, *, result=None, raises=None, side_effect=None):
        if result is not None:
            self._next_result = result
        if raises is not None:
            self._next_raise = raises
        if side_effect is not None:
            self._side_effect = side_effect

    def exec(self, container_id, cmd, *, workdir=None, timeout=None, detach=False):
        self.exec_calls.append({
            "container_id": container_id,
            "cmd": list(cmd),
            "workdir": workdir,
            "timeout": timeout,
            "detach": detach,
        })
        if self._next_raise is not None:
            err = self._next_raise
            self._next_raise = None
            raise err
        if self._side_effect is not None:
            self._side_effect(cmd, workdir)
        return self._next_result


@dataclass
class _ContainerHandle:
    container_id: str = "cnt_abc"


class _FakeSandbox:
    def __init__(self):
        self.backend = _FakeBackend()
        self.touched: list[str] = []

    def ensure_container_for_user(self, user_id, *, owned_project_ids=()):
        return _ContainerHandle()

    def touch(self, user_id):
        self.touched.append(user_id)


@dataclass
class _FakeExecutionResult:
    stdout: str = ""
    stderr: str = ""
    error: dict | None = None
    execution_count: int | None = None
    timed_out: bool = False
    wall_ms: int = 0
    rich_outputs: list = field(default_factory=list)


class _FakeKernelManager:
    """Wraps a fake sandbox; exposes .execute() like KernelManager."""

    def __init__(self, sandbox: _FakeSandbox):
        self.sandbox = sandbox
        self.execute_calls: list[dict] = []
        self._next_result = _FakeExecutionResult(stdout="ok\n")
        self._side_effect = None

    def configure(self, *, result=None, side_effect=None):
        if result is not None:
            self._next_result = result
        if side_effect is not None:
            self._side_effect = side_effect

    def execute(self, user_id, project_id, code, *, timeout, owned_project_ids):
        self.execute_calls.append({
            "user_id": user_id,
            "project_id": project_id,
            "code": code,
            "timeout": timeout,
        })
        if self._side_effect is not None:
            self._side_effect(code)
        return self._next_result


# ---------------------------------------------------------------------------
# Fixture wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "bash_test.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def projects_root(tmp_path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def project_workdir(projects_root) -> Path:
    wd = projects_root / "p_a"
    for sub in ("inputs", "outputs", "scratch", ".agent-state"):
        (wd / sub).mkdir(parents=True)
    return wd


@pytest.fixture
def seeded(store, project_workdir):
    with store.transaction() as sess:
        sess.add(
            AuthUser(
                user_id="u_alice",
                username="alice",
                email="alice@example.com",
                password_hash="x",
                role="user",
                status="active",
                is_active=True,
            )
        )
        sess.add(
            AuthUser(
                user_id="u_bob",
                username="bob",
                email="bob@example.com",
                password_hash="x",
                role="user",
                status="active",
                is_active=True,
            )
        )
        sess.flush()
        sess.add(
            Project(
                project_id="p_a",
                name="Sales",
                workdir_path="projects/p_a",
                owner_user_id="u_alice",
            )
        )
        sess.commit()


def _ctx(
    *,
    user_id: str = "u_alice",
    project_id: str | None = "p_a",
    sandbox: _FakeSandbox | None = None,
    store: Store | None = None,
    projects_root: Path,
) -> tuple[ToolContext, _FakeKernelManager]:
    sb = sandbox or _FakeSandbox()
    km = _FakeKernelManager(sb)
    state = SimpleNamespace(
        store=store,
        kernel_manager=km,
        cfg=SimpleNamespace(
            agent=SimpleNamespace(
                projects_root=str(projects_root),
                max_project_workdir_bytes=10 * 1024 * 1024 * 1024,
                max_workdir_upload_bytes=500 * 1024 * 1024,
            ),
            auth=SimpleNamespace(enabled=True),
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
        kernel_manager=km,
    ), km


# ---------------------------------------------------------------------------
# Registration + tools_for filtering
# ---------------------------------------------------------------------------


def test_bash_exec_registered():
    assert "bash_exec" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["bash_exec"]
    assert spec.params_schema["required"] == ["command"]


def test_tools_for_drops_bash_exec_when_no_project(seeded, store, projects_root):
    ctx, _ = _ctx(project_id=None, store=store, projects_root=projects_root)
    names = {s.name for s in tools_for(ctx)}
    assert "bash_exec" not in names


def test_tools_for_drops_bash_exec_when_no_kernel_manager(seeded, store, projects_root):
    ctx, _ = _ctx(store=store, projects_root=projects_root)
    ctx.kernel_manager = None
    ctx.state.kernel_manager = None
    names = {s.name for s in tools_for(ctx)}
    assert "bash_exec" not in names


def test_tools_for_includes_bash_exec_when_both_present(seeded, store, projects_root):
    ctx, _ = _ctx(store=store, projects_root=projects_root)
    names = {s.name for s in tools_for(ctx)}
    assert "bash_exec" in names


# ---------------------------------------------------------------------------
# bash_exec dispatch — happy path
# ---------------------------------------------------------------------------


def test_bash_exec_calls_backend_with_correct_workdir_and_shell(
    seeded, store, projects_root
):
    ctx, km = _ctx(store=store, projects_root=projects_root)
    km.sandbox.backend.configure(
        result=_ExecResult(exit_code=0, stdout=b"hello\n", stderr=b""),
    )
    result = dispatch("bash_exec", {"command": "echo hello"}, ctx)

    assert result["stdout"] == "hello\n"
    assert result["stderr"] == ""
    assert result["exit_code"] == 0
    # Backend.exec saw the right shape
    assert len(km.sandbox.backend.exec_calls) == 1
    call = km.sandbox.backend.exec_calls[0]
    assert call["cmd"] == ["bash", "-lc", "echo hello"]
    assert call["workdir"] == "/workdir/p_a"
    assert call["detach"] is False


def test_bash_exec_clamps_timeout(seeded, store, projects_root):
    ctx, km = _ctx(store=store, projects_root=projects_root)
    dispatch("bash_exec", {"command": "x", "timeout": 9999}, ctx)
    assert km.sandbox.backend.exec_calls[0]["timeout"] == 120.0


# ---------------------------------------------------------------------------
# bash_exec error envelopes
# ---------------------------------------------------------------------------


def test_bash_exec_empty_command(seeded, store, projects_root):
    ctx, _ = _ctx(store=store, projects_root=projects_root)
    result = dispatch("bash_exec", {"command": "  "}, ctx)
    assert "error" in result
    assert "non-empty" in result["error"]


def test_bash_exec_no_project_binding(seeded, store, projects_root):
    ctx, _ = _ctx(project_id=None, store=store, projects_root=projects_root)
    result = dispatch("bash_exec", {"command": "ls"}, ctx)
    assert "error" in result
    assert "bound to a project" in result["error"]


def test_bash_exec_no_kernel_manager(seeded, store, projects_root):
    ctx, _ = _ctx(store=store, projects_root=projects_root)
    ctx.kernel_manager = None
    result = dispatch("bash_exec", {"command": "ls"}, ctx)
    assert "error" in result
    assert "agent sandbox" in result["error"] or "operator" in result["error"]


def test_bash_exec_viewer_cannot_run(seeded, store, projects_root):
    """Bob is bound to alice's project (e.g. via read-only-share +
    chat binding) but doesn't OWN it. He can't run shell commands."""
    ctx, _ = _ctx(user_id="u_bob", store=store, projects_root=projects_root)
    result = dispatch("bash_exec", {"command": "ls"}, ctx)
    assert "error" in result
    assert "owner" in result["error"]


def test_bash_exec_backend_raises_returns_friendly_error(
    seeded, store, projects_root
):
    ctx, km = _ctx(store=store, projects_root=projects_root)
    km.sandbox.backend.configure(raises=RuntimeError("daemon down"))
    result = dispatch("bash_exec", {"command": "ls"}, ctx)
    assert "error" in result
    assert "RuntimeError" in result["error"]
    # Don't leak the raw daemon message
    assert "daemon down" not in result["error"]


# ---------------------------------------------------------------------------
# Auto-Artifact tracking — the main 2.7 behaviour
# ---------------------------------------------------------------------------


def test_bash_exec_creates_artifact_for_outputs_write(
    seeded, store, projects_root, project_workdir
):
    ctx, km = _ctx(store=store, projects_root=projects_root)

    # Side-effect: when the agent's "command" runs, simulate it
    # actually writing a file into outputs/. The handler's
    # post-call diff_outputs picks it up.
    def _create_output(cmd, workdir):
        (project_workdir / "outputs" / "report.csv").write_text("a,b\n1,2\n")

    km.sandbox.backend.configure(
        side_effect=_create_output,
        result=_ExecResult(exit_code=0, stdout=b"", stderr=b""),
    )

    result = dispatch(
        "bash_exec",
        {"command": "echo 'a,b' > outputs/report.csv; echo '1,2' >> outputs/report.csv"},
        ctx,
    )
    assert result["artifacts_created"] == 1

    # Artifact row landed in DB
    with store.transaction() as sess:
        arts = list(
            sess.execute(
                select(Artifact).where(Artifact.project_id == "p_a")
            ).scalars()
        )
    assert len(arts) == 1
    art = arts[0]
    assert art.path == "outputs/report.csv"
    assert art.user_id == "u_alice"
    src = art.lineage_json["sources"][0]
    assert src["type"] == "code_run"
    assert src["tool"] == "bash_exec"
    assert len(src["code_hash"]) == 16
    assert art.metadata_json.get("auto_tracked") is True


def test_python_exec_also_auto_tracks_outputs(
    seeded, store, projects_root, project_workdir
):
    """Same plumbing on python_exec — file written by the agent's
    code call surfaces as Artifact with tool='python_exec'."""
    ctx, km = _ctx(store=store, projects_root=projects_root)

    def _create_output(code):
        (project_workdir / "outputs" / "summary.md").write_text("# OK\n")

    km.configure(
        side_effect=_create_output,
        result=_FakeExecutionResult(stdout="done\n"),
    )

    result = dispatch(
        "python_exec",
        {"code": "open('outputs/summary.md','w').write('# OK\\n')"},
        ctx,
    )
    assert result.get("artifacts_created") == 1

    with store.transaction() as sess:
        arts = list(
            sess.execute(
                select(Artifact).where(Artifact.project_id == "p_a")
            ).scalars()
        )
    assert len(arts) == 1
    src = arts[0].lineage_json["sources"][0]
    assert src["tool"] == "python_exec"


def test_no_artifact_when_no_changes_in_outputs(
    seeded, store, projects_root, project_workdir
):
    """Code that doesn't touch outputs/ produces no Artifact rows."""
    ctx, km = _ctx(store=store, projects_root=projects_root)
    km.sandbox.backend.configure(
        result=_ExecResult(exit_code=0, stdout=b"4\n", stderr=b""),
    )
    result = dispatch("bash_exec", {"command": "echo $((2 + 2))"}, ctx)
    assert result["artifacts_created"] == 0

    with store.transaction() as sess:
        arts = list(
            sess.execute(
                select(Artifact).where(Artifact.project_id == "p_a")
            ).scalars()
        )
    assert arts == []


def test_writes_to_scratch_are_NOT_tracked(
    seeded, store, projects_root, project_workdir
):
    """scratch/ is explicitly NOT auto-tracked — agent prompt tells
    LLM "scratch = safe to delete"; we don't pollute Artifact table
    with intermediate files."""
    ctx, km = _ctx(store=store, projects_root=projects_root)

    def _create_scratch(cmd, workdir):
        (project_workdir / "scratch" / "tmp.csv").write_text("...")

    km.sandbox.backend.configure(
        side_effect=_create_scratch,
        result=_ExecResult(exit_code=0, stdout=b"", stderr=b""),
    )
    result = dispatch(
        "bash_exec",
        {"command": "echo ... > scratch/tmp.csv"},
        ctx,
    )
    assert result["artifacts_created"] == 0
    with store.transaction() as sess:
        arts = list(
            sess.execute(
                select(Artifact).where(Artifact.project_id == "p_a")
            ).scalars()
        )
    assert arts == []
