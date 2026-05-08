"""
Route-level tests for the Chat ↔ Project binding (Phase 1.5).

Covers:
  - POST /api/v1/conversations with project_id stores the column
  - The created Conversation surfaces project_id on GET / list
  - Bob cannot bind a chat to alice's private project (404)
  - Viewer (read-only share) CAN bind a chat to a project they
    can read (read-only consultant scenario)
  - PATCH can rebind / unbind via the project_id field
  - Empty string ('') unbinds explicitly
  - GET /conversations?project_id=<pid> filters correctly
  - Cross-user privacy still holds (alice can't see bob's chats
    even when filtering by a shared project)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_principal, get_state
from api.routes.conversations import router as conversations_router
from api.routes.projects import router as projects_router
from config import RelationalConfig, SQLiteConfig
from config.agent import AgentConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "rcb.db")),
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
        for username, email in (
            ("alice", "alice@example.com"),
            ("bob", "bob@example.com"),
        ):
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
        sess.commit()
    return ids


@pytest.fixture
def projects_root(tmp_path) -> Path:
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_client(store, projects_root, principal_user_id):
    from api.auth.authz import AuthorizationService

    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(
            auth=AuthConfig(enabled=True),
            agent=AgentConfig(projects_root=str(projects_root)),
        ),
        store=store,
        authz=AuthorizationService(store),
    )
    fake_principal = AuthenticatedPrincipal(
        user_id=principal_user_id,
        username=principal_user_id.removeprefix("u_"),
        role="user",
        via="cookie",
    )
    app = FastAPI()
    app.include_router(projects_router)
    app.include_router(conversations_router)
    app.dependency_overrides[get_state] = lambda: fake_state
    app.dependency_overrides[get_principal] = lambda: fake_principal
    return TestClient(app)


@pytest.fixture
def alice_client(store, seeded, projects_root):
    return _make_client(store, projects_root, seeded["alice"])


@pytest.fixture
def bob_client(store, seeded, projects_root):
    return _make_client(store, projects_root, seeded["bob"])


@pytest.fixture
def alice_project(alice_client) -> str:
    r = alice_client.post("/api/v1/projects", json={"name": "Sales work"})
    return r.json()["project_id"]


# ---------------------------------------------------------------------------
# Create + read
# ---------------------------------------------------------------------------


def test_create_conversation_with_project_id(alice_client, alice_project):
    r = alice_client.post(
        "/api/v1/conversations",
        json={"title": "Q3 review", "project_id": alice_project},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["project_id"] == alice_project
    assert body["title"] == "Q3 review"

    # GET round-trips it
    r = alice_client.get(f"/api/v1/conversations/{body['conversation_id']}")
    assert r.status_code == 200
    assert r.json()["project_id"] == alice_project


def test_create_conversation_without_project_id_works(alice_client):
    r = alice_client.post("/api/v1/conversations", json={"title": "Plain Q&A"})
    assert r.status_code == 201
    assert r.json()["project_id"] is None


def test_create_conversation_empty_project_id_treated_as_none(alice_client):
    r = alice_client.post(
        "/api/v1/conversations",
        json={"title": "Whitespace edge", "project_id": "   "},
    )
    assert r.status_code == 201
    assert r.json()["project_id"] is None


def test_bob_cannot_bind_to_alices_private_project(bob_client, alice_project):
    r = bob_client.post(
        "/api/v1/conversations",
        json={"title": "Hijack attempt", "project_id": alice_project},
    )
    # 404 — same code as missing project, no existence leak
    assert r.status_code == 404


def test_viewer_can_bind_to_shared_project(
    alice_client, bob_client, alice_project
):
    """Read-only share scenario: bob is invited as a viewer of
    alice's project, so bob can chat *against* the project even
    though he can't write into the workdir. The agent's writes
    will still 404 at the file-API layer downstream — Phase 2's
    job, not Phase 1.5's."""
    alice_client.post(
        f"/api/v1/projects/{alice_project}/members",
        json={"email": "bob@example.com"},
    )
    r = bob_client.post(
        "/api/v1/conversations",
        json={"title": "Looking around", "project_id": alice_project},
    )
    assert r.status_code == 201
    assert r.json()["project_id"] == alice_project


# ---------------------------------------------------------------------------
# PATCH rebind / unbind
# ---------------------------------------------------------------------------


def test_patch_unbind_via_empty_string(alice_client, alice_project):
    cid = alice_client.post(
        "/api/v1/conversations",
        json={"title": "Bound", "project_id": alice_project},
    ).json()["conversation_id"]
    # Unbind via ""
    r = alice_client.patch(
        f"/api/v1/conversations/{cid}",
        json={"project_id": ""},
    )
    assert r.status_code == 200
    assert r.json()["project_id"] is None


def test_patch_rebind_to_different_project(alice_client):
    p1 = alice_client.post(
        "/api/v1/projects", json={"name": "First"}
    ).json()["project_id"]
    p2 = alice_client.post(
        "/api/v1/projects", json={"name": "Second"}
    ).json()["project_id"]
    cid = alice_client.post(
        "/api/v1/conversations",
        json={"title": "Hop projects", "project_id": p1},
    ).json()["conversation_id"]
    r = alice_client.patch(
        f"/api/v1/conversations/{cid}",
        json={"project_id": p2},
    )
    assert r.status_code == 200
    assert r.json()["project_id"] == p2


def test_patch_rebind_to_unauthorized_project_404(
    alice_client, bob_client, alice_project
):
    """Bob has his own chat; tries to rebind it to alice's project.
    404 — same code as missing project."""
    cid = bob_client.post(
        "/api/v1/conversations", json={"title": "Bob's"}
    ).json()["conversation_id"]
    r = bob_client.patch(
        f"/api/v1/conversations/{cid}",
        json={"project_id": alice_project},
    )
    assert r.status_code == 404


def test_patch_does_not_change_unrelated_fields(alice_client, alice_project):
    cid = alice_client.post(
        "/api/v1/conversations",
        json={"title": "Original", "project_id": alice_project},
    ).json()["conversation_id"]
    # Toggle is_favorite without touching project_id; binding survives
    r = alice_client.patch(
        f"/api/v1/conversations/{cid}",
        json={"is_favorite": True},
    )
    assert r.status_code == 200
    assert r.json()["project_id"] == alice_project
    assert r.json()["is_favorite"] is True


# ---------------------------------------------------------------------------
# Filter by project_id
# ---------------------------------------------------------------------------


def test_list_filtered_by_project_id(alice_client, alice_project):
    bound1 = alice_client.post(
        "/api/v1/conversations",
        json={"title": "Bound 1", "project_id": alice_project},
    ).json()["conversation_id"]
    bound2 = alice_client.post(
        "/api/v1/conversations",
        json={"title": "Bound 2", "project_id": alice_project},
    ).json()["conversation_id"]
    # Plain chat outside the project
    alice_client.post("/api/v1/conversations", json={"title": "Plain"})

    r = alice_client.get(
        "/api/v1/conversations", params={"project_id": alice_project}
    )
    assert r.status_code == 200
    body = r.json()
    ids = {c["conversation_id"] for c in body["items"]}
    assert ids == {bound1, bound2}
    assert body["total"] == 2


def test_list_filtered_by_unknown_project_returns_empty(alice_client):
    """Filter by a project the caller can't read silently returns
    [] — same privacy stance as the rest of the conversations API
    (we don't confirm whether a project the caller can't see exists)."""
    r = alice_client.get(
        "/api/v1/conversations", params={"project_id": "ghost_project_id"}
    )
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["total"] == 0


def test_list_without_project_filter_unchanged(alice_client, alice_project):
    """The Phase-0 list behaviour (all of caller's chats, newest first)
    must keep working when project_id is omitted."""
    alice_client.post(
        "/api/v1/conversations",
        json={"title": "Bound", "project_id": alice_project},
    )
    alice_client.post("/api/v1/conversations", json={"title": "Plain"})
    r = alice_client.get("/api/v1/conversations")
    assert r.status_code == 200
    assert r.json()["total"] == 2


def test_cross_user_filter_does_not_leak(
    alice_client, bob_client, alice_project
):
    """Even when alice shares a project with bob and bob has a chat
    bound to it, alice's filter on the same project_id only sees
    HER chats (the user-privacy filter still applies)."""
    alice_client.post(
        f"/api/v1/projects/{alice_project}/members",
        json={"email": "bob@example.com"},
    )
    bob_client.post(
        "/api/v1/conversations",
        json={"title": "Bob's chat in shared", "project_id": alice_project},
    )
    alice_client.post(
        "/api/v1/conversations",
        json={"title": "Alice's chat in shared", "project_id": alice_project},
    )
    r = alice_client.get(
        "/api/v1/conversations", params={"project_id": alice_project}
    )
    items = r.json()["items"]
    titles = {c["title"] for c in items}
    assert titles == {"Alice's chat in shared"}
