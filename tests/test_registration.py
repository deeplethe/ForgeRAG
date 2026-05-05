"""
Self-registration tests — service layer + route integration.

Three behaviours that must hold across every mode:

  * Input validation: bad email / username / short password reject
    before policy is consulted.
  * First-user-becomes-admin: when no active admin exists, the
    first ``register_user`` call promotes the registrant to admin
    + active regardless of registration_mode. Concurrent attempts
    are gated by the username UNIQUE index (only one INSERT wins).
  * Invitation tokens always grant active status atomically with
    consuming the invitation. invite_only mode rejects without a
    valid token; open mode lets them through; approval mode marks
    them pending_approval.

Login behaviour is tested in passing — pending_approval / suspended
status produces a precise 403 from the login endpoint.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth.registration import (
    EmailTaken,
    InvalidEmail,
    InvalidUsername,
    InvitationProblem,
    RegistrationClosed,
    UsernameTaken,
    WeakPassword,
    register_user,
)
from api.deps import get_state
from api.routes.auth import router as auth_router
from api.routes.folders import router as folders_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.invitation_service import FolderInvitationService
from persistence.models import AuthUser, Folder
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "reg.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


def _cfg(mode: str = "approval") -> SimpleNamespace:
    """Minimal cfg shim with just ``cfg.auth.registration_mode``."""
    return SimpleNamespace(
        auth=AuthConfig(enabled=True, registration_mode=mode)  # type: ignore[arg-type]
    )


def _seed_admin(store: Store) -> str:
    """Helper: drop in an existing active admin so the
    first-registration override doesn't fire."""
    with store.transaction() as sess:
        u = AuthUser(
            user_id="u_admin",
            username="admin",
            email="admin@example.com",
            password_hash="x",
            role="admin",
            status="active",
            is_active=True,
        )
        sess.add(u)
        sess.commit()
    return "u_admin"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_email", ["", "not-an-email", "a@b", "@example.com"])
def test_register_rejects_bad_email(store, bad_email):
    with store.transaction() as sess:
        with pytest.raises(InvalidEmail):
            register_user(
                cfg=_cfg(), sess=sess,
                email=bad_email, username="alice", password="abcdefgh",
            )


@pytest.mark.parametrize("bad_username", ["ab", "with spaces", "with.dot", "x" * 33])
def test_register_rejects_bad_username(store, bad_username):
    with store.transaction() as sess:
        with pytest.raises(InvalidUsername):
            register_user(
                cfg=_cfg(), sess=sess,
                email="a@b.com", username=bad_username, password="abcdefgh",
            )


def test_register_rejects_short_password(store):
    with store.transaction() as sess:
        with pytest.raises(WeakPassword):
            register_user(
                cfg=_cfg(), sess=sess,
                email="a@b.com", username="alice", password="short",
            )


def test_register_rejects_duplicate_email(store):
    _seed_admin(store)
    with store.transaction() as sess:
        register_user(
            cfg=_cfg("open"), sess=sess,
            email="alice@example.com", username="alice", password="abcdefgh",
        )
        sess.commit()
    with store.transaction() as sess, pytest.raises(EmailTaken):
        register_user(
            cfg=_cfg("open"), sess=sess,
            email="alice@example.com", username="alice2", password="abcdefgh",
        )


def test_register_rejects_duplicate_username(store):
    _seed_admin(store)
    with store.transaction() as sess:
        register_user(
            cfg=_cfg("open"), sess=sess,
            email="alice@example.com", username="alice", password="abcdefgh",
        )
        sess.commit()
    with store.transaction() as sess, pytest.raises(UsernameTaken):
        register_user(
            cfg=_cfg("open"), sess=sess,
            email="alice2@example.com", username="alice", password="abcdefgh",
        )


# ---------------------------------------------------------------------------
# First-user-becomes-admin override
# ---------------------------------------------------------------------------


def test_first_register_is_promoted_to_admin_open(store):
    """In open mode + empty auth_users, first registration is admin."""
    with store.transaction() as sess:
        result = register_user(
            cfg=_cfg("open"), sess=sess,
            email="first@example.com", username="first", password="abcdefgh",
        )
        sess.commit()
    assert result.role == "admin"
    assert result.status == "active"


def test_first_register_promoted_even_in_approval_mode(store):
    """approval mode would normally produce pending_approval, but
    the empty-table override fires first."""
    with store.transaction() as sess:
        result = register_user(
            cfg=_cfg("approval"), sess=sess,
            email="first@example.com", username="first", password="abcdefgh",
        )
        sess.commit()
    assert result.role == "admin"
    assert result.status == "active"


def test_first_register_promoted_even_in_invite_only(store):
    """No active admin → first registration takes over even when
    the mode would otherwise reject."""
    with store.transaction() as sess:
        result = register_user(
            cfg=_cfg("invite_only"), sess=sess,
            email="first@example.com", username="first", password="abcdefgh",
        )
        sess.commit()
    assert result.role == "admin"
    assert result.status == "active"


def test_second_register_falls_back_to_normal_flow(store):
    """After the first admin is created, the next registration goes
    through the configured mode — approval here means
    pending_approval."""
    with store.transaction() as sess:
        register_user(
            cfg=_cfg("approval"), sess=sess,
            email="first@example.com", username="first", password="abcdefgh",
        )
        sess.commit()
    with store.transaction() as sess:
        second = register_user(
            cfg=_cfg("approval"), sess=sess,
            email="second@example.com", username="second", password="abcdefgh",
        )
        sess.commit()
    assert second.role == "user"
    assert second.status == "pending_approval"


def test_register_promoted_when_existing_admin_is_suspended(store):
    """An admin who's been suspended doesn't count as 'active admin'.
    Next registration takes over."""
    with store.transaction() as sess:
        sess.add(
            AuthUser(
                user_id="u_dead",
                username="dead_admin",
                password_hash="x",
                role="admin",
                status="suspended",
                is_active=False,
            )
        )
        sess.commit()
    with store.transaction() as sess:
        result = register_user(
            cfg=_cfg("approval"), sess=sess,
            email="rescue@example.com", username="rescue", password="abcdefgh",
        )
        sess.commit()
    assert result.role == "admin"
    assert result.status == "active"


# ---------------------------------------------------------------------------
# Mode policy (with admin already in place)
# ---------------------------------------------------------------------------


def test_open_mode_active_immediately(store):
    _seed_admin(store)
    with store.transaction() as sess:
        result = register_user(
            cfg=_cfg("open"), sess=sess,
            email="alice@example.com", username="alice", password="abcdefgh",
        )
        sess.commit()
    assert result.role == "user"
    assert result.status == "active"


def test_approval_mode_starts_pending(store):
    _seed_admin(store)
    with store.transaction() as sess:
        result = register_user(
            cfg=_cfg("approval"), sess=sess,
            email="alice@example.com", username="alice", password="abcdefgh",
        )
        sess.commit()
    assert result.status == "pending_approval"


def test_invite_only_mode_rejects_without_token(store):
    _seed_admin(store)
    with store.transaction() as sess, pytest.raises(RegistrationClosed):
        register_user(
            cfg=_cfg("invite_only"), sess=sess,
            email="alice@example.com", username="alice", password="abcdefgh",
        )


# ---------------------------------------------------------------------------
# Invitation token integration
# ---------------------------------------------------------------------------


def _seed_invitation(store, *, role: str = "rw") -> str:
    """Create an admin + folder + invitation, returns the raw token."""
    with store.transaction() as sess:
        sess.add(
            AuthUser(
                user_id="u_admin",
                username="admin",
                email="admin@example.com",
                password_hash="x",
                role="admin",
                status="active",
                is_active=True,
            )
        )
        sess.flush()
        sess.add(
            Folder(
                folder_id="f_research",
                path="/research",
                path_lower="/research",
                parent_id="__root__",
                name="research",
                shared_with=[{"user_id": "u_admin", "role": "rw"}],
            )
        )
        sess.flush()
        issued = FolderInvitationService(sess).create(
            folder_id="f_research",
            target_email="alice@example.com",
            role=role,  # type: ignore[arg-type]
            inviter_user_id="u_admin",
        )
        sess.commit()
    return issued.token


def test_invitation_lands_active_in_approval_mode(store):
    token = _seed_invitation(store)
    with store.transaction() as sess:
        result = register_user(
            cfg=_cfg("approval"),  # mode wants pending — invitation overrides
            sess=sess,
            email="alice@example.com", username="alice", password="abcdefgh",
            invitation_token=token,
        )
        sess.commit()
    assert result.status == "active"
    assert result.redeemed_folder_path == "/research"

    # Grant landed in shared_with via FolderShareService cascade.
    with store.transaction() as sess:
        f = sess.get(Folder, "f_research")
        from persistence.folder_share_service import _grant_for
        assert _grant_for(f.shared_with, result.user_id) == "rw"


def test_invitation_lets_invite_only_through(store):
    token = _seed_invitation(store, role="r")
    with store.transaction() as sess:
        result = register_user(
            cfg=_cfg("invite_only"),
            sess=sess,
            email="alice@example.com", username="alice", password="abcdefgh",
            invitation_token=token,
        )
        sess.commit()
    assert result.status == "active"


def test_bad_invitation_token_raises(store):
    _seed_admin(store)
    with store.transaction() as sess, pytest.raises(InvitationProblem):
        register_user(
            cfg=_cfg("approval"),
            sess=sess,
            email="alice@example.com", username="alice", password="abcdefgh",
            invitation_token="not-a-real-token",
        )


# ---------------------------------------------------------------------------
# Route integration: login status gate + register endpoint shape
# ---------------------------------------------------------------------------


@pytest.fixture
def app_client(store, tmp_path):
    """A test client over the auth + folders routers with auth
    enabled in cfg, so login + status checks fire for real."""
    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True, registration_mode="open")),
        store=store,
    )
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(folders_router)
    app.dependency_overrides[get_state] = lambda: fake_state
    with TestClient(app) as c:
        yield c, store


def test_route_register_returns_201_open_mode(app_client):
    c, _ = app_client
    r = c.post(
        "/api/v1/auth/register",
        json={
            "email": "alice@example.com",
            "username": "alice",
            "password": "abcdefgh",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "active"
    # First-user-becomes-admin: empty table → admin.
    assert body["role"] == "admin"


def test_route_register_409_on_duplicate_username(app_client):
    c, _ = app_client
    c.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "username": "alice", "password": "abcdefgh"},
    )
    r = c.post(
        "/api/v1/auth/register",
        json={"email": "b@example.com", "username": "alice", "password": "abcdefgh"},
    )
    assert r.status_code == 409


def test_route_login_rejects_pending_approval(store):
    """A pending user has the right password but can't log in until
    an admin approves. The endpoint returns 403 with a precise
    message (not 401, which would be ambiguous with a wrong
    password)."""
    _seed_admin(store)
    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True, registration_mode="approval")),
        store=store,
    )
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_state] = lambda: fake_state
    with TestClient(app) as c:
        c.post(
            "/api/v1/auth/register",
            json={
                "email": "alice@example.com",
                "username": "alice",
                "password": "abcdefgh",
            },
        )
        r = c.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "abcdefgh"},
        )
    assert r.status_code == 403
    assert "pending" in r.json()["detail"].lower()


def test_route_login_works_for_active_user_via_email(store):
    """Email login works after activation."""
    _seed_admin(store)
    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True, registration_mode="open")),
        store=store,
    )
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_state] = lambda: fake_state
    with TestClient(app) as c:
        c.post(
            "/api/v1/auth/register",
            json={
                "email": "alice@example.com",
                "username": "alice",
                "password": "abcdefgh",
            },
        )
        # Login by email.
        r = c.post(
            "/api/v1/auth/login",
            json={"username": "alice@example.com", "password": "abcdefgh"},
        )
    assert r.status_code == 200
    assert r.json()["username"] == "alice"
