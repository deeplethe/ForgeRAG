"""
Mutation-route authz: folder rename / move / delete + document
move / bulk-move / rename.

Pre-audit, these routes only ran ``ScopeService.require_folder``,
which is a Phase-1 always-allow placeholder ("Swap in a real gate
later"). The user-level authz check (``state.authz.can``) was
never wired in. So a non-admin user could:

  * rename / move / soft-delete any folder corpus-wide
  * move / rename any document corpus-wide
  * via ``POST /documents/bulk-move`` enumerate doc existence by
    iterating doc_ids and watching which ones land in "moved" vs
    "errors"

Audit fix wires ``require_doc_access`` / ``require_folder_access``
into each route. These tests pin the new behaviour:

  * cross-user mutation → 404 (same as a missing resource — never
    confirms existence)
  * own-folder mutation → 200
  * admin role bypasses both source and target gates

Setup:

    /research  → alice rw,  doc d_research
    /scratch   → bob   rw,  doc d_scratch

Auth-disabled deployments are covered separately in
``test_resource_authz.py`` — every gate routes through
``require_*_access`` which already has the auth-disabled
short-circuit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal, AuthorizationService
from api.deps import get_state
from api.routes.documents import router as documents_router
from api.routes.folders import router as folders_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import (
    AuthUser,
    Document,
    File,
    Folder,
)
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "rma.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict:
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

        for fid, path, sw in (
            ("f_research", "/research", [{"user_id": ids["alice"], "role": "rw"}]),
            ("f_scratch", "/scratch", [{"user_id": ids["bob"], "role": "rw"}]),
        ):
            sess.add(
                Folder(
                    folder_id=fid,
                    path=path,
                    path_lower=path,
                    parent_id="__root__",
                    name=path.lstrip("/"),
                    shared_with=sw,
                )
            )
        sess.flush()

        for fid in ("file_research", "file_scratch"):
            sess.add(
                File(
                    file_id=fid,
                    content_hash=fid,
                    storage_key=f"{fid}.pdf",
                    original_name=f"{fid}.pdf",
                    display_name=f"{fid}.pdf",
                    size_bytes=1,
                    mime_type="application/pdf",
                    user_id=(
                        ids["alice"] if fid.endswith("research") else ids["bob"]
                    ),
                )
            )
        sess.flush()

        for did, fid, file_id, path in (
            ("d_research", "f_research", "file_research", "/research/r.pdf"),
            ("d_scratch", "f_scratch", "file_scratch", "/scratch/s.pdf"),
        ):
            sess.add(
                Document(
                    doc_id=did,
                    file_id=file_id,
                    folder_id=fid,
                    path=path,
                    filename=path.rsplit("/", 1)[-1],
                    format="pdf",
                    active_parse_version=1,
                )
            )
        sess.commit()
    return {"users": ids}


def _build_app(
    store: Store, principal: AuthenticatedPrincipal, *, auth_enabled: bool = True
) -> FastAPI:
    fake_state = SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        authz=AuthorizationService(store),
        # vector / graph_store untouched by these routes (post-commit
        # cross-store sync no-ops when attrs are missing or None).
        vector=None,
        graph_store=None,
        vector_store=None,
    )
    app = FastAPI()
    app.include_router(folders_router)
    app.include_router(documents_router)
    app.dependency_overrides[get_state] = lambda: fake_state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    return app


def _alice(seeded):
    return AuthenticatedPrincipal(
        user_id=seeded["users"]["alice"], username="alice", role="user", via="session"
    )


def _bob(seeded):
    return AuthenticatedPrincipal(
        user_id=seeded["users"]["bob"], username="bob", role="user", via="session"
    )


def _admin(seeded):
    return AuthenticatedPrincipal(
        user_id=seeded["users"]["admin"], username="admin", role="admin", via="session"
    )


# ---------------------------------------------------------------------------
# /api/v1/folders — rename / move / delete
# ---------------------------------------------------------------------------


class TestFolderRename:
    def test_owner_ok(self, store, seeded):
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/folders/rename",
                json={"path": "/research", "new_name": "research2"},
            )
        assert r.status_code == 200

    def test_cross_user_404(self, store, seeded):
        """alice tries to rename bob's /scratch — must fail with 404,
        not 403, not 200."""
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/folders/rename",
                json={"path": "/scratch", "new_name": "hijacked"},
            )
        assert r.status_code == 404

    def test_admin_bypass(self, store, seeded):
        app = _build_app(store, _admin(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/folders/rename",
                json={"path": "/scratch", "new_name": "scratch2"},
            )
        assert r.status_code == 200


class TestFolderMove:
    def test_cross_user_source_404(self, store, seeded):
        """alice moves bob's /scratch under /research. Source check
        fails first → 404."""
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/folders/move",
                json={"path": "/scratch", "to_parent_path": "/research"},
            )
        assert r.status_code == 404

    def test_cross_user_target_404(self, store, seeded):
        """bob moves his own /scratch under alice's /research. Source
        passes, target fails → 404."""
        app = _build_app(store, _bob(seeded))
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/folders/move",
                json={"path": "/scratch", "to_parent_path": "/research"},
            )
        assert r.status_code == 404


class TestFolderDelete:
    def test_owner_ok(self, store, seeded):
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.delete("/api/v1/folders", params={"path": "/research"})
        assert r.status_code == 200

    def test_cross_user_404(self, store, seeded):
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.delete("/api/v1/folders", params={"path": "/scratch"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/v1/documents — move / bulk-move / rename
# ---------------------------------------------------------------------------


class TestDocumentMove:
    def test_cross_user_source_404(self, store, seeded):
        """alice moves bob's d_scratch into her own /research."""
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/documents/d_scratch/path",
                json={"to_path": "/research"},
            )
        assert r.status_code == 404

    def test_cross_user_target_404(self, store, seeded):
        """bob moves his d_scratch into alice's /research."""
        app = _build_app(store, _bob(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/documents/d_scratch/path",
                json={"to_path": "/research"},
            )
        assert r.status_code == 404

    def test_admin_bypass(self, store, seeded):
        app = _build_app(store, _admin(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/documents/d_scratch/path",
                json={"to_path": "/research"},
            )
        assert r.status_code == 200


class TestDocumentBulkMove:
    def test_inaccessible_source_reports_not_found(self, store, seeded):
        """alice bulk-moves [d_research, d_scratch] into /research.
        d_research succeeds; d_scratch reports ``error: not found``
        (NOT a 403, NOT silently skipped) — a non-admin user must
        not be able to enumerate doc existence by inspecting which
        ids return ``error: not found`` vs which silently absent."""
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/documents/bulk-move",
                json={
                    "doc_ids": ["d_research", "d_scratch"],
                    "to_path": "/research",
                },
            )
        assert r.status_code == 200
        body = r.json()
        moved_ids = {m["doc_id"] for m in body["moved"]}
        error_ids = {e["doc_id"] for e in body["errors"]}
        assert moved_ids == {"d_research"}
        assert error_ids == {"d_scratch"}

    def test_inaccessible_target_aborts_whole_batch(self, store, seeded):
        """alice tries to bulk-move HER OWN docs into /scratch (bob's).
        Target gate fails up-front → 404 (no partial moves)."""
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/documents/bulk-move",
                json={"doc_ids": ["d_research"], "to_path": "/scratch"},
            )
        assert r.status_code == 404
        # Verify no mutation happened.
        with store.transaction() as sess:
            doc = sess.get(Document, "d_research")
            assert doc.folder_id == "f_research"


class TestDocumentRename:
    def test_owner_ok(self, store, seeded):
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/documents/d_research/filename",
                json={"new_filename": "renamed.pdf"},
            )
        assert r.status_code == 200

    def test_cross_user_404(self, store, seeded):
        app = _build_app(store, _alice(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/documents/d_scratch/filename",
                json={"new_filename": "hijacked.pdf"},
            )
        assert r.status_code == 404
        # Verify no mutation happened.
        with store.transaction() as sess:
            doc = sess.get(Document, "d_scratch")
            assert doc.filename == "s.pdf"

    def test_admin_bypass(self, store, seeded):
        app = _build_app(store, _admin(seeded))
        with TestClient(app) as c:
            r = c.patch(
                "/api/v1/documents/d_scratch/filename",
                json={"new_filename": "admin-renamed.pdf"},
            )
        assert r.status_code == 200
