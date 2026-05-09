"""
Schema sanity for the folder-as-cwd refactor (S1).

Locks in three things:

1. ``Conversation.cwd_path`` accepts paths and round-trips through
   the store's dict serialiser.
2. ``AgentRun`` and ``Artifact`` can be created with
   ``project_id=NULL`` (was NOT NULL before
   20260518_add_conversation_cwd_path) and ``cwd_path`` set
   instead — that's how new folder-bound rows are anchored.
3. ``project_id=NULL`` rows don't break index lookups by
   ``cwd_path`` (the new index is what the agent-turn endpoints
   will filter by).

A full alembic up/down round-trip lives in the migration's own
test (manually run via ``alembic upgrade/downgrade head``); here
we just exercise the SQLAlchemy mapping + the in-process model
contracts the rest of the codebase relies on.
"""

from __future__ import annotations

import pytest

from config import RelationalConfig, SQLiteConfig
from persistence.models import (
    AgentRun,
    Artifact,
    Conversation,
)
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "cwd-schema.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Conversation.cwd_path
# ---------------------------------------------------------------------------


def test_conversation_accepts_cwd_path(store):
    store.create_conversation(
        {
            "conversation_id": "c1",
            "title": "first chat",
            "user_id": None,
            "project_id": None,
            "cwd_path": "/data/sales/2025",
        }
    )
    got = store.get_conversation("c1")
    assert got is not None
    assert got["cwd_path"] == "/data/sales/2025"
    # project_id is allowed to be NULL alongside cwd_path — folder-
    # as-cwd rows don't need a Project to anchor them.
    assert got["project_id"] is None


def test_conversation_cwd_path_defaults_none(store):
    """Pre-refactor / plain Q&A rows leave cwd_path NULL — that's
    how the route's selector tells "folder chat" from "no folder"
    chat."""
    store.create_conversation(
        {
            "conversation_id": "c2",
            "title": "ephemeral",
            "user_id": None,
            "project_id": None,
        }
    )
    got = store.get_conversation("c2")
    assert got["cwd_path"] is None


def test_conversation_cwd_path_editable_via_update(store):
    """The user can change a conversation's folder mid-stream
    (UI's "switch folder" gesture). Make sure update_conversation
    accepts the field."""
    store.create_conversation(
        {
            "conversation_id": "c3",
            "title": "moveable",
            "user_id": None,
            "project_id": None,
            "cwd_path": "/data/sales/2025/Q1",
        }
    )
    store.update_conversation("c3", cwd_path="/data/sales/2025/Q3")
    got = store.get_conversation("c3")
    assert got["cwd_path"] == "/data/sales/2025/Q3"


# ---------------------------------------------------------------------------
# AgentRun + Artifact relaxed nullability
# ---------------------------------------------------------------------------


def test_agent_run_project_id_now_nullable(store):
    """Was NOT NULL pre-20260518; new folder-as-cwd runs anchor on
    cwd_path instead. Empty project_id must insert cleanly."""
    with store.transaction() as sess:
        sess.add(
            AgentRun(
                run_id="r1",
                project_id=None,
                cwd_path="/data/sales/2025",
                status="ok",
            )
        )
        sess.flush()
        row = sess.get(AgentRun, "r1")
        assert row is not None
        assert row.project_id is None
        assert row.cwd_path == "/data/sales/2025"


def test_artifact_project_id_now_nullable(store):
    """Same nullability change as AgentRun — agent outputs under
    folder-as-cwd live in their cwd folder, not under a Project."""
    with store.transaction() as sess:
        sess.add(
            Artifact(
                artifact_id="a1",
                project_id=None,
                cwd_path="/data/sales/2025",
                path="outputs/chart.png",
                mime="image/png",
                size_bytes=1234,
            )
        )
        sess.flush()
        row = sess.get(Artifact, "a1")
        assert row is not None
        assert row.project_id is None
        assert row.cwd_path == "/data/sales/2025"
        assert row.path == "outputs/chart.png"


def test_legacy_project_bound_rows_still_work(store):
    """Existing rows that pre-date the refactor have a project_id
    + no cwd_path. Verify both shapes coexist after the schema
    change."""
    from persistence.models import AuthUser, Project

    with store.transaction() as sess:
        # The AuthUser is needed because Project.owner_user_id is
        # a non-null FK; the project + run are what we actually want
        # to verify.
        sess.add(
            AuthUser(
                user_id="u_x",
                username="legacy-owner",
                email="x@example.com",
                password_hash="x",
                role="user",
                status="active",
                is_active=True,
            )
        )
        sess.flush()
        sess.add(
            Project(
                project_id="p_legacy",
                name="legacy",
                workdir_path="projects/p_legacy",
                owner_user_id="u_x",
            )
        )
        sess.flush()
        sess.add(
            AgentRun(
                run_id="r_legacy",
                project_id="p_legacy",
                cwd_path=None,
                status="ok",
            )
        )
        sess.flush()
        row = sess.get(AgentRun, "r_legacy")
        assert row.project_id == "p_legacy"
        assert row.cwd_path is None


# ---------------------------------------------------------------------------
# cwd_path indexed for fan-out queries
# ---------------------------------------------------------------------------


def test_cwd_path_query_round_trips(store):
    """Sanity: the new index makes ``WHERE cwd_path = ?`` cheap.
    Test exercises the path through SQLAlchemy + ensures rows are
    actually retrievable by cwd_path."""
    from sqlalchemy import select

    store.create_conversation(
        {"conversation_id": "c_a", "title": "a", "cwd_path": "/foo"}
    )
    store.create_conversation(
        {"conversation_id": "c_b", "title": "b", "cwd_path": "/foo"}
    )
    store.create_conversation(
        {"conversation_id": "c_c", "title": "c", "cwd_path": "/bar"}
    )
    with store.transaction() as sess:
        rows = sess.execute(
            select(Conversation).where(Conversation.cwd_path == "/foo")
        ).scalars().all()
        ids = sorted(r.conversation_id for r in rows)
        assert ids == ["c_a", "c_b"]
