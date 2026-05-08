"""
Route-level tests for POST /api/v1/projects/{id}/import.

Covers the two-gate authz model (project write × library doc read),
idempotency, quota, missing-blob handling, and audit-log capture.
Same TestClient pattern as test_route_project_files.py — auth ON,
two principals (alice + bob), real Store + sqlite.

Library plumbing is stubbed at the FileStore boundary: a tiny
``FakeFileStore`` exposes ``materialize(file_id, dst)`` which copies
a known fixture blob into the requested workdir path, mirroring the
shape of the real FileStore.materialize without dragging in
ChromaDB / chunking / etc.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal
from api.deps import get_principal, get_state
from api.routes.project_files import router as files_router
from api.routes.projects import router as projects_router
from config import RelationalConfig, SQLiteConfig
from config.agent import AgentConfig
from config.auth_config import AuthConfig
from persistence.models import (
    Artifact,
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


class FakeFileStore:
    """Just enough surface to satisfy ProjectImportService.

    Holds a registry of (file_id → bytes); ``materialize`` writes the
    bytes to the target path. That's the only method the import
    service calls from the real FileStore."""

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
        sqlite=SQLiteConfig(path=str(tmp_path / "rpi.db")),
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
def file_store() -> FakeFileStore:
    return FakeFileStore()


@pytest.fixture
def seeded(store: Store, file_store: FakeFileStore) -> dict[str, str]:
    """Two users, one folder (/sales) shared with alice only, one
    document inside it backed by a real fake-blob file."""
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
        sess.flush()  # users must exist before folders reference them
        # Folder /sales — alice rw, bob no grant
        sess.add(
            Folder(
                folder_id="f_sales",
                path="/sales",
                path_lower="/sales",
                parent_id="__root__",
                name="sales",
                shared_with=[{"user_id": ids["alice"], "role": "rw"}],
            )
        )
        # File row + blob
        file_store.add("blob_q3", b"q3-sales-csv-bytes,1,2,3\n")
        sess.add(
            File(
                file_id="blob_q3",
                content_hash="abcd1234",
                storage_key="blobs/ab/cd/abcd1234.csv",
                original_name="q3_sales.csv",
                display_name="q3_sales.csv",
                size_bytes=len(b"q3-sales-csv-bytes,1,2,3\n"),
                mime_type="text/csv",
            )
        )
        sess.flush()  # folder + file rows must land before documents reference them
        # Document — alice can see it (it's in /sales which alice has)
        sess.add(
            Document(
                doc_id="d_q3_sales",
                file_id="blob_q3",
                folder_id="f_sales",
                path="/sales/q3_sales.csv",
                filename="q3_sales.csv",
                format="csv",
                active_parse_version=1,
            )
        )
        # A second document in alice's folder, with NO file_id (e.g.
        # placeholder for a future URL-only library doc) — the
        # 422 "no blob" path uses this.
        sess.add(
            Document(
                doc_id="d_orphan",
                file_id=None,
                folder_id="f_sales",
                path="/sales/orphan.txt",
                filename="orphan.txt",
                format="txt",
                active_parse_version=1,
            )
        )
        sess.commit()
    return ids


def _make_client(
    store: Store,
    file_store: FakeFileStore,
    projects_root: Path,
    principal_user_id: str,
    *,
    max_workdir_bytes: int = 10 * 1024 * 1024,
    max_upload_bytes: int = 1 * 1024 * 1024,
):
    from api.auth.authz import AuthorizationService

    fake_state = SimpleNamespace(
        cfg=SimpleNamespace(
            auth=AuthConfig(enabled=True),
            agent=AgentConfig(
                projects_root=str(projects_root),
                max_project_workdir_bytes=max_workdir_bytes,
                max_workdir_upload_bytes=max_upload_bytes,
            ),
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
    app.dependency_overrides[get_state] = lambda: fake_state
    app.dependency_overrides[get_principal] = lambda: fake_principal
    return TestClient(app)


@pytest.fixture
def alice_client(store, file_store, seeded, projects_root):
    return _make_client(store, file_store, projects_root, seeded["alice"])


@pytest.fixture
def bob_client(store, file_store, seeded, projects_root):
    return _make_client(store, file_store, projects_root, seeded["bob"])


@pytest.fixture
def alice_project(alice_client) -> str:
    r = alice_client.post(
        "/api/v1/projects",
        json={"name": "Sales analysis", "description": ""},
    )
    return r.json()["project_id"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_import_landed_in_workdir_and_artifact(
    alice_client, alice_project, projects_root, store
):
    r = alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["target_path"] == "inputs/q3_sales.csv"
    assert body["source_doc_id"] == "d_q3_sales"
    assert body["reused"] is False
    assert body["mime"] == "text/csv"

    # File on disk inside the workdir
    workdir = projects_root / alice_project
    target = workdir / "inputs" / "q3_sales.csv"
    assert target.exists()
    assert target.read_bytes() == b"q3-sales-csv-bytes,1,2,3\n"

    # Listed by the file API
    rows = alice_client.get(
        f"/api/v1/projects/{alice_project}/files",
        params={"path": "inputs"},
    ).json()
    assert any(e["name"] == "q3_sales.csv" for e in rows)

    # Artifact row created with proper lineage
    with store.transaction() as sess:
        art = sess.get(Artifact, body["artifact_id"])
        assert art is not None
        assert art.project_id == alice_project
        assert art.run_id is None  # user-driven, not from a run
        assert art.path == "inputs/q3_sales.csv"
        sources = (art.lineage_json or {}).get("sources") or []
        assert len(sources) == 1
        assert sources[0]["type"] == "doc"
        assert sources[0]["doc_id"] == "d_q3_sales"
        assert sources[0]["library_path"] == "/sales/q3_sales.csv"


def test_import_idempotent_returns_same_artifact(
    alice_client, alice_project
):
    first = alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales"},
    )
    assert first.status_code == 201
    aid = first.json()["artifact_id"]

    second = alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales"},
    )
    assert second.status_code == 201
    assert second.json()["artifact_id"] == aid
    assert second.json()["reused"] is True


def test_import_collision_with_manual_upload_picks_suffix(
    alice_client, alice_project, projects_root
):
    # Manually upload a file at the target path FIRST
    from io import BytesIO
    alice_client.post(
        f"/api/v1/projects/{alice_project}/files",
        files={"file": ("q3_sales.csv", BytesIO(b"manual upload"), "text/csv")},
        data={"path": "inputs/q3_sales.csv"},
    )
    # Now import the Library doc with the same filename
    r = alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["target_path"] == "inputs/q3_sales (1).csv"
    workdir = projects_root / alice_project
    assert (workdir / "inputs" / "q3_sales.csv").read_bytes() == b"manual upload"
    assert (workdir / "inputs" / "q3_sales (1).csv").read_bytes() == b"q3-sales-csv-bytes,1,2,3\n"


# ---------------------------------------------------------------------------
# Authz boundaries
# ---------------------------------------------------------------------------


def test_non_member_cannot_import(bob_client, alice_project):
    r = bob_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales"},
    )
    # Bob can't see the project at all (404 from project gate)
    assert r.status_code == 404


def test_viewer_cannot_import(alice_client, bob_client, alice_project):
    # Alice invites bob as read-only viewer
    alice_client.post(
        f"/api/v1/projects/{alice_project}/members",
        json={"email": "bob@example.com"},
    )
    # Even though bob has read on the project, importing is a write
    r = bob_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales"},
    )
    assert r.status_code == 404, "viewer write must 404"


def test_user_without_library_access_cannot_import(
    bob_client, store, file_store, projects_root
):
    """Bob owns his own project; he tries to import a Library doc
    in a folder he has NO grant on. The doc-access check refuses."""
    # Bob creates his own project
    r = bob_client.post("/api/v1/projects", json={"name": "Bob's"})
    pid = r.json()["project_id"]
    # Tries to import alice's doc → 404 (same code as missing doc)
    r = bob_client.post(
        f"/api/v1/projects/{pid}/import",
        json={"doc_id": "d_q3_sales"},
    )
    assert r.status_code == 404


def test_unknown_doc_404(alice_client, alice_project):
    r = alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "doc_does_not_exist"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_doc_without_blob_returns_422(alice_client, alice_project):
    r = alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_orphan"},
    )
    assert r.status_code == 422


def test_quota_exceeded_returns_413(
    store, file_store, seeded, projects_root
):
    # Tight workdir cap so the import busts it
    client = _make_client(
        store,
        file_store,
        projects_root,
        seeded["alice"],
        max_workdir_bytes=512,  # smaller than the README scaffold
    )
    pid = client.post("/api/v1/projects", json={"name": "Tight"}).json()[
        "project_id"
    ]
    r = client.post(
        f"/api/v1/projects/{pid}/import",
        json={"doc_id": "d_q3_sales"},
    )
    assert r.status_code == 413


def test_target_subdir_honored(alice_client, alice_project, projects_root):
    r = alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales", "target_subdir": "scratch"},
    )
    assert r.status_code == 201
    assert r.json()["target_path"] == "scratch/q3_sales.csv"
    assert (projects_root / alice_project / "scratch" / "q3_sales.csv").exists()


def test_target_subdir_reserved_rejected(alice_client, alice_project):
    r = alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales", "target_subdir": ".trash"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_captures_import(alice_client, alice_project, store):
    alice_client.post(
        f"/api/v1/projects/{alice_project}/import",
        json={"doc_id": "d_q3_sales"},
    )
    with store.transaction() as sess:
        actions = [
            r.action
            for r in sess.execute(
                __import__("sqlalchemy").select(AuditLogRow).where(
                    AuditLogRow.target_id == alice_project
                )
            ).scalars()
        ]
    assert "project.import" in actions
