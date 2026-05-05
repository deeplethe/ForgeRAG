"""
Route-level tests for /api/v1/admin/users.

Exercises the admin-only gate (auth-disabled bypass + auth-enabled
non-admin 403), the lifecycle transitions (approve / suspend /
reactivate / hard-delete), the self-protection rules
(no-suspend-self, no-demote-self, no-delete-self), and the schema
cascade behaviour on user delete.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_state
from api.routes.admin import router as admin_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthSession, AuthUser, Conversation, Folder
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "adm.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


def _seed(store: Store) -> dict[str, str]:
    """Two admins (so we can test self-protection vs the other admin),
    one pending, one active user."""
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, role, status in (
            ("admin1", "admin", "active"),
            ("admin2", "admin", "active"),
            ("alice", "user", "pending_approval"),
            ("bob", "user", "active"),
        ):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    email=f"{username}@example.com",
                    password_hash="x",
                    role=role,
                    status=status,
                    is_active=(status == "active"),
                )
            )
        sess.commit()
    return ids


def _build_app(
    store: Store,
    *,
    auth_enabled: bool = True,
    principal: AuthenticatedPrincipal | None = None,
) -> FastAPI:
    """Build a focused app with the admin router; injects the principal
    via a Starlette middleware so request.state.principal is set the
    same way the real AuthMiddleware would set it."""
    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        store=store,
    )
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[get_state] = lambda: fake_state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        if principal is not None:
            request.state.principal = principal
        return await call_next(request)

    return app


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_non_admin_principal_403_when_auth_enabled(store):
    """A logged-in but non-admin user can't reach /admin endpoints."""
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["bob"], username="bob", role="user", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.get("/api/v1/admin/users")
    assert r.status_code == 403


def test_no_principal_401(store):
    _seed(store)
    app = _build_app(store, auth_enabled=True, principal=None)
    with TestClient(app) as c:
        r = c.get("/api/v1/admin/users")
    assert r.status_code == 401


def test_auth_disabled_bypass_lets_anyone_in(store):
    """When auth.enabled=false, the middleware would synthesise a
    local-admin principal in production. The /admin gate then
    short-circuits regardless of role."""
    _seed(store)
    principal = AuthenticatedPrincipal(
        user_id="local", username="local", role="admin", via="auth_disabled"
    )
    app = _build_app(store, auth_enabled=False, principal=principal)
    with TestClient(app) as c:
        r = c.get("/api/v1/admin/users")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def test_list_users_filters_by_status(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        rows = c.get("/api/v1/admin/users?status=pending_approval").json()
    assert {r["username"] for r in rows} == {"alice"}


def test_list_users_filters_by_role(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        rows = c.get("/api/v1/admin/users?role=admin").json()
    assert {r["username"] for r in rows} == {"admin1", "admin2"}


def test_get_user_returns_full_record(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        body = c.get(f"/api/v1/admin/users/{ids['alice']}").json()
    assert body["username"] == "alice"
    assert body["status"] == "pending_approval"
    assert body["is_active"] is False


def test_get_unknown_user_404(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.get("/api/v1/admin/users/ghost")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


def test_approve_pending_user(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(f"/api/v1/admin/users/{ids['alice']}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["is_active"] is True


def test_approve_already_active_is_idempotent(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(f"/api/v1/admin/users/{ids['bob']}/approve")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_approve_rejects_non_pending(store):
    """Suspended users use /reactivate, not /approve."""
    ids = _seed(store)
    with store.transaction() as sess:
        u = sess.get(AuthUser, ids["bob"])
        u.status = "suspended"
        u.is_active = False
        sess.commit()
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(f"/api/v1/admin/users/{ids['bob']}/approve")
    assert r.status_code == 409


def test_suspend_active_user_revokes_sessions(store):
    """Suspending kicks the user out of every active session
    immediately (revoked_at on each row)."""
    ids = _seed(store)
    with store.transaction() as sess:
        sess.add(
            AuthSession(session_id="s_bob", user_id=ids["bob"])
        )
        sess.commit()
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(f"/api/v1/admin/users/{ids['bob']}/suspend")
    assert r.status_code == 200
    assert r.json()["status"] == "suspended"
    with store.transaction() as sess:
        s = sess.get(AuthSession, "s_bob")
        assert s.revoked_at is not None


def test_cannot_suspend_self(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(f"/api/v1/admin/users/{ids['admin1']}/suspend")
    assert r.status_code == 400


def test_reactivate_suspended_user(store):
    ids = _seed(store)
    with store.transaction() as sess:
        u = sess.get(AuthUser, ids["bob"])
        u.status = "suspended"
        u.is_active = False
        sess.commit()
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(f"/api/v1/admin/users/{ids['bob']}/reactivate")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


# ---------------------------------------------------------------------------
# PATCH (role / display_name)
# ---------------------------------------------------------------------------


def test_patch_changes_role_and_display_name(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.patch(
            f"/api/v1/admin/users/{ids['bob']}",
            json={"role": "admin", "display_name": "Bob B."},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert body["display_name"] == "Bob B."


def test_cannot_demote_self(store):
    """admin1 trying to demote admin1 → user is rejected."""
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.patch(
            f"/api/v1/admin/users/{ids['admin1']}",
            json={"role": "user"},
        )
    assert r.status_code == 400


def test_other_admin_can_demote(store):
    """admin2 demoting admin1 is fine — last-admin lock-out is the
    operator's responsibility (we only block self-demote)."""
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin2"], username="admin2", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.patch(
            f"/api/v1/admin/users/{ids['admin1']}",
            json={"role": "user"},
        )
    assert r.status_code == 200
    assert r.json()["role"] == "user"


# ---------------------------------------------------------------------------
# DELETE — hard delete + cascade behaviour
# ---------------------------------------------------------------------------


def test_delete_user_204(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.delete(f"/api/v1/admin/users/{ids['bob']}")
    assert r.status_code == 204
    with store.transaction() as sess:
        assert sess.get(AuthUser, ids["bob"]) is None


def test_delete_unknown_idempotent_204(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.delete("/api/v1/admin/users/ghost")
    assert r.status_code == 204


def test_cannot_delete_self(store):
    ids = _seed(store)
    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.delete(f"/api/v1/admin/users/{ids['admin1']}")
    assert r.status_code == 400


def test_delete_cascades_per_schema(store):
    """Conversations the user owned get hard-deleted (CASCADE);
    folders they owned have owner_user_id set to NULL — admin
    cleans up via transfer-ownership later."""
    ids = _seed(store)
    with store.transaction() as sess:
        sess.add(
            Conversation(
                conversation_id="c_bob_1",
                title="Bob's chat",
                user_id=ids["bob"],
            )
        )
        sess.add(
            Folder(
                folder_id="f_bob_proj",
                path="/bob_proj",
                path_lower="/bob_proj",
                parent_id="__root__",
                name="bob_proj",
                owner_user_id=ids["bob"],
            )
        )
        sess.commit()

    principal = AuthenticatedPrincipal(
        user_id=ids["admin1"], username="admin1", role="admin", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        c.delete(f"/api/v1/admin/users/{ids['bob']}")

    with store.transaction() as sess:
        # Conversation cascaded.
        assert sess.get(Conversation, "c_bob_1") is None
        # Folder remains, owner nulled out — admin will transfer later.
        f = sess.get(Folder, "f_bob_proj")
        assert f is not None
        assert f.owner_user_id is None
