"""
Route-level tests for ``GET /api/v1/auth/me`` and ``PATCH /api/v1/auth/me``.

The PATCH endpoint is the user-side complement to admin user
patching: a regular user can rename themselves (display_name),
but cannot touch role / status / email — those stay on the
admin surface (``api/routes/admin.py``).

These tests exercise the four behaviours the frontend depends on:

  1. Setting display_name persists and is reflected in the
     response + future GET /me calls.
  2. Empty / whitespace-only display_name resets the column to
     NULL so the GET /me fallback chain (email-prefix → username)
     re-engages — important so the avatar / chat label keep
     working after a user "clears" their display name.
  3. role / status are NOT writable from this endpoint (Pydantic
     drops unknown fields, so the request succeeds but the row
     is unchanged — a regular user cannot self-promote).
  4. 401 on no principal — the endpoint can't be reached
     anonymously even when auth is disabled in the test rig.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_state
from api.routes.auth import router as auth_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "me.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


def _seed(store: Store) -> str:
    """Single regular user — enough to exercise the self-edit path."""
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
        sess.commit()
    return "u_alice"


def _build_app(
    store: Store,
    *,
    principal: AuthenticatedPrincipal | None = None,
) -> FastAPI:
    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True)),
        store=store,
    )
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_state] = lambda: fake_state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        if principal is not None:
            request.state.principal = principal
        return await call_next(request)

    return app


def test_patch_me_sets_display_name(store):
    uid = _seed(store)
    p = AuthenticatedPrincipal(
        user_id=uid, username="alice", role="user", via="session"
    )
    app = _build_app(store, principal=p)
    with TestClient(app) as c:
        r = c.patch("/api/v1/auth/me", json={"display_name": "Alice Smith"})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Alice Smith"

    # Verify it persisted across requests, not just in the response.
    with TestClient(app) as c:
        r2 = c.get("/api/v1/auth/me")
    assert r2.json()["display_name"] == "Alice Smith"


def test_patch_me_blank_name_resets_to_fallback(store):
    """Empty / whitespace-only display_name nulls the column —
    GET /me then falls back to email-prefix ('alice')."""
    uid = _seed(store)
    p = AuthenticatedPrincipal(
        user_id=uid, username="alice", role="user", via="session"
    )
    app = _build_app(store, principal=p)
    with TestClient(app) as c:
        c.patch("/api/v1/auth/me", json={"display_name": "Temporary"})
        r = c.patch("/api/v1/auth/me", json={"display_name": "   "})
    assert r.status_code == 200, r.text
    # Fallback chain kicks back in.
    assert r.json()["display_name"] == "alice"


def test_patch_me_ignores_role_field(store):
    """Sending role='admin' to /auth/me must NOT promote the user.
    Pydantic's PatchMeReq doesn't declare ``role``, so it's silently
    dropped — defence-in-depth on top of the dedicated /admin gate."""
    uid = _seed(store)
    p = AuthenticatedPrincipal(
        user_id=uid, username="alice", role="user", via="session"
    )
    app = _build_app(store, principal=p)
    with TestClient(app) as c:
        r = c.patch(
            "/api/v1/auth/me",
            json={"display_name": "Alice", "role": "admin"},
        )
    assert r.status_code == 200, r.text
    # The /me response reflects the principal's role, which the test
    # rig fixed at "user". If the row had been promoted, the next
    # request (with the same principal) would still show "user" too —
    # but checking the DB directly is the unambiguous assertion.
    with store.transaction() as sess:
        user = sess.get(AuthUser, uid)
        assert user.role == "user"


def test_patch_me_unauthenticated_401(store):
    _seed(store)
    app = _build_app(store, principal=None)
    with TestClient(app) as c:
        r = c.patch("/api/v1/auth/me", json={"display_name": "x"})
    assert r.status_code == 401
