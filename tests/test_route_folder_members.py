"""
Route-level tests for /api/v1/folders/{id}/members.

Uses TestClient + the share-service fixture from S3.a (alice owns
/research, bob owns /legal with carol shared as r). Auth is
disabled in these tests so the middleware synthesises a local
admin and the share-permission gate becomes a passthrough — we're
testing the wiring (request shape, error mapping, cascade visible
through the API) rather than the authz check itself (covered by
test_route_authz.py).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_principal, get_state
from api.routes.folders import router as folders_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser, Folder
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixture: minimal app with only the folder routes mounted, plus a
# fake AppState that exposes the two attributes the routes touch
# (cfg.auth.enabled and store).
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "rfm.db")),
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
        for username, role, email in (
            ("admin", "admin", "admin@example.com"),
            ("alice", "user", "alice@example.com"),
            ("bob", "user", "bob@example.com"),
            ("carol", "user", "carol@example.com"),
            ("dan", "user", "dan@example.com"),
        ):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    email=email,
                    password_hash="x",
                    role=role,
                    status="active",
                    is_active=True,
                )
            )
        sess.flush()

        # __root__ has no shared_with — admin reaches it via role bypass.
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
        sess.add(
            Folder(
                folder_id="f_legal",
                path="/legal",
                path_lower="/legal",
                parent_id="__root__",
                name="legal",
                shared_with=[
                    {"user_id": ids["bob"], "role": "rw"},
                    {"user_id": ids["carol"], "role": "r"},
                ],
            )
        )
        sess.flush()
        sess.add(
            Folder(
                folder_id="f_legal_contracts",
                path="/legal/contracts",
                path_lower="/legal/contracts",
                parent_id="f_legal",
                name="contracts",
                shared_with=[
                    {"user_id": ids["bob"], "role": "rw"},
                    {"user_id": ids["carol"], "role": "r"},
                ],
            )
        )
        sess.commit()
    return ids


@pytest.fixture
def client(store, seeded):
    """Build a focused FastAPI app with only the folders routes and
    a fake state. Auth gate is disabled for these tests."""
    from api.auth.authz import AuthorizationService

    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=False)),
        store=store,
        authz=AuthorizationService(store),
    )
    fake_principal = AuthenticatedPrincipal(
        user_id=seeded["admin"],
        username="admin",
        role="admin",
        via="auth_disabled",
    )

    app = FastAPI()
    app.include_router(folders_router)
    app.dependency_overrides[get_state] = lambda: fake_state
    app.dependency_overrides[get_principal] = lambda: fake_principal

    with TestClient(app) as c:
        yield c, seeded


# ---------------------------------------------------------------------------
# GET /folders/{id}/members
# ---------------------------------------------------------------------------


def test_list_members_returns_direct_grants(client):
    c, ids = client
    r = c.get("/api/v1/folders/f_legal/members")
    assert r.status_code == 200, r.text
    rows = r.json()
    by_user = {m["user_id"]: m for m in rows}
    assert by_user[ids["bob"]]["role"] == "rw"
    assert by_user[ids["bob"]]["source"] == "direct"
    assert by_user[ids["carol"]]["role"] == "r"
    assert by_user[ids["carol"]]["source"] == "direct"


def test_list_members_marks_inherited_on_subfolder(client):
    c, ids = client
    r = c.get("/api/v1/folders/f_legal_contracts/members")
    assert r.status_code == 200
    rows = r.json()
    by_user = {m["user_id"]: m for m in rows}
    # Carol's row was cascaded down from /legal during fixture setup
    # (mirroring real-world create-subfolder copy behaviour). The
    # service detects this and labels her as inherited.
    assert by_user[ids["carol"]]["source"] == "inherited:f_legal"


def test_list_members_unknown_folder_404(client):
    c, _ = client
    r = c.get("/api/v1/folders/ghost/members")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /folders/{id}/members
# ---------------------------------------------------------------------------


def test_add_member_by_email(client):
    c, ids = client
    r = c.post(
        "/api/v1/folders/f_research/members",
        json={"email": "dan@example.com", "role": "rw"},
    )
    assert r.status_code == 201, r.text
    rows = r.json()
    by_user = {m["user_id"]: m["role"] for m in rows}
    assert by_user[ids["dan"]] == "rw"


def test_add_member_unknown_email_404(client):
    c, _ = client
    r = c.post(
        "/api/v1/folders/f_research/members",
        json={"email": "ghost@example.com", "role": "r"},
    )
    assert r.status_code == 404


def test_add_member_invalid_role_422(client):
    c, _ = client
    r = c.post(
        "/api/v1/folders/f_research/members",
        json={"email": "dan@example.com", "role": "admin"},
    )
    assert r.status_code == 422  # pydantic regex rejection


def test_add_member_cascades_visible_on_subfolder(client):
    """After granting dan rw on /legal, his row appears as inherited
    on /legal/contracts."""
    c, ids = client
    r = c.post(
        "/api/v1/folders/f_legal/members",
        json={"email": "dan@example.com", "role": "rw"},
    )
    assert r.status_code == 201

    sub = c.get("/api/v1/folders/f_legal_contracts/members").json()
    by_user = {m["user_id"]: m for m in sub}
    assert by_user[ids["dan"]]["role"] == "rw"
    assert by_user[ids["dan"]]["source"] == "inherited:f_legal"


# ---------------------------------------------------------------------------
# PATCH /folders/{id}/members/{user_id}
# ---------------------------------------------------------------------------


def test_patch_member_role(client):
    c, ids = client
    # Add dan with r first.
    c.post(
        "/api/v1/folders/f_research/members",
        json={"email": "dan@example.com", "role": "r"},
    )
    # Upgrade to rw.
    r = c.patch(
        f"/api/v1/folders/f_research/members/{ids['dan']}",
        json={"role": "rw"},
    )
    assert r.status_code == 200
    by_user = {m["user_id"]: m["role"] for m in r.json()}
    assert by_user[ids["dan"]] == "rw"


def test_patch_inherited_member_409(client):
    """Carol's grant on /legal/contracts is inherited from /legal.
    Editing it on /legal/contracts must be rejected with 409 — the
    UI should send the request to /legal instead."""
    c, ids = client
    r = c.patch(
        f"/api/v1/folders/f_legal_contracts/members/{ids['carol']}",
        json={"role": "rw"},
    )
    assert r.status_code == 409, r.text
    assert "inherited" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# DELETE /folders/{id}/members/{user_id}
# ---------------------------------------------------------------------------


def test_delete_member_drops_grant(client):
    c, ids = client
    c.post(
        "/api/v1/folders/f_research/members",
        json={"email": "dan@example.com", "role": "rw"},
    )
    r = c.delete(f"/api/v1/folders/f_research/members/{ids['dan']}")
    assert r.status_code == 200
    by_user = {m["user_id"] for m in r.json()}
    assert ids["dan"] not in by_user


def test_delete_member_with_ancestor_grant_409(client):
    """Carol is in /legal's shared_with. Trying to remove her from
    /legal/contracts (where she's only inherited from above) is
    rejected — admin has to remove from /legal first or move
    /legal/contracts out from under it."""
    c, ids = client
    r = c.delete(f"/api/v1/folders/f_legal_contracts/members/{ids['carol']}")
    assert r.status_code == 409, r.text
    # Body names the offending ancestor.
    assert "f_legal" in r.json()["detail"]


def test_delete_last_rw_member_is_allowed(client):
    """Removing the last rw member of a folder is allowed; admin can
    still manage it via role bypass. There's no special-case
    protection — the admin is responsible for not locking themselves
    + their team out of a folder."""
    c, ids = client
    r = c.delete(f"/api/v1/folders/f_research/members/{ids['alice']}")
    assert r.status_code == 200
    assert ids["alice"] not in {m["user_id"] for m in r.json()}
