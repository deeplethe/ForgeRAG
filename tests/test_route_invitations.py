"""
Route-level tests for folder invitations.

Owner-side endpoints (``POST/GET/DELETE /folders/{id}/invitations``)
live on the folders router and require auth; recipient-side
endpoints (``GET/POST /auth/invitations/...``) live on the auth
router and bypass the auth middleware (the token in the URL is the
auth).

Auth is disabled in this fixture so the share-permission gate is a
passthrough — the focus here is request shape, error mapping, and
the create → preview → consume → list-without-consumed lifecycle.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_principal, get_state
from api.routes.auth import router as auth_router
from api.routes.folders import router as folders_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser, Folder
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "ri.db")),
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
        for username, email in (("alice", "alice@example.com"), ("bob", "bob@example.com")):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    email=email,
                    password_hash="x",
                    role="user",
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
                owner_user_id=ids["alice"],
            )
        )
        sess.commit()
    return ids


@pytest.fixture
def client(store, seeded):
    from api.auth.authz import AuthorizationService

    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(auth=AuthConfig(enabled=False)),
        store=store,
        authz=AuthorizationService(store),
    )
    fake_principal = AuthenticatedPrincipal(
        user_id=seeded["alice"],
        username="alice",
        role="user",
        via="auth_disabled",
    )
    app = FastAPI()
    app.include_router(folders_router)
    app.include_router(auth_router)
    app.dependency_overrides[get_state] = lambda: fake_state
    app.dependency_overrides[get_principal] = lambda: fake_principal
    with TestClient(app) as c:
        yield c, seeded


# ---------------------------------------------------------------------------
# Owner-side: create / list / revoke
# ---------------------------------------------------------------------------


def test_issue_invitation_returns_url_and_metadata(client):
    c, _ = client
    r = c.post(
        "/api/v1/folders/f_research/invitations",
        json={"email": "newcomer@example.com", "role": "rw"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["folder_path"] == "/research"
    assert body["role"] == "rw"
    assert body["target_email"] == "newcomer@example.com"
    assert body["invitation_url"].startswith("/auth/register?invite=")
    assert body["expires_at"]


def test_issue_invitation_unknown_folder_404(client):
    c, _ = client
    r = c.post(
        "/api/v1/folders/ghost/invitations",
        json={"email": "x@example.com", "role": "r"},
    )
    assert r.status_code == 404


def test_issue_invitation_invalid_role_422(client):
    c, _ = client
    r = c.post(
        "/api/v1/folders/f_research/invitations",
        json={"email": "x@example.com", "role": "admin"},
    )
    assert r.status_code == 422


def test_list_invitations_filters_consumed_by_default(client):
    c, _ = client
    a = c.post(
        "/api/v1/folders/f_research/invitations",
        json={"email": "alpha@example.com", "role": "r"},
    ).json()
    b = c.post(
        "/api/v1/folders/f_research/invitations",
        json={"email": "beta@example.com", "role": "rw"},
    ).json()
    # Consume one (token from the URL).
    token = a["invitation_url"].split("=", 1)[1]
    consume_r = c.post(
        "/api/v1/auth/invitations/consume",
        json={"token": token, "user_id": "u_bob"},
    )
    assert consume_r.status_code == 200, consume_r.text

    rows = c.get("/api/v1/folders/f_research/invitations").json()
    emails = [r["target_email"] for r in rows]
    assert emails == ["beta@example.com"]
    # include_consumed=true surfaces both.
    rows = c.get(
        "/api/v1/folders/f_research/invitations?include_consumed=true"
    ).json()
    ids = {r["invitation_id"] for r in rows}
    assert ids == {a["invitation_id"], b["invitation_id"]}


def test_revoke_invitation_returns_204(client):
    c, _ = client
    issued = c.post(
        "/api/v1/folders/f_research/invitations",
        json={"email": "x@example.com", "role": "r"},
    ).json()
    r = c.delete(
        f"/api/v1/folders/f_research/invitations/{issued['invitation_id']}"
    )
    assert r.status_code == 204
    # After revoke, the token won't preview either.
    token = issued["invitation_url"].split("=", 1)[1]
    pre = c.get(f"/api/v1/auth/invitations/{token}/preview")
    assert pre.status_code == 404


# ---------------------------------------------------------------------------
# Recipient-side: preview / consume
# ---------------------------------------------------------------------------


def test_preview_returns_folder_and_inviter(client):
    c, _ = client
    issued = c.post(
        "/api/v1/folders/f_research/invitations",
        json={"email": "newcomer@example.com", "role": "rw"},
    ).json()
    token = issued["invitation_url"].split("=", 1)[1]

    r = c.get(f"/api/v1/auth/invitations/{token}/preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["folder_path"] == "/research"
    assert body["role"] == "rw"
    assert body["inviter_username"] == "alice"


def test_preview_unknown_token_404(client):
    c, _ = client
    r = c.get("/api/v1/auth/invitations/not-a-real-token/preview")
    assert r.status_code == 404


def test_consume_grants_access_to_redeemer(client):
    c, ids = client
    issued = c.post(
        "/api/v1/folders/f_research/invitations",
        json={"email": "bob@example.com", "role": "rw"},
    ).json()
    token = issued["invitation_url"].split("=", 1)[1]

    r = c.post(
        "/api/v1/auth/invitations/consume",
        json={"token": token, "user_id": ids["bob"]},
    )
    assert r.status_code == 200
    # Bob now appears in /research members.
    members = c.get("/api/v1/folders/f_research/members").json()
    by_user = {m["user_id"]: m["role"] for m in members}
    assert by_user[ids["bob"]] == "rw"


def test_consume_double_redeem_409(client):
    c, ids = client
    issued = c.post(
        "/api/v1/folders/f_research/invitations",
        json={"email": "bob@example.com", "role": "r"},
    ).json()
    token = issued["invitation_url"].split("=", 1)[1]
    c.post(
        "/api/v1/auth/invitations/consume",
        json={"token": token, "user_id": ids["bob"]},
    )
    r = c.post(
        "/api/v1/auth/invitations/consume",
        json={"token": token, "user_id": ids["bob"]},
    )
    assert r.status_code == 409
