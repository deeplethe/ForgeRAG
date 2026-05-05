"""
Multi-user authz on the trash surface.

Setup:

    /research        shared_with [{alice, rw}]
    /scratch         shared_with [{bob, rw}]
    /shared          shared_with [{bob, rw}, {carol, r}]

Then trash one document from each:
    research/q3.pdf  → trashed by alice
    scratch/notes.md → trashed by bob
    shared/memo.pdf  → trashed by bob

What must hold:

    * list:    each user only sees trash from folders they can read.
                 carol sees the /shared item (she has r) but not the
                 others.
    * restore: rw needed; an r-only carol cannot restore /shared/memo.
    * purge:   rw needed; same as restore.
    * partial-success: a batch with one unauthorized item still
                 processes the authorized ones; the unauthorized item
                 lands in ``denied``.
    * orphans: a doc whose original_folder_id no longer exists is
                 admin-only.
    * admin:   bypasses every per-folder check.
    * auth disabled: pass-through (no filtering).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.auth import AuthenticatedPrincipal, AuthorizationService
from api.deps import get_state
from api.routes.trash import router as trash_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import AuthUser, Document, Folder
from persistence.store import Store
from persistence.trash_service import TrashService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "trash.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict:
    """Three users, three folders, and three docs already trashed
    (one per folder). Returns a dict with name → id maps."""
    ids: dict[str, str] = {}
    folder_ids: dict[str, str] = {}
    doc_ids: dict[str, str] = {}

    with store.transaction() as sess:
        for username, role in (
            ("admin", "admin"),
            ("alice", "user"),
            ("bob", "user"),
            ("carol", "user"),
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
            (
                "f_shared",
                "/shared",
                [
                    {"user_id": ids["bob"], "role": "rw"},
                    {"user_id": ids["carol"], "role": "r"},
                ],
            ),
        ):
            folder_ids[fid] = fid
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

        # Now trash one doc per folder. We construct the trashed-state
        # directly rather than going through move_document_to_trash so
        # the fixture stays schema-focused.
        for did, src_folder, src_path, original_filename in (
            ("d_research_q3", "f_research", "/research/q3.pdf", "q3.pdf"),
            ("d_scratch_notes", "f_scratch", "/scratch/notes.md", "notes.md"),
            ("d_shared_memo", "f_shared", "/shared/memo.pdf", "memo.pdf"),
        ):
            trash_path = f"/__trash__/2026_{did}.pdf"
            doc_ids[did] = did
            sess.add(
                Document(
                    doc_id=did,
                    folder_id="__trash__",
                    path=trash_path,
                    filename=original_filename,
                    format="pdf",
                    trashed_metadata={
                        "original_folder_id": src_folder,
                        "original_path": src_path,
                        "trashed_at": "2026-05-05T00:00:00",
                        "trashed_by": "system",
                    },
                )
            )
        sess.commit()

    return {"users": ids, "folders": folder_ids, "docs": doc_ids}


def _state_with_authz(store: Store, *, auth_enabled: bool = True) -> SimpleNamespace:
    """Minimal AppState shim — TrashService only touches store +
    cfg.auth + authz (when present)."""
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        authz=AuthorizationService(store),
    )


# ---------------------------------------------------------------------------
# list — service layer
# ---------------------------------------------------------------------------


def test_list_filters_to_accessible_folders_only(store, seeded):
    """alice has rw on /research only, so she sees one trashed item."""
    state = _state_with_authz(store)
    out = TrashService(state).list(
        user_id=seeded["users"]["alice"], is_admin=False
    )
    doc_ids = [it["doc_id"] for it in out["items"] if it["type"] == "document"]
    assert doc_ids == ["d_research_q3"]


def test_list_carol_sees_shared_via_read_grant(store, seeded):
    """carol has only ``r`` on /shared — she still sees that folder's
    trash (read access is enough for listing)."""
    state = _state_with_authz(store)
    out = TrashService(state).list(
        user_id=seeded["users"]["carol"], is_admin=False
    )
    doc_ids = [it["doc_id"] for it in out["items"] if it["type"] == "document"]
    assert doc_ids == ["d_shared_memo"]


def test_list_admin_sees_everything(store, seeded):
    state = _state_with_authz(store)
    out = TrashService(state).list(
        user_id=seeded["users"]["admin"], is_admin=True
    )
    doc_ids = sorted(
        it["doc_id"] for it in out["items"] if it["type"] == "document"
    )
    assert doc_ids == ["d_research_q3", "d_scratch_notes", "d_shared_memo"]


def test_list_omits_orphans_for_non_admin(store, seeded):
    """A trashed doc whose original_folder_id is missing (legacy row
    or original folder hard-deleted) should be invisible to non-
    admins."""
    with store.transaction() as sess:
        d = sess.get(Document, "d_research_q3")
        d.trashed_metadata = {
            **(d.trashed_metadata or {}),
            "original_folder_id": None,
        }
        sess.commit()
    state = _state_with_authz(store)
    out = TrashService(state).list(
        user_id=seeded["users"]["alice"], is_admin=False
    )
    assert "d_research_q3" not in [
        it["doc_id"] for it in out["items"] if it["type"] == "document"
    ]
    # Admin still sees it (they have role bypass on _user_can).
    out_admin = TrashService(state).list(
        user_id=seeded["users"]["admin"], is_admin=True
    )
    assert "d_research_q3" in [
        it["doc_id"] for it in out_admin["items"] if it["type"] == "document"
    ]


def test_list_passthrough_when_user_id_none(store, seeded):
    """auth-disabled deployments call with ``user_id=None``; the
    service returns everything without filtering."""
    state = _state_with_authz(store, auth_enabled=False)
    out = TrashService(state).list(user_id=None, is_admin=True)
    doc_ids = sorted(
        it["doc_id"] for it in out["items"] if it["type"] == "document"
    )
    assert len(doc_ids) == 3


# ---------------------------------------------------------------------------
# restore — service layer
# ---------------------------------------------------------------------------


def test_restore_rejects_unauthorized_in_partial_batch(store, seeded):
    """alice asks to restore three docs — one of hers, two she doesn't
    own. Hers gets restored; the others land in ``denied``."""
    state = _state_with_authz(store)
    out = TrashService(state).restore(
        doc_ids=["d_research_q3", "d_scratch_notes", "d_shared_memo"],
        user_id=seeded["users"]["alice"],
        is_admin=False,
    )
    restored_ids = [r["doc_id"] for r in out["restored"]]
    denied_ids = [r["doc_id"] for r in out["denied"]]
    assert restored_ids == ["d_research_q3"]
    assert sorted(denied_ids) == ["d_scratch_notes", "d_shared_memo"]


def test_restore_carol_with_read_only_denied(store, seeded):
    """carol has only r on /shared — she can SEE the trash but cannot
    restore (restore is a write op gated on rw)."""
    state = _state_with_authz(store)
    out = TrashService(state).restore(
        doc_ids=["d_shared_memo"],
        user_id=seeded["users"]["carol"],
        is_admin=False,
    )
    assert out["restored"] == []
    assert [r["doc_id"] for r in out["denied"]] == ["d_shared_memo"]


def test_restore_admin_can_restore_anything(store, seeded):
    state = _state_with_authz(store)
    out = TrashService(state).restore(
        doc_ids=["d_scratch_notes"],
        user_id=seeded["users"]["admin"],
        is_admin=True,
    )
    assert [r["doc_id"] for r in out["restored"]] == ["d_scratch_notes"]
    assert out["denied"] == []


# ---------------------------------------------------------------------------
# purge — service layer
# ---------------------------------------------------------------------------


def test_purge_rejects_unauthorized(store, seeded):
    state = _state_with_authz(store)
    out = TrashService(state).purge(
        doc_ids=["d_scratch_notes"],
        user_id=seeded["users"]["alice"],
        is_admin=False,
    )
    assert out["purged_documents"] == 0
    assert [r["doc_id"] for r in out["denied"]] == ["d_scratch_notes"]


def test_purge_carol_read_only_cannot_purge(store, seeded):
    """carol's r grant on /shared is enough to LIST, not to purge."""
    state = _state_with_authz(store)
    out = TrashService(state).purge(
        doc_ids=["d_shared_memo"],
        user_id=seeded["users"]["carol"],
        is_admin=False,
    )
    assert out["purged_documents"] == 0
    assert [r["doc_id"] for r in out["denied"]] == ["d_shared_memo"]


def test_purge_owner_succeeds(store, seeded):
    state = _state_with_authz(store)
    out = TrashService(state).purge(
        doc_ids=["d_research_q3"],
        user_id=seeded["users"]["alice"],
        is_admin=False,
    )
    assert out["purged_documents"] == 1
    assert out["denied"] == []


def test_purge_partial_success(store, seeded):
    """alice purges the one she owns plus one she doesn't — the
    authorized one goes through, the other is reported as denied,
    the call is HTTP 200 not 403."""
    state = _state_with_authz(store)
    out = TrashService(state).purge(
        doc_ids=["d_research_q3", "d_scratch_notes"],
        user_id=seeded["users"]["alice"],
        is_admin=False,
    )
    assert out["purged_documents"] == 1
    assert [r["doc_id"] for r in out["denied"]] == ["d_scratch_notes"]


# ---------------------------------------------------------------------------
# Route layer — sanity check + missing-empty-trash endpoint
# ---------------------------------------------------------------------------


def _build_app(store, principal, *, auth_enabled=True):
    fake_state = SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=auth_enabled)),
        authz=AuthorizationService(store),
    )
    app = FastAPI()
    app.include_router(trash_router)
    app.dependency_overrides[get_state] = lambda: fake_state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    return app


def test_route_list_filters_for_principal(store, seeded):
    principal = AuthenticatedPrincipal(
        user_id=seeded["users"]["alice"],
        username="alice",
        role="user",
        via="session",
    )
    app = _build_app(store, principal)
    with TestClient(app) as c:
        body = c.get("/api/v1/trash").json()
    doc_ids = [it["doc_id"] for it in body["items"] if it["type"] == "document"]
    assert doc_ids == ["d_research_q3"]


def test_route_purge_partial_success_returns_denied(store, seeded):
    principal = AuthenticatedPrincipal(
        user_id=seeded["users"]["alice"],
        username="alice",
        role="user",
        via="session",
    )
    app = _build_app(store, principal)
    with TestClient(app) as c:
        r = c.request(
            "DELETE",
            "/api/v1/trash/items",
            json={
                "doc_ids": ["d_research_q3", "d_scratch_notes"],
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["purged_documents"] == 1
    assert [d["doc_id"] for d in body["denied"]] == ["d_scratch_notes"]


def test_route_no_empty_trash_endpoint(store, seeded):
    """``DELETE /trash`` with no body was the legacy "empty everything"
    endpoint. It's gone in the multi-user redesign — empty-all is a
    foot-gun. The router 405s any request hitting that path now."""
    principal = AuthenticatedPrincipal(
        user_id=seeded["users"]["admin"],
        username="admin",
        role="admin",
        via="session",
    )
    app = _build_app(store, principal)
    with TestClient(app) as c:
        r = c.delete("/api/v1/trash")
    assert r.status_code in (404, 405)
