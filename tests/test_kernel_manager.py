"""
Unit tests for KernelManager — Phase 2.3.

All tests use a ``FakeSandboxBackend`` (same one as 2.2) plus a
``FakeKernelClient`` stub that mimics ``BlockingKernelClient``'s
public surface. No docker, no jupyter_client, no real ZMQ — every
test runs in milliseconds.

The opt-in live test (real ipykernel inside a real container)
lives in ``test_phase2_live_kernel.py`` and is skipped without
the sandbox image present.

Coverage:
- get_or_start: starts on first call, reuses on second; uses
  SandboxManager.ensure_container under the hood
- connection file written to <projects>/<pid>/.agent-state/
  with the right shape (5 ports, hmac key, transport=tcp)
- container.exec called with the correct ipykernel_launcher cmd
  pointing at the container-side connection-file path
- _ContainerPortPool: 5-port window allocation, free, exhaustion
- execute: collects stdout / stderr / errors / execution_count
  from a stub iopub stream; returns the result dataclass
- timeout: deadline elapses → timed_out=True, partial output
- touch updates last_active_at on both kernel and underlying
  container
- reap_idle_kernels: stops past-threshold kernels but leaves
  the container alive (container reaping is SandboxManager's job)
- shutdown_kernel / shutdown_all: drop handles + free ports
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from persistence.kernel_manager import (
    DEFAULT_PORT_POOL_SIZE,
    DEFAULT_PORT_POOL_START,
    ExecutionResult,
    KernelHandle,
    KernelManager,
    KernelPortPoolExhausted,
    _ContainerPortPool,
    build_connection_info,
)
from persistence.sandbox_manager import SandboxManager
from tests.test_sandbox_manager import FakeSandboxBackend


# ---------------------------------------------------------------------------
# Fake jupyter_client
# ---------------------------------------------------------------------------


class FakeKernelClient:
    """Mimics ``BlockingKernelClient`` enough to drive
    ``KernelManager``.

    State machine: caller drops messages onto ``iopub_queue`` /
    ``shell_queue`` to simulate kernel responses; ``execute``
    returns a deterministic msg_id. ``wait_for_ready`` is a no-op
    so the boot path is instant.
    """

    def __init__(self):
        self.iopub_queue: list[dict] = []
        self.shell_queue: list[dict] = []
        self.executes: list[str] = []
        self.shutdown_called = False
        self.channels_started = False
        self.connection_file_loaded: str | None = None
        self._next_msg_seq = 0

    # Construction-style hooks the manager expects
    def load_connection_file(self): pass
    def start_channels(self):
        self.channels_started = True
    def stop_channels(self):
        self.channels_started = False
    def wait_for_ready(self, timeout=None): pass
    def shutdown(self, restart=False):
        self.shutdown_called = True

    def execute(self, code, *, store_history=True, allow_stdin=False):
        self.executes.append(code)
        self._next_msg_seq += 1
        return f"msg_{self._next_msg_seq:04d}"

    def get_iopub_msg(self, timeout=None):
        if not self.iopub_queue:
            raise RuntimeError("empty")
        return self.iopub_queue.pop(0)

    def get_shell_msg(self, timeout=None):
        if not self.shell_queue:
            raise RuntimeError("empty")
        return self.shell_queue.pop(0)


def _msg(parent_msg_id: str, mtype: str, content: dict) -> dict:
    return {
        "parent_header": {"msg_id": parent_msg_id},
        "header": {"msg_type": mtype},
        "msg_type": mtype,
        "content": content,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def projects_root(tmp_path) -> Path:
    p = tmp_path / "projects"
    p.mkdir()
    return p


@pytest.fixture
def envs_root(tmp_path) -> Path:
    p = tmp_path / "user-envs"
    p.mkdir()
    return p


@pytest.fixture
def project_workdirs(projects_root) -> dict[str, Path]:
    """Pre-create two project workdirs (and their .agent-state
    subdir which the kernel-manager writes connection files into)."""
    out = {}
    for pid in ("p_a", "p_b"):
        wd = projects_root / pid
        (wd / ".agent-state").mkdir(parents=True)
        out[pid] = wd
    return out


@pytest.fixture
def backend() -> FakeSandboxBackend:
    return FakeSandboxBackend()


@pytest.fixture
def sandbox(backend, projects_root, envs_root) -> SandboxManager:
    return SandboxManager(
        backend=backend,
        image="test/img:latest",
        projects_root=projects_root,
        user_envs_root=envs_root,
    )


@pytest.fixture
def make_kernel_manager(sandbox, projects_root):
    """Factory; tests that need a stub kernel client install one
    via ``client_factory`` to avoid touching real jupyter_client."""

    def _make(client_factory=None, **overrides) -> KernelManager:
        kwargs = {
            "sandbox": sandbox,
            "projects_root": projects_root,
            "boot_timeout_seconds": 1.0,  # fail fast in tests
        }
        kwargs.update(overrides)
        mgr = KernelManager(**kwargs)
        if client_factory is not None:
            mgr._make_kernel_client = client_factory  # type: ignore[assignment]
        return mgr
    return _make


# ---------------------------------------------------------------------------
# _ContainerPortPool
# ---------------------------------------------------------------------------


class TestPortPool:
    def test_allocate_and_free_window(self):
        pool = _ContainerPortPool(start=5000, size=10)
        a = pool.allocate()
        assert a == (5000, 5001, 5002, 5003, 5004)
        b = pool.allocate()
        assert b == (5005, 5006, 5007, 5008, 5009)
        with pytest.raises(KernelPortPoolExhausted):
            pool.allocate()
        pool.free(a)
        c = pool.allocate()
        # Reused the freed window, low-index-first
        assert c == a

    def test_size_must_be_multiple_of_5(self):
        with pytest.raises(ValueError):
            _ContainerPortPool(start=5000, size=12)

    def test_thread_safe(self):
        pool = _ContainerPortPool(start=5000, size=200)  # 40 windows
        results: list[tuple] = []
        lock = threading.Lock()

        def go():
            for _ in range(10):
                p = pool.allocate()
                with lock:
                    results.append(p)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        # 4 threads × 10 = 40 allocations, all distinct
        assert len(results) == 40
        bases = {r[0] for r in results}
        assert len(bases) == 40


# ---------------------------------------------------------------------------
# build_connection_info
# ---------------------------------------------------------------------------


def test_connection_info_shape():
    ci = build_connection_info(ports=(1, 2, 3, 4, 5))
    assert ci["shell_port"] == 1
    assert ci["iopub_port"] == 2
    assert ci["stdin_port"] == 3
    assert ci["control_port"] == 4
    assert ci["hb_port"] == 5
    assert ci["ip"] == "0.0.0.0"
    assert ci["transport"] == "tcp"
    assert ci["signature_scheme"] == "hmac-sha256"
    assert ci["kernel_name"] == "python3"
    # HMAC key: hex, length 64 (256 bits)
    assert isinstance(ci["key"], str) and len(ci["key"]) == 64


# ---------------------------------------------------------------------------
# get_or_start
# ---------------------------------------------------------------------------


def test_get_or_start_writes_connection_file_and_launches(
    make_kernel_manager, project_workdirs, backend, sandbox, projects_root
):
    fake_client = FakeKernelClient()

    def factory(path):
        fake_client.connection_file_loaded = str(path)
        return fake_client

    mgr = make_kernel_manager(client_factory=factory)
    handle = mgr.get_or_start("u_alice", "p_a")

    assert isinstance(handle, KernelHandle)
    assert handle.user_id == "u_alice"
    assert handle.project_id == "p_a"
    assert handle.container_id  # SandboxManager allocated one
    # Connection file written to the right path
    assert handle.connection_file_host.exists()
    assert handle.connection_file_host == (
        projects_root / "p_a" / ".agent-state" / f"kernel-{handle.kernel_id}.json"
    )
    # JSON shape sane
    body = json.loads(handle.connection_file_host.read_text())
    assert body["shell_port"] == handle.ports[0]
    assert body["transport"] == "tcp"
    # Container path the kernel sees
    assert handle.connection_file_container == (
        f"/workdir/p_a/.agent-state/kernel-{handle.kernel_id}.json"
    )
    # Backend.exec called with ipykernel_launcher pointing at the
    # CONTAINER-side path
    assert any(
        cmd[:3] == ["python", "-m", "ipykernel_launcher"]
        and cmd[3] == "-f"
        and cmd[4] == handle.connection_file_container
        for _, cmd in backend.exec_calls
    )


def test_get_or_start_reuses_handle_for_same_pair(
    make_kernel_manager, project_workdirs, backend
):
    fake = FakeKernelClient()
    mgr = make_kernel_manager(client_factory=lambda _path: fake)
    h1 = mgr.get_or_start("u_alice", "p_a")
    h2 = mgr.get_or_start("u_alice", "p_a")
    assert h1 is h2
    # Only one ipykernel launch in the backend exec history
    launch_calls = [
        c for _, c in backend.exec_calls
        if c[:3] == ["python", "-m", "ipykernel_launcher"]
    ]
    assert len(launch_calls) == 1


def test_get_or_start_different_projects_different_kernels(
    make_kernel_manager, project_workdirs, backend
):
    # Each project gets its own fake client (fresh state)
    clients_by_path: dict[str, FakeKernelClient] = {}

    def factory(path):
        c = FakeKernelClient()
        clients_by_path[str(path)] = c
        return c

    mgr = make_kernel_manager(client_factory=factory)
    h_a = mgr.get_or_start("u_alice", "p_a")
    h_b = mgr.get_or_start("u_alice", "p_b")
    # Same container (per-user)
    assert h_a.container_id == h_b.container_id
    # Different kernel IDs + different ports
    assert h_a.kernel_id != h_b.kernel_id
    assert set(h_a.ports).isdisjoint(set(h_b.ports))


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def test_execute_collects_stdout_stderr_and_status(
    make_kernel_manager, project_workdirs
):
    fake = FakeKernelClient()
    mgr = make_kernel_manager(client_factory=lambda _: fake)
    handle = mgr.get_or_start("u_alice", "p_a")

    # Pre-load the iopub queue with a deterministic execution
    msg_id = "msg_0001"  # FakeKernelClient assigns this for the next execute
    fake.iopub_queue = [
        _msg(msg_id, "status", {"execution_state": "busy"}),
        _msg(msg_id, "stream", {"name": "stdout", "text": "hello "}),
        _msg(msg_id, "stream", {"name": "stdout", "text": "world\n"}),
        _msg(msg_id, "execute_result", {
            "execution_count": 7,
            "data": {"text/plain": "42"},
            "metadata": {},
        }),
        _msg(msg_id, "status", {"execution_state": "idle"}),
    ]
    fake.shell_queue = [
        {
            "parent_header": {"msg_id": msg_id},
            "content": {"status": "ok", "execution_count": 7},
        }
    ]

    res = mgr.execute("u_alice", "p_a", "print('hello world'); 42")
    assert isinstance(res, ExecutionResult)
    assert res.stdout == "hello world\n42"
    assert res.stderr == ""
    assert res.error is None
    assert res.execution_count == 7
    assert res.timed_out is False
    # rich_outputs captured the execute_result MIME bundle for 2.5
    assert any(o.get("kind") == "execute_result" for o in res.rich_outputs)


def test_execute_captures_error_from_iopub(
    make_kernel_manager, project_workdirs
):
    fake = FakeKernelClient()
    mgr = make_kernel_manager(client_factory=lambda _: fake)
    mgr.get_or_start("u_alice", "p_a")

    msg_id = "msg_0001"
    fake.iopub_queue = [
        _msg(msg_id, "status", {"execution_state": "busy"}),
        _msg(msg_id, "error", {
            "ename": "NameError",
            "evalue": "name 'foo' is not defined",
            "traceback": ["Traceback...", "NameError: ..."],
        }),
        _msg(msg_id, "status", {"execution_state": "idle"}),
    ]
    res = mgr.execute("u_alice", "p_a", "foo")
    assert res.error is not None
    assert res.error["ename"] == "NameError"
    assert "foo" in res.error["evalue"]


def test_execute_times_out_when_no_idle(
    make_kernel_manager, project_workdirs
):
    fake = FakeKernelClient()
    mgr = make_kernel_manager(client_factory=lambda _: fake)
    mgr.get_or_start("u_alice", "p_a")

    # No status:idle ever sent → execute hits the deadline
    msg_id = "msg_0001"
    fake.iopub_queue = [
        _msg(msg_id, "status", {"execution_state": "busy"}),
        _msg(msg_id, "stream", {"name": "stdout", "text": "started\n"}),
    ]
    res = mgr.execute("u_alice", "p_a", "while True: pass", timeout=0.5)
    assert res.timed_out is True
    assert "started" in res.stdout


def test_execute_ignores_messages_for_other_msg_id(
    make_kernel_manager, project_workdirs
):
    fake = FakeKernelClient()
    mgr = make_kernel_manager(client_factory=lambda _: fake)
    mgr.get_or_start("u_alice", "p_a")

    msg_id = "msg_0001"
    fake.iopub_queue = [
        _msg("msg_OTHER", "stream", {"name": "stdout", "text": "leak\n"}),
        _msg(msg_id, "stream", {"name": "stdout", "text": "ours\n"}),
        _msg(msg_id, "status", {"execution_state": "idle"}),
    ]
    res = mgr.execute("u_alice", "p_a", "print('ours')")
    assert "leak" not in res.stdout
    assert "ours" in res.stdout


# ---------------------------------------------------------------------------
# touch + reap
# ---------------------------------------------------------------------------


def test_touch_updates_last_active(
    make_kernel_manager, project_workdirs
):
    # KernelManager's clock is consulted exactly twice for this
    # flow: once for the kernel-handle started_at + last_active_at
    # (single ``now = self._clock()`` reused), once for touch.
    # SandboxManager uses its own clock (utcnow default) and
    # doesn't consume from this iter.
    times = iter([
        datetime(2026, 5, 9, 10, 0, 0),  # kernel handle started_at + last_active_at
        datetime(2026, 5, 9, 10, 5, 0),  # touch
    ])
    fake = FakeKernelClient()
    mgr = make_kernel_manager(
        client_factory=lambda _: fake,
        clock=lambda: next(times),
    )
    h = mgr.get_or_start("u_alice", "p_a")
    assert h.last_active_at == datetime(2026, 5, 9, 10, 0, 0)
    mgr.touch("u_alice", "p_a")
    assert h.last_active_at == datetime(2026, 5, 9, 10, 5, 0)


def test_reap_idle_kernels_drops_stale(
    make_kernel_manager, project_workdirs
):
    # KernelManager's clock is consulted three times: one for each
    # kernel's started_at/last_active_at (single call each), one
    # for the reap cutoff. Sandbox uses its own clock (utcnow).
    times = iter([
        datetime(2026, 5, 9, 10, 0, 0),  # p_a kernel handle
        datetime(2026, 5, 9, 10, 30, 0),  # p_b kernel handle
        datetime(2026, 5, 9, 10, 30, 1),  # reap clock
    ])
    fakes_by_path: dict[str, FakeKernelClient] = {}

    def factory(path):
        c = FakeKernelClient()
        fakes_by_path[str(path)] = c
        return c

    mgr = make_kernel_manager(
        client_factory=factory,
        clock=lambda: next(times),
        kernel_idle_seconds=10 * 60,  # 10 min
    )
    h_a = mgr.get_or_start("u_alice", "p_a")
    h_b = mgr.get_or_start("u_alice", "p_b")

    n = mgr.reap_idle_kernels()
    assert n == 1
    # p_a's kernel was past 10-min idle threshold → reaped
    live_pids = {h.project_id for h in mgr.list_live()}
    assert live_pids == {"p_b"}
    # Its connection file was cleaned up
    assert not h_a.connection_file_host.exists()
    # Sandbox container survives — kernel reap doesn't touch the
    # outer container (that's SandboxManager.reap_idle's job).
    assert mgr.sandbox.get("u_alice") is not None


def test_shutdown_kernel_frees_port_window(
    make_kernel_manager, project_workdirs
):
    fake = FakeKernelClient()
    mgr = make_kernel_manager(client_factory=lambda _: fake)
    h = mgr.get_or_start("u_alice", "p_a")
    ports_before = h.ports
    assert mgr.shutdown_kernel("u_alice", "p_a") is True
    # Same window now reusable for a new kernel
    h2 = mgr.get_or_start("u_alice", "p_a")
    assert h2.ports == ports_before


def test_shutdown_all_clears_table(make_kernel_manager, project_workdirs):
    factory = lambda _: FakeKernelClient()
    mgr = make_kernel_manager(client_factory=factory)
    mgr.get_or_start("u_alice", "p_a")
    mgr.get_or_start("u_alice", "p_b")
    mgr.shutdown_all()
    assert mgr.list_live() == []


# ---------------------------------------------------------------------------
# published_ports plumbed to SandboxManager
# ---------------------------------------------------------------------------


def test_published_ports_helper_emits_full_range():
    from persistence.kernel_manager import KernelManager

    # No sandbox needed — pure helper
    mgr = KernelManager.__new__(KernelManager)
    mgr.port_pool_start = 35555
    mgr.port_pool_size = 60
    spec = mgr.published_ports_for_container()
    assert len(spec) == 60
    assert spec[35555] == 35555
    assert spec[35614] == 35614  # 35555 + 60 - 1


def test_sandbox_manager_passes_published_ports_through(
    backend, projects_root, envs_root
):
    """When SandboxManager is constructed with published_ports, the
    backend sees them on start_container."""
    pp = {35555: 35555, 35556: 35556}
    sb = SandboxManager(
        backend=backend,
        image="test/img:latest",
        projects_root=projects_root,
        user_envs_root=envs_root,
        published_ports=pp,
    )
    sb.ensure_container_for_user("u_alice")
    assert backend.start_calls[0]["published_ports"] == pp
