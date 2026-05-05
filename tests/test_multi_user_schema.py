"""Schema-level tests for the multi-user migration (S1).

These cover the new columns / tables added by the
``20260505_multi_user`` migration, plus the bootstrap path that
takes ownership of ``__root__`` for the auto-provisioned admin.

We don't run alembic in unit tests — the schema is established via
``Base.metadata.create_all()`` in ``Store.ensure_schema``. The
generic ``Store._migrate_add_columns`` then adds anything missing
on existing DBs. Both paths exercise the new model fields.

S2/S3/S4 will add behaviour tests on top of this; for now the
contract is: the columns exist, they accept the documented values,
the bootstrap admin lands as the owner of __root__, and the
deprecated ``folder_grants`` seed row is gone.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select

from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import (
    AuthToken,
    AuthUser,
    Conversation,
    File,
    Folder,
    FolderInvitation,
)
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "mu.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Column existence
# ---------------------------------------------------------------------------


def test_auth_users_has_new_columns(store: Store):
    insp = inspect(store._engine)
    cols = {c["name"] for c in insp.get_columns("auth_users")}
    assert {"email", "display_name", "status"} <= cols


def test_auth_tokens_has_scope_columns(store: Store):
    insp = inspect(store._engine)
    cols = {c["name"] for c in insp.get_columns("auth_tokens")}
    assert {"scope_path", "scope_role"} <= cols


def test_folders_has_shared_with(store: Store):
    insp = inspect(store._engine)
    cols = {c["name"] for c in insp.get_columns("folders")}
    assert "shared_with" in cols
    # ``owner_user_id`` was dropped in 20260506_drop_folder_owner —
    # ``shared_with`` is the sole authz field on folders now.
    assert "owner_user_id" not in cols


def test_documents_has_owner_user_id(store: Store):
    insp = inspect(store._engine)
    cols = {c["name"] for c in insp.get_columns("documents")}
    assert "owner_user_id" in cols


def test_files_has_owner_user_id(store: Store):
    insp = inspect(store._engine)
    cols = {c["name"] for c in insp.get_columns("files")}
    assert "owner_user_id" in cols


def test_conversations_has_user_id(store: Store):
    insp = inspect(store._engine)
    cols = {c["name"] for c in insp.get_columns("conversations")}
    assert "user_id" in cols


def test_folder_invitations_table_exists(store: Store):
    insp = inspect(store._engine)
    assert insp.has_table("folder_invitations")
    cols = {c["name"] for c in insp.get_columns("folder_invitations")}
    assert {
        "invitation_id",
        "folder_id",
        "inviter_user_id",
        "target_email",
        "role",
        "token_hash",
        "expires_at",
        "consumed_at",
    } <= cols


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_auth_user_default_status_is_active(store: Store):
    with store.transaction() as sess:
        u = AuthUser(
            user_id="u1",
            username="alice",
            password_hash="x",
        )
        sess.add(u)
        sess.flush()
        # Default applied via server_default at INSERT time on commit;
        # make sure we re-read.
        sess.commit()

    with store.transaction() as sess:
        row = sess.execute(select(AuthUser).where(AuthUser.user_id == "u1")).scalar_one()
        assert row.status == "active"
        assert row.email is None
        assert row.display_name is None


def test_folder_default_shared_with_is_empty(store: Store):
    # __root__ is seeded by Store.ensure_schema via _seed_system_folders.
    with store.transaction() as sess:
        root = sess.get(Folder, "__root__")
    assert root is not None
    # SQLite stores JSON as text; SQLAlchemy decodes back to list.
    assert root.shared_with == []


def test_auth_token_scope_columns_default_null(store: Store):
    with store.transaction() as sess:
        u = AuthUser(user_id="u1", username="alice", password_hash="x")
        sess.add(u)
        sess.flush()
        t = AuthToken(
            token_id="t1",
            user_id="u1",
            name="cli",
            token_hash="h",
            hash_prefix="hp",
        )
        sess.add(t)
        sess.commit()

    with store.transaction() as sess:
        row = sess.execute(select(AuthToken).where(AuthToken.token_id == "t1")).scalar_one()
        assert row.scope_path is None
        assert row.scope_role is None


# ---------------------------------------------------------------------------
# Folder shared_with round-trips JSON correctly
# ---------------------------------------------------------------------------


def test_folder_shared_with_round_trips(store: Store):
    with store.transaction() as sess:
        f = Folder(
            folder_id="f_team",
            path="/team",
            path_lower="/team",
            parent_id="__root__",
            name="team",
            shared_with=[
                {"user_id": "u1", "role": "rw"},
                {"user_id": "u2", "role": "r"},
            ],
        )
        sess.add(f)
        sess.commit()

    with store.transaction() as sess:
        row = sess.get(Folder, "f_team")
        assert row.shared_with == [
            {"user_id": "u1", "role": "rw"},
            {"user_id": "u2", "role": "r"},
        ]


# ---------------------------------------------------------------------------
# Bootstrap — creates an active admin row; admin reaches everything
# via role bypass (no per-folder ownership needed)
# ---------------------------------------------------------------------------


def test_bootstrap_creates_active_admin(store: Store):
    from api.auth.bootstrap import bootstrap_if_empty

    class _Cfg:
        auth = AuthConfig(enabled=True, initial_password="opencraig")

    bootstrap_if_empty(_Cfg, store)

    with store.transaction() as sess:
        admin = sess.execute(
            select(AuthUser).where(AuthUser.username == "admin")
        ).scalar_one()
        assert admin.role == "admin"
        assert admin.status == "active"
        assert admin.is_active is True

        # __root__ has no shared_with — admin doesn't need a grant
        # to reach it; ``can()`` short-circuits on role=admin.
        root = sess.get(Folder, "__root__")
        assert root.shared_with == []


def test_bootstrap_is_idempotent(store: Store):
    """Second call must not duplicate the admin."""
    from api.auth.bootstrap import bootstrap_if_empty

    class _Cfg:
        auth = AuthConfig(enabled=True, initial_password="opencraig")

    bootstrap_if_empty(_Cfg, store)
    bootstrap_if_empty(_Cfg, store)

    with store.transaction() as sess:
        users = sess.execute(select(AuthUser)).scalars().all()
        assert len(users) == 1


def test_bootstrap_skips_when_auth_disabled(store: Store):
    from api.auth.bootstrap import bootstrap_if_empty

    class _Cfg:
        auth = AuthConfig(enabled=False)

    bootstrap_if_empty(_Cfg, store)

    with store.transaction() as sess:
        users = sess.execute(select(AuthUser)).scalars().all()
        assert users == []


# ---------------------------------------------------------------------------
# Deprecated folder_grants seed row is gone
# ---------------------------------------------------------------------------


def test_folder_grants_table_not_required(store: Store):
    """The model was removed; seed no longer inserts the bootstrap row.
    Fresh DBs should not blow up if the table is absent."""
    insp = inspect(store._engine)
    # On a fresh test DB created via Base.metadata.create_all() the
    # folder_grants table simply doesn't exist any more — the model
    # was deleted.
    assert not insp.has_table("folder_grants")


# ---------------------------------------------------------------------------
# Folder invitation
# ---------------------------------------------------------------------------


def test_folder_invitation_round_trip(store: Store):
    from datetime import datetime, timedelta

    with store.transaction() as sess:
        u = AuthUser(user_id="u1", username="alice", password_hash="x")
        sess.add(u)
        f = Folder(
            folder_id="f1",
            path="/proj",
            path_lower="/proj",
            parent_id="__root__",
            name="proj",
        )
        sess.add(f)
        sess.flush()

        inv = FolderInvitation(
            invitation_id="inv1",
            folder_id="f1",
            inviter_user_id="u1",
            target_email="bob@example.com",
            role="rw",
            token_hash="h" * 64,
            expires_at=datetime.utcnow() + timedelta(days=7),
        )
        sess.add(inv)
        sess.commit()

    with store.transaction() as sess:
        row = sess.execute(
            select(FolderInvitation).where(FolderInvitation.invitation_id == "inv1")
        ).scalar_one()
        assert row.target_email == "bob@example.com"
        assert row.role == "rw"
        assert row.consumed_at is None
        assert row.consumed_by_user_id is None


# ---------------------------------------------------------------------------
# Conversation user_id is nullable + indexed
# ---------------------------------------------------------------------------


def test_conversation_user_id_nullable(store: Store):
    with store.transaction() as sess:
        c = Conversation(conversation_id="c1", title="legacy")
        sess.add(c)
        sess.commit()

    with store.transaction() as sess:
        row = sess.get(Conversation, "c1")
        assert row.user_id is None  # legacy conversations stay un-owned


def test_file_owner_user_id_round_trips(store: Store):
    with store.transaction() as sess:
        u = AuthUser(user_id="u1", username="alice", password_hash="x")
        sess.add(u)
        sess.flush()
        f = File(
            file_id="f1",
            content_hash="abc",
            storage_key="local/f1",
            original_name="x.pdf",
            display_name="x.pdf",
            size_bytes=10,
            mime_type="application/pdf",
            owner_user_id="u1",
        )
        sess.add(f)
        sess.commit()

    with store.transaction() as sess:
        row = sess.get(File, "f1")
        assert row.owner_user_id == "u1"
