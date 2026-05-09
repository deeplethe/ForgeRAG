"""
Tests for ``/api/v1/workdir/*`` — folder-as-cwd file API.

The user's private workdir is the read-write surface the chat's
``cwd_path`` navigates within. These tests exercise the basic
shape (list / mkdir / upload / download) + the path-safety
contract (no traversal, no absolute paths, no escape via symlinks
/ ``..``).

Auth integration — same principal-bridge pattern as the LLM
proxy + MCP server tests: minimal FastAPI app, ``get_principal``
overridden to a fixed test user, no real auth chain involved.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import get_principal, get_state
from api.routes import workdir_files as workdir_files_routes


def _principal():
    return SimpleNamespace(
        user_id="u_alice",
        username="alice",
        role="user",
        via="cookie",
    )


@pytest.fixture
def state(tmp_path):
    """Minimal AppState with cfg.agent.user_workdirs_root pointing
    at a per-test temp dir. The route auto-creates per-user subdirs
    on first access."""
    return SimpleNamespace(
        cfg=SimpleNamespace(
            agent=SimpleNamespace(
                user_workdirs_root=str(tmp_path / "user-workdirs"),
            )
        ),
    )


@pytest.fixture
def app(state):
    a = FastAPI()
    a.include_router(workdir_files_routes.router)
    a.dependency_overrides[get_principal] = _principal
    a.dependency_overrides[get_state] = lambda: state
    return a


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Workdir info / auto-create
# ---------------------------------------------------------------------------


def test_info_endpoint_auto_creates_user_workdir(client, state, tmp_path):
    """First call to ``/info`` should ensure the user's private
    workdir exists on disk — Workspace UI hits this on mount,
    don't make it a chicken-and-egg with a follow-up upload."""
    r = client.get("/api/v1/workdir/info")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "u_alice"
    assert (tmp_path / "user-workdirs" / "u_alice").exists()


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_root_empty_when_workdir_fresh(client):
    r = client.get("/api/v1/workdir/files?path=")
    assert r.status_code == 200
    assert r.json() == []


def test_list_returns_files_and_folders(client, tmp_path):
    root = tmp_path / "user-workdirs" / "u_alice"
    root.mkdir(parents=True)
    (root / "sales").mkdir()
    (root / "reports").mkdir()
    (root / "scratch.txt").write_text("hi")

    r = client.get("/api/v1/workdir/files?path=")
    assert r.status_code == 200
    entries = r.json()
    names = [e["name"] for e in entries]
    # Folders sorted before files; both alphabetical
    assert names == ["reports", "sales", "scratch.txt"]
    # Folders flagged correctly
    assert next(e for e in entries if e["name"] == "sales")["is_dir"] is True
    assert next(e for e in entries if e["name"] == "scratch.txt")["is_dir"] is False
    # Paths are workdir-relative, leading slash, posix separator
    assert next(e for e in entries if e["name"] == "sales")["path"] == "/sales"


def test_list_subfolder(client, tmp_path):
    root = tmp_path / "user-workdirs" / "u_alice"
    root.mkdir(parents=True)
    (root / "sales" / "2025").mkdir(parents=True)
    (root / "sales" / "Q3-report.pdf").write_text("x")

    r = client.get("/api/v1/workdir/files?path=/sales")
    assert r.status_code == 200
    names = [e["name"] for e in r.json()]
    assert names == ["2025", "Q3-report.pdf"]


def test_list_filters_dot_prefixed_entries(client, tmp_path):
    """Hidden files (``.agent-state/``, ``.trash``, etc.) are
    runtime state — the user shouldn't see them in the
    Workspace tree."""
    root = tmp_path / "user-workdirs" / "u_alice"
    root.mkdir(parents=True)
    (root / "visible.txt").write_text("x")
    (root / ".agent-state").mkdir()
    (root / ".trash").mkdir()

    r = client.get("/api/v1/workdir/files?path=")
    names = [e["name"] for e in r.json()]
    assert names == ["visible.txt"]


def test_list_404_on_missing_folder(client):
    r = client.get("/api/v1/workdir/files?path=/no/such/folder")
    assert r.status_code == 404


def test_list_400_when_path_is_a_file(client, tmp_path):
    root = tmp_path / "user-workdirs" / "u_alice"
    root.mkdir(parents=True)
    (root / "x.txt").write_text("hello")
    r = client.get("/api/v1/workdir/files?path=/x.txt")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Path-safety contract — the security-relevant invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_path", [
    "/sales/../../secret",   # parent traversal
    "/../etc/passwd",        # explicit ../
    "C:\\Windows\\foo",      # Windows drive letter
    "/sales/..",             # ends in ..
])
def test_path_traversal_refused(client, bad_path):
    r = client.get(f"/api/v1/workdir/files?path={bad_path}")
    assert r.status_code == 400


def test_user_cannot_see_other_users_workdir(client, tmp_path, state, app):
    """Bind-mount isolation is host-level (different mount per
    user); but defence-in-depth: the route's path resolution is
    also rooted at <root>/<user_id>/ so even if file-level acls
    accidentally allowed read, the route refuses."""
    # Set up u_bob's workdir with a file
    bob_root = tmp_path / "user-workdirs" / "u_bob"
    bob_root.mkdir(parents=True)
    (bob_root / "secret.txt").write_text("bob's data")

    # alice can't reach it via any path — the route's auto-create
    # only ever creates her own root; she can't traverse out.
    r = client.get("/api/v1/workdir/files?path=/../u_bob")
    assert r.status_code == 400
    # And listing root only shows alice's stuff (which is empty)
    r = client.get("/api/v1/workdir/files?path=")
    assert r.json() == []


# ---------------------------------------------------------------------------
# Mkdir
# ---------------------------------------------------------------------------


def test_mkdir_creates_folder(client, tmp_path):
    r = client.post(
        "/api/v1/workdir/folders",
        json={"path": "/sales/2025"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["path"] == "/sales/2025"
    assert body["is_dir"] is True
    assert (tmp_path / "user-workdirs" / "u_alice" / "sales" / "2025").is_dir()


def test_mkdir_idempotent(client, tmp_path):
    """The Workspace UI's "create or open" flow benefits from
    mkdir being idempotent — calling twice should succeed both
    times, returning the same entry."""
    r1 = client.post("/api/v1/workdir/folders", json={"path": "/foo"})
    r2 = client.post("/api/v1/workdir/folders", json={"path": "/foo"})
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["path"] == r2.json()["path"]


def test_mkdir_409_when_path_is_a_file(client, tmp_path):
    root = tmp_path / "user-workdirs" / "u_alice"
    root.mkdir(parents=True)
    (root / "thing").write_text("x")
    r = client.post("/api/v1/workdir/folders", json={"path": "/thing"})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Upload + download round-trip
# ---------------------------------------------------------------------------


def test_upload_and_download_roundtrip(client, tmp_path):
    """File uploaded → readable via /files listing → downloadable
    via /download with content intact."""
    payload = b"hello opencraig"
    r = client.post(
        "/api/v1/workdir/upload",
        data={"path": "/sales/2025"},
        files={"file": ("Q3-report.txt", io.BytesIO(payload), "text/plain")},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "Q3-report.txt"
    assert r.json()["path"] == "/sales/2025/Q3-report.txt"
    assert r.json()["size_bytes"] == len(payload)

    # Show up in listing
    r = client.get("/api/v1/workdir/files?path=/sales/2025")
    names = [e["name"] for e in r.json()]
    assert "Q3-report.txt" in names

    # Round-trip via download
    r = client.get("/api/v1/workdir/download?path=/sales/2025/Q3-report.txt")
    assert r.status_code == 200
    assert r.content == payload


def test_upload_refuses_filename_with_separator(client, tmp_path):
    r = client.post(
        "/api/v1/workdir/upload",
        data={"path": "/"},
        files={"file": ("../escape.txt", io.BytesIO(b"x"), "text/plain")},
    )
    assert r.status_code == 400


def test_download_404_for_missing_file(client):
    r = client.get("/api/v1/workdir/download?path=/no/such/file.txt")
    assert r.status_code == 404


def test_download_404_for_directory(client, tmp_path):
    root = tmp_path / "user-workdirs" / "u_alice"
    root.mkdir(parents=True)
    (root / "afolder").mkdir()
    r = client.get("/api/v1/workdir/download?path=/afolder")
    assert r.status_code == 404
