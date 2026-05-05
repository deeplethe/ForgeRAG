"""
Trash API — list / restore / permanent delete / stats.

Endpoints:
    GET    /api/v1/trash                 List trashed items the caller can see
    GET    /api/v1/trash/stats           Aggregate stats (admin = all; user = their accessible)
    POST   /api/v1/trash/restore         Restore selected items
    DELETE /api/v1/trash/items           Permanently delete selected items

Multi-user authz: each trashed item carries
``trashed_metadata.original_folder_id``. Listing filters to items
where the caller has at least ``r`` on the source folder; restore
gates each item on ``soft_delete`` (= ``rw``); purge gates on
``purge`` (also ``rw`` under the simplified role matrix). Items the
caller can't act on go to ``denied`` instead of failing the whole
batch — partial-success semantics so a 50-item purge with one
unauthorized doesn't roll the rest back.

Admins (``role=admin``) bypass per-folder checks. There is no
"empty whole trash" endpoint — that's a foot-gun in multi-user;
operators pick items via ``DELETE /trash/items`` instead, and the
nightly auto-purge handles age-based cleanup.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from persistence.trash_service import TrashService

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
from ..state import AppState

router = APIRouter(prefix="/api/v1/trash", tags=["trash"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RestoreReq(BaseModel):
    doc_ids: list[str] | None = Field(default=None)
    folder_paths: list[str] | None = Field(default=None)


class PurgeItemsReq(BaseModel):
    doc_ids: list[str] | None = Field(default=None)
    folder_paths: list[str] | None = Field(default=None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_scope(
    state: AppState, principal: AuthenticatedPrincipal
) -> tuple[str | None, bool]:
    """Convert the principal into ``(user_id, is_admin)`` for the
    service layer.

    When auth is disabled (or the principal came from the synthetic
    ``local`` admin) we pass ``user_id=None`` so the service skips
    filtering — the caller is trusted with the whole trash.
    """
    if not state.cfg.auth.enabled or principal.via == "auth_disabled":
        return None, True
    return principal.user_id, principal.role == "admin"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_trash(
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    user_id, is_admin = _user_scope(state, principal)
    return TrashService(state).list(user_id=user_id, is_admin=is_admin)


@router.get("/stats")
def trash_stats(
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Aggregate counts. Non-admin users see only their own
    accessible-folder slice; admins see the full bin."""
    user_id, is_admin = _user_scope(state, principal)
    listing = TrashService(state).list(user_id=user_id, is_admin=is_admin)
    items = listing["items"]
    return {
        "items": sum(1 for it in items if it["type"] == "document"),
        "top_level_folders": sum(1 for it in items if it["type"] == "folder"),
    }


@router.post("/restore")
def restore_items(
    body: RestoreReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    user_id, is_admin = _user_scope(state, principal)
    return TrashService(state).restore(
        doc_ids=body.doc_ids,
        folder_paths=body.folder_paths,
        user_id=user_id,
        is_admin=is_admin,
    )


@router.delete("/items")
def purge_items(
    body: PurgeItemsReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    if not body.doc_ids and not body.folder_paths:
        raise HTTPException(400, "specify at least one doc_id or folder_path")
    user_id, is_admin = _user_scope(state, principal)
    return TrashService(state).purge(
        doc_ids=body.doc_ids,
        folder_paths=body.folder_paths,
        user_id=user_id,
        is_admin=is_admin,
    )
