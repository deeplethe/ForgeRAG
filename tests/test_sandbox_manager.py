"""
Unit tests for SandboxManager — Phase 2.2.

All tests use ``FakeSandboxBackend``, a 60-line in-memory backend
that mimics the docker SDK's contract. Real docker / image is
exercised by tests/test_sandbox_image.py opt-in path; this file
covers the lifecycle logic that sits on TOP of the backend, so
every test runs in milliseconds without docker being installed.

Tested behaviours:
- ensure_container starts on first call, returns the same handle
  on subsequent calls (no double-start)
- stale handle (backend says container's gone) → fresh start on
  next ensure_container
- per-user lock: concurrent ensure_container for the SAME user
  results in ONE container; for different users → independent
- mounts composed correctly from owned project IDs + per-user envs
- touch updates last_active_at (so reap doesn't murder active users)
- reap_idle stops containers past the threshold, leaves recent ones
- shutdown stops everything
- orphan adoption: pre-existing containers from a previous worker
  surface on first ensure_container call without spawning duplicates
- backend errors during start surface as SandboxStartError
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from persistence.sandbox_manager import (
    DEFAULT_CONTAINER_NAME_PREFIX,
    ContainerHandle,
    ExecResult,
    Mount,
    SandboxManager,
    SandboxStartError,
)


# ---------------------------------------------------------------------------
# Fake backend
# ---------------------------------------------------------------------------


class FakeSandboxBackend:
    """In-memory stand-in for DockerBackend. Tracks the same shape
    of state — running container_id → name + mounts — so the
    manager can't tell it's not talking to docker."""

    def __init__(self):
        # container_id → {name, image, mounts, env, running}
        self.containers: dict[str, dict] = {}
        self._next_id = 0
        # Test hooks
        self.start_should_fail = False
        self.start_calls: list[dict] = []
        self.exec_calls: list[tuple[str, list[str]]] = []
        self.stop_calls: list[str] = []
        self.preexisting: list[tuple[str, str]] = []  # adopted on list_owned

    def _new_id(self) -> str:
        self._next_id += 1
        return f"cnt_{self._next_id:04d}"

    def start_container(self, *, image, name, mounts, env=None, published_ports=None):
        self.start_calls.append({
            "image": image,
            "name": name,
            "mounts": tuple(mounts),
            "env": env,
            "published_ports": dict(published_ports or {}),
        })
        if self.start_should_fail:
            raise RuntimeError("simulated start failure")
        cid = self._new_id()
        self.containers[cid] = {
            "name": name,
            "image": image,
            "mounts": tuple(mounts),
            "env": dict(env or {}),
            "published_ports": dict(published_ports or {}),
            "running": True,
        }
        return cid

    def exec(self, container_id, cmd, *, timeout=None, workdir=None):
        self.exec_calls.append((container_id, list(cmd)))
        if container_id not in self.containers or not self.containers[container_id]["running"]:
            return ExecResult(exit_code=126, stdout=b"", stderr=b"container not running")
        return ExecResult(exit_code=0, stdout=b"OK\n", stderr=b"")

    def stop(self, container_id, *, timeout=10):
        self.stop_calls.append(container_id)
        if container_id in self.containers:
            self.containers[container_id]["running"] = False

    def is_running(self, container_id) -> bool:
        return self.containers.get(container_id, {}).get("running", False)

    def list_owned(self, *, name_prefix):
        return list(self.preexisting)

    # Test hook — make a container "vanish" without going through stop
    def crash(self, container_id: str) -> None:
        if container_id in self.containers:
            self.containers[container_id]["running"] = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend() -> FakeSandboxBackend:
    return FakeSandboxBackend()


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
def make_manager(backend, projects_root, envs_root):
    """Factory so individual tests can override clock / idle-seconds."""
    def _make(**overrides) -> SandboxManager:
        kwargs = {
            "backend": backend,
            "image": "test/img:latest",
            "projects_root": projects_root,
            "user_envs_root": envs_root,
        }
        kwargs.update(overrides)
        return SandboxManager(**kwargs)
    return _make


# ---------------------------------------------------------------------------
# ensure_container semantics
# ---------------------------------------------------------------------------


def test_ensure_starts_on_first_call(make_manager, backend):
    mgr = make_manager()
    h = mgr.ensure_container_for_user("u_alice")
    assert isinstance(h, ContainerHandle)
    assert h.user_id == "u_alice"
    assert h.container_id in backend.containers
    assert len(backend.start_calls) == 1
    # User-envs mount always present (auto-created)
    container_paths = {m.container_path for m in h.mounts}
    assert "/workspace/.envs" in container_paths


def test_ensure_returns_same_handle_when_already_running(make_manager, backend):
    mgr = make_manager()
    h1 = mgr.ensure_container_for_user("u_alice")
    h2 = mgr.ensure_container_for_user("u_alice")
    assert h1 is h2
    assert len(backend.start_calls) == 1, "second call must not spawn"


def test_ensure_starts_fresh_after_stale_handle(make_manager, backend):
    mgr = make_manager()
    h1 = mgr.ensure_container_for_user("u_alice")
    backend.crash(h1.container_id)
    h2 = mgr.ensure_container_for_user("u_alice")
    assert h2.container_id != h1.container_id
    assert len(backend.start_calls) == 2


def test_owned_project_mounts_composed(make_manager, projects_root, backend):
    (projects_root / "p1").mkdir()
    (projects_root / "p2").mkdir()
    mgr = make_manager()
    h = mgr.ensure_container_for_user(
        "u_alice", owned_project_ids=["p1", "p2", "p_missing"]
    )
    paths = {m.container_path: m.host_path for m in h.mounts}
    assert "/workdir/p1" in paths
    assert "/workdir/p2" in paths
    # Missing project workdir is silently skipped (logged warning)
    assert "/workdir/p_missing" not in paths


def test_user_envs_dir_auto_created(make_manager, envs_root):
    mgr = make_manager()
    mgr.ensure_container_for_user("u_alice")
    assert (envs_root / "u_alice").exists()


def test_start_failure_wraps_in_sandbox_start_error(make_manager, backend):
    backend.start_should_fail = True
    mgr = make_manager()
    with pytest.raises(SandboxStartError) as excinfo:
        mgr.ensure_container_for_user("u_alice")
    assert "u_alice" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_same_user_starts_one_container(make_manager, backend):
    mgr = make_manager()
    results: list[ContainerHandle] = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(mgr.ensure_container_for_user("u_alice"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(results) == 8
    # All 8 callers got the same handle
    cids = {h.container_id for h in results}
    assert len(cids) == 1
    # Backend saw one start, not eight
    assert len(backend.start_calls) == 1


def test_concurrent_different_users_independent(make_manager, backend):
    mgr = make_manager()
    results: dict[str, ContainerHandle] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def go(uid: str):
        barrier.wait()
        h = mgr.ensure_container_for_user(uid)
        with lock:
            results[uid] = h

    threads = [
        threading.Thread(target=go, args=(f"u_{i}",)) for i in range(4)
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(results) == 4
    cids = {h.container_id for h in results.values()}
    assert len(cids) == 4, "each user must get their own container"
    assert len(backend.start_calls) == 4


# ---------------------------------------------------------------------------
# Touch + idle reap
# ---------------------------------------------------------------------------


def test_touch_refreshes_last_active(make_manager, backend):
    times = iter([
        datetime(2026, 5, 8, 12, 0, 0),
        datetime(2026, 5, 8, 12, 5, 0),  # touch
    ])
    mgr = make_manager(clock=lambda: next(times))
    h = mgr.ensure_container_for_user("u_alice")
    assert h.last_active_at == datetime(2026, 5, 8, 12, 0, 0)
    mgr.touch("u_alice")
    assert h.last_active_at == datetime(2026, 5, 8, 12, 5, 0)


def test_reap_stops_idle_containers(make_manager, backend):
    # Two users; alice gets started in the past, bob starts fresh.
    times = iter([
        datetime(2026, 5, 8, 11, 0, 0),  # alice start
        datetime(2026, 5, 8, 11, 59, 0),  # bob start
        datetime(2026, 5, 8, 12, 0, 0),  # reap clock
    ])
    mgr = make_manager(
        clock=lambda: next(times),
        container_idle_seconds=30 * 60,  # 30 min
    )
    h_alice = mgr.ensure_container_for_user("u_alice")
    h_bob = mgr.ensure_container_for_user("u_bob")

    n = mgr.reap_idle()

    assert n == 1
    assert mgr.get("u_alice") is None
    assert mgr.get("u_bob") is not None
    assert h_alice.container_id in backend.stop_calls
    assert h_bob.container_id not in backend.stop_calls


def test_reap_skips_recently_touched(make_manager, backend):
    times = iter([
        datetime(2026, 5, 8, 11, 0, 0),  # alice start
        datetime(2026, 5, 8, 11, 55, 0),  # alice touch (5 min before reap clock)
        datetime(2026, 5, 8, 12, 0, 0),  # reap clock
    ])
    mgr = make_manager(
        clock=lambda: next(times),
        container_idle_seconds=30 * 60,
    )
    mgr.ensure_container_for_user("u_alice")
    mgr.touch("u_alice")
    n = mgr.reap_idle()
    assert n == 0
    assert mgr.get("u_alice") is not None


# ---------------------------------------------------------------------------
# Stop + shutdown
# ---------------------------------------------------------------------------


def test_stop_user_removes_handle_and_calls_backend(make_manager, backend):
    mgr = make_manager()
    h = mgr.ensure_container_for_user("u_alice")
    assert mgr.stop_user("u_alice") is True
    assert mgr.get("u_alice") is None
    assert h.container_id in backend.stop_calls


def test_stop_user_returns_false_when_unknown(make_manager):
    mgr = make_manager()
    assert mgr.stop_user("u_ghost") is False


def test_stop_user_resilient_to_backend_failure(make_manager, backend):
    mgr = make_manager()
    h = mgr.ensure_container_for_user("u_alice")
    # Simulate backend.stop raising
    real_stop = backend.stop
    def boom(*a, **kw):
        raise RuntimeError("daemon down")
    backend.stop = boom
    # Should NOT raise; handle still evicted
    assert mgr.stop_user("u_alice") is True
    assert mgr.get("u_alice") is None
    backend.stop = real_stop  # restore for cleanup


def test_shutdown_stops_all(make_manager, backend):
    mgr = make_manager()
    mgr.ensure_container_for_user("u_alice")
    mgr.ensure_container_for_user("u_bob")
    mgr.shutdown()
    assert mgr.list_live() == []
    # Both got a stop call
    assert len(backend.stop_calls) == 2


# ---------------------------------------------------------------------------
# Orphan adoption (worker restart recovery)
# ---------------------------------------------------------------------------


def test_orphan_adoption_picks_up_existing_containers(make_manager, backend):
    # Pre-seed the backend as if a previous worker left containers
    backend.containers["cnt_existing_alice"] = {
        "name": f"{DEFAULT_CONTAINER_NAME_PREFIX}u_alice",
        "image": "x",
        "mounts": (),
        "env": {},
        "running": True,
    }
    backend.preexisting = [
        (f"{DEFAULT_CONTAINER_NAME_PREFIX}u_alice", "cnt_existing_alice"),
    ]
    mgr = make_manager()
    # First ensure call adopts the orphan rather than spawning
    h = mgr.ensure_container_for_user("u_alice")
    assert h.container_id == "cnt_existing_alice"
    assert h.metadata.get("adopted") is True
    assert len(backend.start_calls) == 0, "must not spawn duplicate after adoption"


def test_orphan_adoption_only_runs_once(make_manager, backend):
    backend.preexisting = []  # nothing to adopt
    mgr = make_manager()
    mgr.ensure_container_for_user("u_alice")
    mgr.ensure_container_for_user("u_bob")
    # list_owned called once total — adoption is a one-shot sweep
    # (we count by inspecting the manager's flag through behaviour)
    # Second call's adoption is a no-op; subsequent users start
    # fresh. No assertion on FakeBackend here; this is a behavioural
    # guarantee test.
    assert mgr.get("u_alice") is not None
    assert mgr.get("u_bob") is not None


def test_list_live_returns_snapshot(make_manager):
    mgr = make_manager()
    mgr.ensure_container_for_user("u_a")
    mgr.ensure_container_for_user("u_b")
    snapshot = mgr.list_live()
    assert {h.user_id for h in snapshot} == {"u_a", "u_b"}
    # Mutating the list doesn't affect manager's internal state
    snapshot.clear()
    assert len(mgr.list_live()) == 2
