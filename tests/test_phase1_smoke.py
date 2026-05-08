"""
Phase 1 end-to-end smoke — drives the whole user journey through
the API in one test, verifying the Library / Workspace split holds
together as a system rather than as isolated routes.

The journey:
  1. Alice creates a project. Workdir scaffolds on disk.
  2. Alice uploads a CSV into inputs/ via the file API.
  3. Alice imports a Library doc she has read access to → lands as
     an Artifact in inputs/ with proper lineage.
  4. Alice creates a chat bound to the project. Conversation row
     carries project_id.
  5. Alice asks bob to view (read-only share). Bob sees the project
     on his list with role='r', can list files / download / list
     trash, but cannot upload, mkdir, soft-delete, restore, purge,
     or rename.
  6. Bob CAN open a chat against the shared project (read-only
     consultant scenario) — Conversation.project_id gets set.
  7. Alice soft-deletes one of the inputs. It shows up in trash.
     Trash routes still allow alice (owner) but not bob (viewer).
  8. Alice restores the file. Original path is occupied (a manual
     upload sits there) so the restored file lands at
     "<base> (restored).<ext>".
  9. Alice soft-deletes the entire project. Conversation.project_id
     is NULLed back to None (chats survive as plain Q&A);
     bob no longer sees the project.

Audit log is checked at the end for the canonical actions emitted
along the way, so a regression that drops audit-row writes in any
of the touched paths surfaces here.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_principal, get_state
from api.routes.conversations import router as conversations_router
from api.routes.project_files import router as files_router
from api.routes.projects import router as projects_router
from config import RelationalConfig, SQLiteConfig
from config.agent import AgentConfig
from config.auth_config import AuthConfig
from persistence.models import (
    AuditLogRow,
    AuthUser,
    Document,
    File,
    Folder,
)
from persistence.store import Store


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeFileStore:
    """Minimal FileStore for the import path — same shape as
    test_route_project_import.py's FakeFileStore."""

    def __init__(self):
        self._blobs: dict[str, bytes] = {}

    def add(self, file_id: str, content: bytes) -> None:
        self._blobs[file_id] = content

    def materialize(self, file_id: str, local_path: Path) -> Path:
        if file_id not in self._blobs:
            raise KeyError(f"file {file_id} not found")
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(self._blobs[file_id])
        return dst


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "smoke.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def projects_root(tmp_path) -> Path:
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def file_store() -> _FakeFileStore:
    return _FakeFileStore()


@pytest.fixture
def seeded(store: Store, file_store: _FakeFileStore) -> dict[str, str]:
    """Two users, one Library folder + doc that alice has read access
    on (bob has none)."""
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
        sess.flush()
        sess.add(
            Folder(
                folder_id="f_alice_data",
                path="/alice-data",
                path_lower="/alice-data",
                parent_id="__root__",
                name="alice-data",
                shared_with=[{"user_id": ids["alice"], "role": "rw"}],
            )
        )
        file_store.add("blob_brief", b"Q3 brief: revenue up 12%, costs flat\n")
        sess.add(
            File(
                file_id="blob_brief",
                content_hash="brief1234",
                storage_key="blobs/br/brief1234.txt",
                original_name="brief.txt",
                display_name="brief.txt",
                size_bytes=len(b"Q3 brief: revenue up 12%, costs flat\n"),
                mime_type="text/plain",
            )
        )
        sess.flush()
        sess.add(
            Document(
                doc_id="d_brief",
                file_id="blob_brief",
                folder_id="f_alice_data",
                path="/alice-data/brief.txt",
                filename="brief.txt",
                format="txt",
                active_parse_version=1,
            )
        )
        sess.commit()
    return ids


def _make_client(store, file_store, projects_root, principal_user_id):
    from api.auth.authz import AuthorizationService

    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(
            auth=AuthConfig(enabled=True),
            agent=AgentConfig(projects_root=str(projects_root)),
        ),
        store=store,
        authz=AuthorizationService(store),
        file_store=file_store,
    )
    fake_principal = AuthenticatedPrincipal(
        user_id=principal_user_id,
        username=principal_user_id.removeprefix("u_"),
        role="user",
        via="cookie",
    )
    app = FastAPI()
    app.include_router(projects_router)
    app.include_router(files_router)
    app.include_router(conversations_router)
    app.dependency_overrides[get_state] = lambda: fake_state
    app.dependency_overrides[get_principal] = lambda: fake_principal
    return TestClient(app)


@pytest.fixture
def alice_client(store, file_store, seeded, projects_root):
    return _make_client(store, file_store, projects_root, seeded["alice"])


@pytest.fixture
def bob_client(store, file_store, seeded, projects_root):
    return _make_client(store, file_store, projects_root, seeded["bob"])


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------


def test_phase1_end_to_end(alice_client, bob_client, projects_root, store):
    # ── 1. Alice creates a project ──
    r = alice_client.post(
        "/api/v1/projects",
        json={"name": "Q3 review", "description": "Quarterly numbers"},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["project_id"]
    workdir = projects_root / pid
    assert (workdir / "inputs").is_dir()
    assert (workdir / ".trash").is_dir()
    assert (workdir / ".agent-state" / "trash.json").exists()

    # ── 2. Alice uploads a manual file ──
    r = alice_client.post(
        f"/api/v1/projects/{pid}/files",
        files={"file": ("manual.csv", BytesIO(b"manual,upload\n"), "text/csv")},
        data={"path": "inputs/manual.csv"},
    )
    assert r.status_code == 201

    # ── 3. Alice imports a Library doc ──
    r = alice_client.post(
        f"/api/v1/projects/{pid}/import",
        json={"doc_id": "d_brief"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["target_path"] == "inputs/brief.txt"
    assert (workdir / "inputs" / "brief.txt").read_bytes() == (
        b"Q3 brief: revenue up 12%, costs flat\n"
    )

    # ── 4. Alice creates a chat bound to the project ──
    r = alice_client.post(
        "/api/v1/conversations",
        json={"title": "Walking through Q3", "project_id": pid},
    )
    assert r.status_code == 201
    conv_id = r.json()["conversation_id"]
    assert r.json()["project_id"] == pid

    # ── 5. Alice invites bob as read-only viewer ──
    r = alice_client.post(
        f"/api/v1/projects/{pid}/members",
        json={"email": "bob@example.com"},
    )
    assert r.status_code == 201

    # Bob sees the project, role='r'
    rows = bob_client.get("/api/v1/projects").json()
    assert {p["project_id"] for p in rows} == {pid}
    assert rows[0]["role"] == "r"

    # Bob can list, download, list trash
    r = bob_client.get(f"/api/v1/projects/{pid}/files", params={"path": "inputs"})
    assert r.status_code == 200
    names = {e["name"] for e in r.json()}
    assert {"manual.csv", "brief.txt"}.issubset(names)
    r = bob_client.get(
        f"/api/v1/projects/{pid}/files/download",
        params={"path": "inputs/brief.txt"},
    )
    assert r.status_code == 200
    assert b"Q3 brief" in r.content
    assert bob_client.get(f"/api/v1/projects/{pid}/trash").status_code == 200

    # Bob CANNOT mutate
    r = bob_client.post(
        f"/api/v1/projects/{pid}/files",
        files={"file": ("x.txt", BytesIO(b"x"), "text/plain")},
        data={"path": "inputs/x.txt"},
    )
    assert r.status_code == 404
    r = bob_client.delete(
        f"/api/v1/projects/{pid}/files",
        params={"path": "inputs/manual.csv"},
    )
    assert r.status_code == 404
    r = bob_client.post(
        f"/api/v1/projects/{pid}/files/mkdir",
        json={"path": "outputs/sneak"},
    )
    assert r.status_code == 404

    # ── 6. Bob opens a chat against the shared project (consultant view) ──
    r = bob_client.post(
        "/api/v1/conversations",
        json={"title": "Looking around", "project_id": pid},
    )
    assert r.status_code == 201
    bob_conv_id = r.json()["conversation_id"]
    assert r.json()["project_id"] == pid

    # Bob's import ATTEMPT is blocked (write op even via read share)
    r = bob_client.post(
        f"/api/v1/projects/{pid}/import",
        json={"doc_id": "d_brief"},
    )
    assert r.status_code == 404

    # ── 7. Alice soft-deletes manual.csv ──
    r = alice_client.delete(
        f"/api/v1/projects/{pid}/files",
        params={"path": "inputs/manual.csv"},
    )
    assert r.status_code == 200
    trash_id = r.json()["trash_id"]
    assert not (workdir / "inputs" / "manual.csv").exists()
    # Trash list shows it (alice and bob both)
    assert trash_id in {
        e["trash_id"] for e in alice_client.get(f"/api/v1/projects/{pid}/trash").json()
    }
    assert trash_id in {
        e["trash_id"] for e in bob_client.get(f"/api/v1/projects/{pid}/trash").json()
    }
    # Bob cannot restore / purge / empty
    assert (
        bob_client.post(f"/api/v1/projects/{pid}/trash/{trash_id}/restore").status_code
        == 404
    )

    # ── 8. Re-upload occupies original path; restore picks suffix ──
    alice_client.post(
        f"/api/v1/projects/{pid}/files",
        files={"file": ("manual.csv", BytesIO(b"v2"), "text/csv")},
        data={"path": "inputs/manual.csv"},
    )
    r = alice_client.post(f"/api/v1/projects/{pid}/trash/{trash_id}/restore")
    assert r.status_code == 200
    assert r.json()["path"] == "inputs/manual (restored).csv"

    # ── 9. Alice soft-deletes the project; bob loses access ──
    r = alice_client.delete(f"/api/v1/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["trashed"] is True
    # Bob no longer sees it on list (trashed)
    assert bob_client.get("/api/v1/projects").json() == []
    # Alice's chat row's project_id was NULLed back so the chat
    # keeps working as plain Q&A
    r = alice_client.get(f"/api/v1/conversations/{conv_id}")
    assert r.status_code == 200
    assert r.json()["project_id"] is None
    # Bob's chat too
    r = bob_client.get(f"/api/v1/conversations/{bob_conv_id}")
    assert r.status_code == 200
    assert r.json()["project_id"] is None

    # ── 10. Audit log captured the canonical journey ──
    with store.transaction() as sess:
        rows = sess.execute(
            __import__("sqlalchemy").select(AuditLogRow).where(
                AuditLogRow.target_id == pid
            )
        ).scalars()
        actions = [r.action for r in rows]
    expected = {
        "project.create",
        "project.share",
        "project.import",
        "project.file.upload",
        "project.file.delete",
        "project.file.restore",
        "project.trash",
    }
    assert expected.issubset(actions), f"missing audit rows: {expected - set(actions)}"
