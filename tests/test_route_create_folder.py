"""
Route-level tests for ``POST /api/v1/folders`` — verifying the
multi-user authz wiring on subfolder creation.

What must hold:

  1. The new folder copies the parent's ``shared_with`` so the
     creator (who must already have ``rw`` on the parent to write
     under it) inherits the same access on the subfolder.
  2. Non-rw users on the parent get 403 before the folder is
     created.
  3. Admin role bypasses the per-folder check.
  4. Auth-disabled deployments still produce a folder (the
     synthetic ``local`` principal trivially passes the gate).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal, AuthorizationService
from api.deps import get_state
from api.routes.folders import router as folders_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.folder_share_service import _grant_for
from persistence.models import AuthUser, Folder
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "rcf.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict[str, str]:
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, role in (
            ("admin", "admin"),
            ("alice", "user"),
            ("bob", "user"),
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
                shared_with=[{"user_id": ids["alice"], "role": "rw"}],
            )
        )
        sess.commit()
    return ids


def _build_app(
    store: Store,
    *,
    auth_enabled: bool,
    principal: AuthenticatedPrincipal,
) -> FastAPI:
    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        store=store,
        authz=AuthorizationService(store),
    )
    app = FastAPI()
    app.include_router(folders_router)
    app.dependency_overrides[get_state] = lambda: fake_state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    return app


def test_creator_inherits_parent_shared_with(store, seeded):
    """Alice has rw on /research; she creates /research/2024 — the new
    folder copies the parent's shared_with so she keeps her rw."""
    principal = AuthenticatedPrincipal(
        user_id=seeded["alice"],
        username="alice",
        role="user",
        via="session",
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/folders",
            json={"parent_path": "/research", "name": "2024"},
        )
    assert r.status_code == 201, r.text
    new_id = r.json()["folder_id"]
    with store.transaction() as sess:
        f = sess.get(Folder, new_id)
        assert _grant_for(f.shared_with, seeded["alice"]) == "rw"


def test_create_under_other_users_folder_403(store, seeded):
    """bob has no rights on alice's /research; creating a subfolder
    under it must be rejected before the row is written."""
    principal = AuthenticatedPrincipal(
        user_id=seeded["bob"], username="bob", role="user", via="session"
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/folders",
            json={"parent_path": "/research", "name": "intruder"},
        )
    assert r.status_code == 403


def test_admin_can_create_anywhere(store, seeded):
    """Admin role bypasses the per-folder write check."""
    principal = AuthenticatedPrincipal(
        user_id=seeded["admin"],
        username="admin",
        role="admin",
        via="session",
    )
    app = _build_app(store, auth_enabled=True, principal=principal)
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/folders",
            json={"parent_path": "/research", "name": "admin_drop"},
        )
    assert r.status_code == 201
    new_id = r.json()["folder_id"]
    with store.transaction() as sess:
        f = sess.get(Folder, new_id)
        # Inherits /research's shared_with → alice still has rw.
        assert _grant_for(f.shared_with, seeded["alice"]) == "rw"


def test_auth_disabled_creates_folder_with_inherited_share(store, seeded):
    """With auth off the synthetic 'local' admin can create anywhere;
    the new folder still copies the parent's shared_with."""
    principal = AuthenticatedPrincipal(
        user_id="local", username="local", role="admin", via="auth_disabled"
    )
    app = _build_app(store, auth_enabled=False, principal=principal)
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/folders",
            json={"parent_path": "/research", "name": "freshly_made"},
        )
    assert r.status_code == 201
    new_id = r.json()["folder_id"]
    with store.transaction() as sess:
        f = sess.get(Folder, new_id)
        # Parent had alice:rw → inherited.
        assert _grant_for(f.shared_with, seeded["alice"]) == "rw"
