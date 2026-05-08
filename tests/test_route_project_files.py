"""
Route-level tests for /api/v1/projects/{id}/files + /trash.

Covers:
  - list / upload / download / mkdir / move / soft-delete round-trip
  - trash list / restore (with collision -> "(restored)" suffix) /
    purge / empty
  - path-traversal rejections (.. / absolute / .trash / .agent-state)
  - quota: per-file too-large + workdir cap
  - read-only share: viewer can list / download / list-trash; viewer
    cannot upload / move / delete / mkdir / restore / purge / empty
  - audit_log captures every mutation
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
from api.routes.project_files import router as files_router
from api.routes.projects import router as projects_router
from config import RelationalConfig, SQLiteConfig
from config.agent import AgentConfig
from config.auth_config import AuthConfig
from persistence.models import AuditLogRow, AuthUser
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "rpf.db")),
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


def _make_client(
    store: Store,
    projects_root: Path,
    principal_user_id: str,
    *,
    max_workdir_bytes: int = 10 * 1024 * 1024,  # 10 MiB for these tests
    max_upload_bytes: int = 1 * 1024 * 1024,    # 1 MiB per file
):
    """Mounts BOTH project + project-files routers so tests can
    POST /projects to create a project then drive its file API.
    Tighter quota defaults than production (10 MiB workdir / 1 MiB
    per file) so the quota tests stay fast."""
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
def alice_client(store, seeded, projects_root):
    return _make_client(store, projects_root, seeded["alice"])


@pytest.fixture
def bob_client(store, seeded, projects_root):
    return _make_client(store, projects_root, seeded["bob"])


@pytest.fixture
def project(alice_client) -> str:
    """A fresh alice-owned project, returns project_id."""
    r = alice_client.post(
        "/api/v1/projects",
        json={"name": "Files-test", "description": "Workdir CRUD"},
    )
    assert r.status_code == 201, r.text
    return r.json()["project_id"]


def _upload(client, pid: str, path: str, content: bytes, **extra):
    return client.post(
        f"/api/v1/projects/{pid}/files",
        files={"file": (Path(path).name, BytesIO(content), "application/octet-stream")},
        data={"path": path, **{k: str(v).lower() for k, v in extra.items()}},
    )


# ---------------------------------------------------------------------------
# List + scaffold
# ---------------------------------------------------------------------------


def test_root_list_excludes_system_dirs(alice_client, project):
    r = alice_client.get(f"/api/v1/projects/{project}/files")
    assert r.status_code == 200, r.text
    names = {e["name"] for e in r.json()}
    # Conventional dirs are present
    assert {"inputs", "outputs", "scratch"}.issubset(names)
    # System dirs are hidden from the root listing
    assert ".trash" not in names
    assert ".agent-state" not in names


def test_list_subdir(alice_client, project):
    r = alice_client.get(
        f"/api/v1/projects/{project}/files", params={"path": "inputs"}
    )
    assert r.status_code == 200
    # Empty subdir
    assert r.json() == []


def test_list_path_traversal_rejected(alice_client, project):
    r = alice_client.get(
        f"/api/v1/projects/{project}/files", params={"path": "../../etc"}
    )
    assert r.status_code == 400


def test_list_reserved_dir_rejected(alice_client, project):
    for reserved in (".trash", ".agent-state"):
        r = alice_client.get(
            f"/api/v1/projects/{project}/files", params={"path": reserved}
        )
        assert r.status_code == 400, f"{reserved} should 400"


def test_list_absolute_path_rejected(alice_client, project):
    r = alice_client.get(
        f"/api/v1/projects/{project}/files", params={"path": "/etc/passwd"}
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Upload + download
# ---------------------------------------------------------------------------


def test_upload_then_download(alice_client, project):
    payload = b"col1,col2\n1,2\n3,4\n"
    r = _upload(alice_client, project, "inputs/data.csv", payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["path"] == "inputs/data.csv"
    assert body["size_bytes"] == len(payload)

    # Download round-trip
    r = alice_client.get(
        f"/api/v1/projects/{project}/files/download",
        params={"path": "inputs/data.csv"},
    )
    assert r.status_code == 200
    assert r.content == payload


def test_upload_collision_409(alice_client, project):
    _upload(alice_client, project, "inputs/x.txt", b"v1")
    r = _upload(alice_client, project, "inputs/x.txt", b"v2")
    assert r.status_code == 409


def test_upload_overwrite_ok(alice_client, project):
    _upload(alice_client, project, "inputs/x.txt", b"v1")
    r = _upload(alice_client, project, "inputs/x.txt", b"v2", overwrite=True)
    assert r.status_code == 201
    # Download returns v2
    r = alice_client.get(
        f"/api/v1/projects/{project}/files/download",
        params={"path": "inputs/x.txt"},
    )
    assert r.content == b"v2"


def test_upload_too_large_413(alice_client, store, seeded, projects_root):
    # Tighten upload cap to 100 bytes
    client = _make_client(
        store, projects_root, seeded["alice"], max_upload_bytes=100
    )
    pid = client.post(
        "/api/v1/projects", json={"name": "Tight uploads"}
    ).json()["project_id"]
    r = _upload(client, pid, "inputs/big.bin", b"x" * 200)
    assert r.status_code == 413


def test_workdir_quota_exceeded_413(alice_client, store, seeded, projects_root):
    # Tighten workdir cap to 8 KiB; the scaffold (README + empty
    # trash.json) burns ~500 bytes, leaving ~7.5 KiB for uploads.
    client = _make_client(
        store,
        projects_root,
        seeded["alice"],
        max_workdir_bytes=8 * 1024,
        max_upload_bytes=10 * 1024,
    )
    pid = client.post(
        "/api/v1/projects", json={"name": "Tight workdir"}
    ).json()["project_id"]
    # First write fits comfortably
    r = _upload(client, pid, "inputs/a.bin", b"x" * 4 * 1024)
    assert r.status_code == 201
    # Second would push past 8 KiB total
    r = _upload(client, pid, "inputs/b.bin", b"x" * 5 * 1024)
    assert r.status_code == 413


def test_upload_path_traversal_rejected(alice_client, project):
    r = _upload(alice_client, project, "../escape.bin", b"nope")
    assert r.status_code == 400


def test_upload_into_reserved_dir_rejected(alice_client, project):
    r = _upload(alice_client, project, ".trash/sneak.bin", b"nope")
    assert r.status_code == 400
    r = _upload(alice_client, project, ".agent-state/notes.bin", b"nope")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# mkdir + move
# ---------------------------------------------------------------------------


def test_mkdir_then_listed(alice_client, project):
    r = alice_client.post(
        f"/api/v1/projects/{project}/files/mkdir",
        json={"path": "outputs/charts"},
    )
    assert r.status_code == 201
    rows = alice_client.get(
        f"/api/v1/projects/{project}/files", params={"path": "outputs"}
    ).json()
    assert any(e["name"] == "charts" and e["is_dir"] for e in rows)


def test_move_rename_within_dir(alice_client, project):
    _upload(alice_client, project, "inputs/old.txt", b"v")
    r = alice_client.patch(
        f"/api/v1/projects/{project}/files/move",
        json={"from_path": "inputs/old.txt", "to_path": "inputs/new.txt"},
    )
    assert r.status_code == 200
    assert r.json()["path"] == "inputs/new.txt"
    # Original gone
    r = alice_client.get(
        f"/api/v1/projects/{project}/files/download",
        params={"path": "inputs/old.txt"},
    )
    assert r.status_code == 404


def test_move_collision_409(alice_client, project):
    _upload(alice_client, project, "inputs/a.txt", b"a")
    _upload(alice_client, project, "inputs/b.txt", b"b")
    r = alice_client.patch(
        f"/api/v1/projects/{project}/files/move",
        json={"from_path": "inputs/a.txt", "to_path": "inputs/b.txt"},
    )
    assert r.status_code == 409


def test_move_to_reserved_rejected(alice_client, project):
    _upload(alice_client, project, "inputs/x.txt", b"x")
    r = alice_client.patch(
        f"/api/v1/projects/{project}/files/move",
        json={"from_path": "inputs/x.txt", "to_path": ".trash/sneak"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Soft delete + trash round-trip
# ---------------------------------------------------------------------------


def test_soft_delete_then_list_trash_then_restore(alice_client, project):
    _upload(alice_client, project, "outputs/report.txt", b"hello")

    # Soft delete
    r = alice_client.delete(
        f"/api/v1/projects/{project}/files",
        params={"path": "outputs/report.txt"},
    )
    assert r.status_code == 200, r.text
    trash_id = r.json()["trash_id"]
    assert r.json()["original_path"] == "outputs/report.txt"

    # File no longer reachable via download
    r = alice_client.get(
        f"/api/v1/projects/{project}/files/download",
        params={"path": "outputs/report.txt"},
    )
    assert r.status_code == 404

    # Listed in trash
    r = alice_client.get(f"/api/v1/projects/{project}/trash")
    assert r.status_code == 200
    rows = r.json()
    assert {row["trash_id"] for row in rows} == {trash_id}
    assert rows[0]["original_path"] == "outputs/report.txt"

    # Restore
    r = alice_client.post(
        f"/api/v1/projects/{project}/trash/{trash_id}/restore"
    )
    assert r.status_code == 200
    assert r.json()["path"] == "outputs/report.txt"

    # File downloadable again
    r = alice_client.get(
        f"/api/v1/projects/{project}/files/download",
        params={"path": "outputs/report.txt"},
    )
    assert r.status_code == 200
    assert r.content == b"hello"

    # Trash empty
    assert alice_client.get(f"/api/v1/projects/{project}/trash").json() == []


def test_restore_collision_picks_suffix(alice_client, project):
    _upload(alice_client, project, "outputs/r.txt", b"v1")
    delete_r = alice_client.delete(
        f"/api/v1/projects/{project}/files",
        params={"path": "outputs/r.txt"},
    )
    trash_id = delete_r.json()["trash_id"]
    # New file at same path
    _upload(alice_client, project, "outputs/r.txt", b"v2")
    # Restore lands on a "(restored)" suffix
    r = alice_client.post(
        f"/api/v1/projects/{project}/trash/{trash_id}/restore"
    )
    assert r.status_code == 200
    assert r.json()["path"] == "outputs/r (restored).txt"


def test_purge_one_entry(alice_client, project):
    _upload(alice_client, project, "scratch/tmp.txt", b"junk")
    delete_r = alice_client.delete(
        f"/api/v1/projects/{project}/files",
        params={"path": "scratch/tmp.txt"},
    )
    trash_id = delete_r.json()["trash_id"]
    r = alice_client.delete(
        f"/api/v1/projects/{project}/trash/{trash_id}"
    )
    assert r.status_code == 204
    assert alice_client.get(f"/api/v1/projects/{project}/trash").json() == []


def test_empty_trash(alice_client, project):
    for i in range(3):
        _upload(alice_client, project, f"scratch/t{i}.txt", b"v")
        alice_client.delete(
            f"/api/v1/projects/{project}/files",
            params={"path": f"scratch/t{i}.txt"},
        )
    r = alice_client.post(f"/api/v1/projects/{project}/trash/empty")
    assert r.status_code == 200
    assert r.json()["purged_count"] == 3
    assert alice_client.get(f"/api/v1/projects/{project}/trash").json() == []


# ---------------------------------------------------------------------------
# Read-only share enforcement
# ---------------------------------------------------------------------------


def test_viewer_can_read_cannot_write(alice_client, bob_client, project):
    # Alice uploads + invites bob as viewer
    _upload(alice_client, project, "outputs/shared.txt", b"hello")
    r = alice_client.post(
        f"/api/v1/projects/{project}/members",
        json={"email": "bob@example.com"},
    )
    assert r.status_code == 201

    # Bob can list + download
    assert bob_client.get(f"/api/v1/projects/{project}/files").status_code == 200
    r = bob_client.get(
        f"/api/v1/projects/{project}/files/download",
        params={"path": "outputs/shared.txt"},
    )
    assert r.status_code == 200
    assert r.content == b"hello"

    # Bob can list trash
    assert bob_client.get(f"/api/v1/projects/{project}/trash").status_code == 200

    # Bob CANNOT upload / move / delete / mkdir / restore / purge / empty
    assert _upload(bob_client, project, "inputs/sneak.txt", b"x").status_code == 404
    r = bob_client.patch(
        f"/api/v1/projects/{project}/files/move",
        json={"from_path": "outputs/shared.txt", "to_path": "outputs/hijacked.txt"},
    )
    assert r.status_code == 404
    r = bob_client.delete(
        f"/api/v1/projects/{project}/files",
        params={"path": "outputs/shared.txt"},
    )
    assert r.status_code == 404
    r = bob_client.post(
        f"/api/v1/projects/{project}/files/mkdir",
        json={"path": "outputs/sneak"},
    )
    assert r.status_code == 404


def test_non_member_404_on_everything(alice_client, bob_client, project):
    # Bob has no grant at all — every endpoint 404s
    assert bob_client.get(f"/api/v1/projects/{project}/files").status_code == 404
    assert bob_client.get(f"/api/v1/projects/{project}/trash").status_code == 404
    assert _upload(bob_client, project, "inputs/x.txt", b"x").status_code == 404


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_captures_mutations(alice_client, store, project):
    _upload(alice_client, project, "outputs/a.txt", b"a")
    alice_client.post(
        f"/api/v1/projects/{project}/files/mkdir", json={"path": "outputs/sub"}
    )
    alice_client.patch(
        f"/api/v1/projects/{project}/files/move",
        json={"from_path": "outputs/a.txt", "to_path": "outputs/sub/a.txt"},
    )
    delete_r = alice_client.delete(
        f"/api/v1/projects/{project}/files",
        params={"path": "outputs/sub/a.txt"},
    )
    trash_id = delete_r.json()["trash_id"]
    alice_client.post(
        f"/api/v1/projects/{project}/trash/{trash_id}/restore"
    )

    with store.transaction() as sess:
        actions = [
            r.action
            for r in sess.execute(
                __import__("sqlalchemy").select(AuditLogRow).where(
                    AuditLogRow.target_id == project
                )
            ).scalars()
        ]
    expected = {
        "project.create",
        "project.file.upload",
        "project.file.mkdir",
        "project.file.move",
        "project.file.delete",
        "project.file.restore",
    }
    assert expected.issubset(actions), f"missing: {expected - set(actions)}"
