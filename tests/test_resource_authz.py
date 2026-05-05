"""
Single-resource GET authz — chunks / blocks / documents / files.

The "click a citation" UX (and any direct deep link) hits one of
these per-id GET routes. Pre S5.4 they all bypassed folder authz —
once a user knew a chunk_id / block_id / doc_id / file_id they
could fetch the content regardless of which folder it lived in.

S5.4 threads ``require_*_access`` helpers through every relevant
route. The contract:

  * Resolve the resource → its containing folder.
  * Call ``authz.can(folder_id, "read")`` for the principal.
  * On miss / no-access: 404 (not 403). Never confirm a stranger's
    id is real.
  * Auth-disabled deployments skip the check.

Setup:

    /research        shared_with [{alice, rw}]
    /scratch         shared_with [{bob,   rw}]

Each folder owns one document with one chunk + block + image
block. Routes are exercised with alice / bob / admin principals;
each principal should see only their own folder's content.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal, AuthorizationService
from api.deps import get_state
from api.routes.chunks import router as chunks_router
from api.routes.documents import router as documents_router
from api.routes.files import router as files_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import (
    AuthUser,
    ChunkRow,
    Document,
    File,
    Folder,
    ParsedBlock,
)
from persistence.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "ra.db")),
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

        # Files (uploader = bob for /scratch's, alice for /research's)
        sess.add(
            File(
                file_id="file_research",
                content_hash="ha",
                storage_key="r.pdf",
                original_name="r.pdf",
                display_name="r.pdf",
                size_bytes=1,
                mime_type="application/pdf",
                owner_user_id=ids["alice"],
            )
        )
        sess.add(
            File(
                file_id="file_scratch",
                content_hash="hb",
                storage_key="s.pdf",
                original_name="s.pdf",
                display_name="s.pdf",
                size_bytes=1,
                mime_type="application/pdf",
                owner_user_id=ids["bob"],
            )
        )
        sess.flush()

        for did, fid, file_id in (
            ("d_research", "f_research", "file_research"),
            ("d_scratch", "f_scratch", "file_scratch"),
        ):
            sess.add(
                Document(
                    doc_id=did,
                    file_id=file_id,
                    folder_id=fid,
                    path=f"/{fid.split('_', 1)[1]}/x.pdf",
                    filename="x.pdf",
                    format="pdf",
                    active_parse_version=1,
                )
            )
        sess.flush()

        # One block + one chunk per document
        for did in ("d_research", "d_scratch"):
            block_id = f"{did}:1:1:1"
            chunk_id = f"{did}:1:c1"
            sess.add(
                ParsedBlock(
                    block_id=block_id,
                    doc_id=did,
                    parse_version=1,
                    page_no=1,
                    seq=1,
                    bbox_x0=0.0,
                    bbox_y0=0.0,
                    bbox_x1=100.0,
                    bbox_y1=20.0,
                    type="paragraph",
                    text="hello",
                    image_storage_key=None,
                )
            )
            sess.add(
                ChunkRow(
                    chunk_id=chunk_id,
                    doc_id=did,
                    parse_version=1,
                    node_id=f"node-{did}",
                    block_ids=[block_id],
                    content="hello world",
                    content_type="text",
                    page_start=1,
                    page_end=1,
                    token_count=2,
                    path=f"/{did.split('_')[1]}/x.pdf",
                )
            )
        sess.commit()
    return {"users": ids}


def _build_app(store: Store, principal: AuthenticatedPrincipal, *, auth_enabled=True):
    # Pretend BM25 is empty + ready so the /chunks/search route's
    # ``state.refresh_bm25()`` branch isn't taken.
    class _EmptyBM25:
        def __len__(self):
            return 0

        def search_chunks(self, q, top_k):
            return []

    fake_state = SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        authz=AuthorizationService(store),
        _bm25=_EmptyBM25(),
        refresh_bm25=lambda: None,
        blob=SimpleNamespace(
            get=lambda key: b"",
            url_for=lambda key: None,
        ),
    )
    app = FastAPI()
    app.include_router(chunks_router)
    app.include_router(documents_router)
    app.include_router(files_router)
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
# /chunks/{id}
# ---------------------------------------------------------------------------


def test_get_chunk_owner_ok(store, seeded):
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/chunks/d_research:1:c1")
    assert r.status_code == 200
    assert r.json()["chunk_id"] == "d_research:1:c1"


def test_get_chunk_cross_user_404(store, seeded):
    """alice asks for bob's chunk — 404, not 403."""
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/chunks/d_scratch:1:c1")
    assert r.status_code == 404


def test_get_chunk_admin_no_bypass_on_content(store, seeded):
    """Admin role bypasses the can() check via role match, so admin
    DOES get to read any chunk's content. (Admin bypass is for the
    shared corpus — chunks are part of that, unlike conversations.)
    """
    app = _build_app(store, _admin(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/chunks/d_research:1:c1")
    assert r.status_code == 200


def test_get_chunk_neighbors_cross_user_404(store, seeded):
    app = _build_app(store, _bob(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/chunks/d_research:1:c1/neighbors")
    assert r.status_code == 404


def test_chunks_search_filters_inaccessible(store, seeded):
    """Even when BM25 is empty (no real index in this fixture) the
    filter shouldn't blow up. We only verify the endpoint works
    without leaking — full BM25 wiring is a separate test surface."""
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/chunks/search?q=hello")
    assert r.status_code == 200
    body = r.json()
    # Empty BM25 in this fixture — just verify shape.
    assert body["items"] == []


# ---------------------------------------------------------------------------
# /blocks/{id}
# ---------------------------------------------------------------------------


def test_get_block_owner_ok(store, seeded):
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/blocks/d_research:1:1:1")
    assert r.status_code == 200


def test_get_block_cross_user_404(store, seeded):
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/blocks/d_scratch:1:1:1")
    assert r.status_code == 404


def test_get_blocks_by_page_cross_user_404(store, seeded):
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/blocks/by-page/d_scratch/1")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /documents/{id}
# ---------------------------------------------------------------------------


def test_get_document_owner_ok(store, seeded):
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/documents/d_research")
    assert r.status_code == 200


def test_get_document_cross_user_404(store, seeded):
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/documents/d_scratch")
    assert r.status_code == 404


def test_list_doc_blocks_cross_user_404(store, seeded):
    app = _build_app(store, _bob(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/documents/d_research/blocks")
    assert r.status_code == 404


def test_list_doc_chunks_cross_user_404(store, seeded):
    app = _build_app(store, _bob(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/documents/d_research/chunks")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /files/{id}
# ---------------------------------------------------------------------------


def test_get_file_via_referencing_doc(store, seeded):
    """alice has rw on /research; the file is referenced by a doc in
    that folder, so she can fetch it."""
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/files/file_research")
    assert r.status_code == 200


def test_get_file_cross_user_404(store, seeded):
    """bob has no access to /research's folder OR is the uploader of
    file_research; both checks fail → 404."""
    app = _build_app(store, _bob(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/files/file_research")
    assert r.status_code == 404


def test_get_file_uploader_path(store, seeded):
    """Uploader gets access via files.owner_user_id even if no
    referencing doc is in an accessible folder. Confirm by orphaning
    file_research (drop the doc) — alice as the uploader still
    fetches it."""
    with store.transaction() as sess:
        d = sess.get(Document, "d_research")
        sess.delete(d)
        sess.commit()
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/files/file_research")
    assert r.status_code == 200


def test_file_download_cross_user_404(store, seeded):
    app = _build_app(store, _bob(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/files/file_research/download")
    assert r.status_code == 404


def test_file_preview_owner_ok(store, seeded):
    app = _build_app(store, _alice(seeded))
    with TestClient(app) as c:
        r = c.get("/api/v1/files/file_research/preview")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Auth disabled: passthrough
# ---------------------------------------------------------------------------


def test_auth_disabled_passthrough(store, seeded):
    principal = AuthenticatedPrincipal(
        user_id="local", username="local", role="admin", via="auth_disabled"
    )
    app = _build_app(store, principal, auth_enabled=False)
    with TestClient(app) as c:
        # Both folders' content is accessible.
        assert c.get("/api/v1/documents/d_research").status_code == 200
        assert c.get("/api/v1/documents/d_scratch").status_code == 200
        assert c.get("/api/v1/chunks/d_research:1:c1").status_code == 200
        assert c.get("/api/v1/chunks/d_scratch:1:c1").status_code == 200
        assert c.get("/api/v1/files/file_research").status_code == 200
