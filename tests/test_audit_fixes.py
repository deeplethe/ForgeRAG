"""
Regression tests for the May 2026 Phase 2 audit's "fix now" items.

#3 — ``_list_owned_project_ids`` caches on ToolContext (per-turn).
     Hitting it twice with the same ctx must NOT re-query the DB.

#4 — ``SandboxManager._lock_for`` uses ``setdefault`` (atomic in
     CPython), removing the table-lock bottleneck that serialised
     every user's ``ensure_container_for_user`` through one mutex.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")


# ---------------------------------------------------------------------------
# Audit #3 — _list_owned_project_ids caches on ToolContext
# ---------------------------------------------------------------------------


class _CountingStore:
    """Counts how many times ``transaction()`` is opened — that's
    what the cache miss looks like from the outside."""

    def __init__(self, project_ids: list[str]):
        self.transaction_calls = 0
        self.project_ids = project_ids

    def transaction(self):
        self.transaction_calls += 1
        store = self

        class _Sess:
            def __enter__(self_): return self_
            def __exit__(self_, *args): return False
            def execute(self_, _stmt):
                class _Sc:
                    def __init__(self_, ids):
                        self_.ids = ids
                    def scalars(self_):
                        # Mimic ORM rows with trashed_metadata=None
                        rows = []
                        for pid in self_.ids:
                            rows.append(SimpleNamespace(
                                project_id=pid, trashed_metadata=None,
                            ))
                        return iter(rows)
                return _Sc(store.project_ids)
        return _Sess()


def test_list_owned_project_ids_caches_per_ctx():
    from api.agent.dispatch import ToolContext
    from api.agent.tools import _list_owned_project_ids

    store = _CountingStore(["p_a", "p_b"])
    state = SimpleNamespace(store=store)
    ctx = ToolContext(
        state=state,
        principal=SimpleNamespace(user_id="u_alice"),
        accessible=set(),
        path_filters=None,
        allowed_doc_ids=None,
        project_id="p_a",
        kernel_manager=None,
    )

    # Five calls within the same context = one DB hit
    for _ in range(5):
        out = _list_owned_project_ids(state, "u_alice", ctx=ctx)
        assert out == ("p_a", "p_b")
    assert store.transaction_calls == 1


def test_list_owned_project_ids_no_ctx_no_cache():
    """Without a ctx (e.g. background tasks calling the helper
    directly), each call re-queries — caller chose not to cache."""
    from api.agent.tools import _list_owned_project_ids

    store = _CountingStore(["p_x"])
    state = SimpleNamespace(store=store)
    for _ in range(3):
        _list_owned_project_ids(state, "u_x", ctx=None)
    assert store.transaction_calls == 3


def test_list_owned_project_ids_separate_ctxs_dont_share():
    """Two ToolContexts (e.g. two concurrent chat turns) each have
    their own cache; one filling doesn't pollute the other."""
    from api.agent.dispatch import ToolContext
    from api.agent.tools import _list_owned_project_ids

    store = _CountingStore(["p_alpha"])
    state = SimpleNamespace(store=store)

    def _ctx() -> ToolContext:
        return ToolContext(
            state=state,
            principal=SimpleNamespace(user_id="u_x"),
            accessible=set(),
            path_filters=None,
            allowed_doc_ids=None,
            project_id="p_alpha",
            kernel_manager=None,
        )

    c1, c2 = _ctx(), _ctx()
    _list_owned_project_ids(state, "u_x", ctx=c1)
    _list_owned_project_ids(state, "u_x", ctx=c2)
    # Each ctx warmed independently
    assert store.transaction_calls == 2
    # And subsequent calls in each are cached
    _list_owned_project_ids(state, "u_x", ctx=c1)
    _list_owned_project_ids(state, "u_x", ctx=c2)
    assert store.transaction_calls == 2


# ---------------------------------------------------------------------------
# Audit #4 — _lock_for no longer takes the table lock
# ---------------------------------------------------------------------------


def test_lock_for_does_not_acquire_table_lock():
    """Smoking gun: hold the table lock externally and call
    _lock_for in another thread — pre-fix it would deadlock; post-
    fix it returns immediately."""
    from persistence.sandbox_manager import SandboxManager

    class _NoOpBackend:
        def start_container(self, **kw): return "x"
        def exec(self, *a, **kw): pass
        def stop(self, *a, **kw): pass
        def is_running(self, *a, **kw): return True
        def list_owned(self, **kw): return []

    mgr = SandboxManager(
        backend=_NoOpBackend(),
        image="x",
        projects_root="/tmp",
        user_envs_root="/tmp",
    )

    # Hold the table lock externally and from another thread try to
    # get a per-user lock. Pre-fix this deadlocks (or at least
    # blocks); post-fix it returns immediately because setdefault
    # is the only operation, no table-lock acquisition.
    result: list[Any] = []

    def _other_thread():
        lock = mgr._lock_for("u_other")
        result.append(lock)

    with mgr._table_lock:
        t = threading.Thread(target=_other_thread)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive(), (
            "_lock_for blocked while table lock held — fix didn't land"
        )
    assert len(result) == 1
    assert isinstance(result[0], type(threading.Lock()))


def test_lock_for_returns_same_lock_per_user():
    """Sanity: setdefault correctness — same user_id gets same
    Lock instance across calls."""
    from persistence.sandbox_manager import SandboxManager

    class _NoOpBackend:
        def start_container(self, **kw): return "x"
        def exec(self, *a, **kw): pass
        def stop(self, *a, **kw): pass
        def is_running(self, *a, **kw): return True
        def list_owned(self, **kw): return []

    mgr = SandboxManager(
        backend=_NoOpBackend(),
        image="x",
        projects_root="/tmp",
        user_envs_root="/tmp",
    )
    a1 = mgr._lock_for("u_alice")
    a2 = mgr._lock_for("u_alice")
    b = mgr._lock_for("u_bob")
    assert a1 is a2
    assert a1 is not b


def test_lock_for_thread_safe_under_concurrent_creation():
    """100 threads all asking for the same user_id's lock should
    all get the same Lock instance — setdefault is atomic, no
    accidental double-construction."""
    from persistence.sandbox_manager import SandboxManager

    class _NoOpBackend:
        def start_container(self, **kw): return "x"
        def exec(self, *a, **kw): pass
        def stop(self, *a, **kw): pass
        def is_running(self, *a, **kw): return True
        def list_owned(self, **kw): return []

    mgr = SandboxManager(
        backend=_NoOpBackend(),
        image="x",
        projects_root="/tmp",
        user_envs_root="/tmp",
    )
    results: list = []
    barrier = threading.Barrier(100)

    def _grab():
        barrier.wait()
        results.append(mgr._lock_for("u_swarm"))

    threads = [threading.Thread(target=_grab) for _ in range(100)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len({id(lock) for lock in results}) == 1, (
        "100 threads got different lock instances — setdefault "
        "lost atomicity somehow"
    )
