"""
Route-level tests for /api/v1/projects.

Phase-0 contract: list / create / get / rename / soft-delete +
member CRUD + the workdir-on-disk side effect. Auth is enabled
(state.cfg.auth.enabled = True) and we drive the routes through
TestClient with two principals (alice + bob) so the
"alice-creates → shares-with-bob → bob-sees-it" roundtrip is
covered against the real authz path, not the local-admin bypass.
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
from api.routes.projects import router as projects_router
from config import RelationalConfig, SQLiteConfig
from config.agent import AgentConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser, Project
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "rp.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict[str, str]:
    """Three users: alice + bob + carol (all 'user' role)."""
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, email in (
            ("alice", "alice@example.com"),
            ("bob", "bob@example.com"),
            ("carol", "carol@example.com"),
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


def _make_client(store: Store, projects_root: Path, principal_user_id: str):
    """Build a TestClient mounting the projects router with a
    principal override hard-coded to ``principal_user_id``. The
    test creates one client per acting user.
    """
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
    app.dependency_overrides[get_state] = lambda: fake_state
    app.dependency_overrides[get_principal] = lambda: fake_principal
    return TestClient(app)


@pytest.fixture
def alice_client(store, seeded, projects_root):
    return _make_client(store, projects_root, seeded["alice"])


@pytest.fixture
def bob_client(store, seeded, projects_root):
    return _make_client(store, projects_root, seeded["bob"])


# ---------------------------------------------------------------------------
# Create + list
# ---------------------------------------------------------------------------


def test_create_project_returns_201_and_owner_role(alice_client, projects_root):
    r = alice_client.post(
        "/api/v1/projects",
        json={"name": "Q3 contracts", "description": "Tracker run"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Q3 contracts"
    assert body["description"] == "Tracker run"
    assert body["role"] == "owner"
    assert body["member_count"] == 0
    # workdir scaffolded on disk
    workdir = projects_root / body["project_id"]
    assert workdir.exists()
    for sub in ("inputs", "outputs", "scratch", ".agent-state"):
        assert (workdir / sub).is_dir(), f"{sub} not created"
    assert (workdir / "README.md").exists()


def test_create_with_blank_name_422(alice_client):
    r = alice_client.post("/api/v1/projects", json={"name": "   "})
    assert r.status_code == 422


def test_list_projects_visible_to_owner_only_until_shared(
    alice_client, bob_client, projects_root
):
    r = alice_client.post("/api/v1/projects", json={"name": "Alice's project"})
    assert r.status_code == 201
    project_id = r.json()["project_id"]

    # Alice sees it
    rows = alice_client.get("/api/v1/projects").json()
    assert {p["project_id"] for p in rows} == {project_id}

    # Bob does not
    rows = bob_client.get("/api/v1/projects").json()
    assert rows == []


# ---------------------------------------------------------------------------
# Detail / 404 on no-access
# ---------------------------------------------------------------------------


def test_get_project_404_when_not_a_member(alice_client, bob_client):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Hidden"}
    ).json()["project_id"]
    r = bob_client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Update (rename + description)
# ---------------------------------------------------------------------------


def test_update_project_rename_and_describe(alice_client):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Initial"}
    ).json()["project_id"]
    r = alice_client.patch(
        f"/api/v1/projects/{pid}",
        json={"name": "Renamed", "description": "Now with prose"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "Now with prose"


def test_update_project_blocked_for_non_member(alice_client, bob_client):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Locked"}
    ).json()["project_id"]
    r = bob_client.patch(
        f"/api/v1/projects/{pid}", json={"name": "Hijacked"}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Membership roundtrip — the "two-user share happy path" the doc calls out
# ---------------------------------------------------------------------------


def test_share_then_bob_sees_project(alice_client, bob_client, store):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Joint review"}
    ).json()["project_id"]

    # Alice adds bob as rw
    r = alice_client.post(
        f"/api/v1/projects/{pid}/members",
        json={"email": "bob@example.com", "role": "rw"},
    )
    assert r.status_code == 201, r.text
    members = r.json()
    by_uid = {m["user_id"]: m for m in members}
    assert by_uid["u_alice"]["role"] == "owner"
    assert by_uid["u_bob"]["role"] == "rw"

    # Bob now sees the project on his list
    bob_rows = bob_client.get("/api/v1/projects").json()
    assert {p["project_id"] for p in bob_rows} == {pid}
    assert bob_rows[0]["role"] == "rw"

    # Bob can fetch detail
    r = bob_client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["role"] == "rw"

    # Bob can edit (rw allows write)
    r = bob_client.patch(
        f"/api/v1/projects/{pid}", json={"description": "Bob updated"}
    )
    assert r.status_code == 200

    # Bob cannot share (only owner / admin)
    r = bob_client.post(
        f"/api/v1/projects/{pid}/members",
        json={"email": "carol@example.com", "role": "r"},
    )
    assert r.status_code == 404, "non-owner share attempt must 404"

    # Audit log captured the share
    with store.transaction() as sess:
        from persistence.models import AuditLogRow

        actions = [
            r.action
            for r in sess.execute(
                __import__("sqlalchemy").select(AuditLogRow).where(
                    AuditLogRow.target_id == pid
                )
            ).scalars()
        ]
    assert "project.create" in actions
    assert "project.share" in actions


def test_update_member_role_then_remove(alice_client, bob_client):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Member churn"}
    ).json()["project_id"]
    alice_client.post(
        f"/api/v1/projects/{pid}/members",
        json={"email": "bob@example.com", "role": "rw"},
    )

    # Demote bob to read-only
    r = alice_client.patch(
        f"/api/v1/projects/{pid}/members/u_bob",
        json={"role": "r"},
    )
    assert r.status_code == 200, r.text
    by_uid = {m["user_id"]: m for m in r.json()}
    assert by_uid["u_bob"]["role"] == "r"

    # Bob can read
    r = bob_client.get(f"/api/v1/projects/{pid}")
    assert r.status_code == 200
    # but not write
    r = bob_client.patch(
        f"/api/v1/projects/{pid}", json={"description": "blocked"}
    )
    assert r.status_code == 404

    # Remove bob entirely
    r = alice_client.delete(f"/api/v1/projects/{pid}/members/u_bob")
    assert r.status_code == 200
    assert all(m["user_id"] != "u_bob" for m in r.json())

    # Bob no longer sees the project
    rows = bob_client.get("/api/v1/projects").json()
    assert rows == []


def test_cannot_remove_owner(alice_client):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Owner-locked"}
    ).json()["project_id"]
    r = alice_client.delete(f"/api/v1/projects/{pid}/members/u_alice")
    assert r.status_code == 409


def test_add_member_unknown_email_404(alice_client):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Open"}
    ).json()["project_id"]
    r = alice_client.post(
        f"/api/v1/projects/{pid}/members",
        json={"email": "ghost@example.com", "role": "rw"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Soft delete — disk + DB in lockstep
# ---------------------------------------------------------------------------


def test_delete_moves_workdir_to_trash(alice_client, projects_root, store):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Doomed"}
    ).json()["project_id"]

    workdir = projects_root / pid
    assert workdir.exists()

    r = alice_client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["trashed"] is True

    # Original path is empty
    assert not workdir.exists()
    # Workdir landed under projects/__trash__/<ts>_<id>/
    trash_root = projects_root / "__trash__"
    assert trash_root.exists()
    matches = list(trash_root.glob(f"*_{pid}"))
    assert len(matches) == 1
    # README + subdirs preserved inside trash so a hand-restore can put them back
    assert (matches[0] / "inputs").is_dir()

    # DB row carries the soft-delete metadata
    with store.transaction() as sess:
        proj = sess.get(Project, pid)
        assert proj is not None
        assert proj.trashed_metadata is not None
        assert proj.trashed_metadata["original_workdir_path"].endswith(pid)

    # List excludes by default; include_trashed surfaces it
    assert alice_client.get("/api/v1/projects").json() == []
    rows = alice_client.get(
        "/api/v1/projects", params={"include_trashed": True}
    ).json()
    assert {p["project_id"] for p in rows} == {pid}


def test_delete_blocked_for_non_owner_member(alice_client, bob_client):
    pid = alice_client.post(
        "/api/v1/projects", json={"name": "Co-owned"}
    ).json()["project_id"]
    alice_client.post(
        f"/api/v1/projects/{pid}/members",
        json={"email": "bob@example.com", "role": "rw"},
    )
    # Even with rw, bob is not the owner — delete is owner/admin only.
    r = bob_client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 404
