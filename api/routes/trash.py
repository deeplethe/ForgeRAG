"""
Trash API — list / restore / permanent delete / empty / stats.

Endpoints:
    GET    /api/v1/trash                 List trashed items
    GET    /api/v1/trash/stats           Aggregate stats
    POST   /api/v1/trash/restore         Restore selected items
    DELETE /api/v1/trash/items           Permanently delete selected items
    DELETE /api/v1/trash                 Empty trash entirely
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from persistence.scope import ScopeMode, ScopeService
from persistence.trash_service import TrashService

from ..deps import get_state
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
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_trash(state: AppState = Depends(get_state)):
    scope = ScopeService(state.store)
    # Listing trash requires the root folder to be in READ scope
    scope.require_folder("__root__", ScopeMode.READ)
    svc = TrashService(state)
    return svc.list()


@router.get("/stats")
def trash_stats(state: AppState = Depends(get_state)):
    from sqlalchemy import func, select

    from persistence.folder_service import TRASH_FOLDER_ID, TRASH_PATH
    from persistence.models import Document, Folder

    with state.store.transaction() as sess:
        doc_count = sess.execute(
            select(func.count()).select_from(Document).where(Document.path.like(TRASH_PATH + "/%"))
        ).scalar_one()
        folder_count = sess.execute(
            select(func.count())
            .select_from(Folder)
            .where((Folder.path.like(TRASH_PATH + "/%")) & (Folder.parent_id == TRASH_FOLDER_ID))
        ).scalar_one()
    return {"items": int(doc_count or 0), "top_level_folders": int(folder_count or 0)}


@router.post("/restore")
def restore_items(body: RestoreReq, state: AppState = Depends(get_state)):
    scope = ScopeService(state.store)
    scope.require_folder("__root__", ScopeMode.WRITE)
    svc = TrashService(state)
    result = svc.restore(doc_ids=body.doc_ids, folder_paths=body.folder_paths)
    return result


@router.delete("/items")
def purge_items(body: PurgeItemsReq, state: AppState = Depends(get_state)):
    scope = ScopeService(state.store)
    scope.require_folder("__root__", ScopeMode.MANAGE)
    svc = TrashService(state)
    if not body.doc_ids and not body.folder_paths:
        raise HTTPException(400, "specify at least one doc_id or folder_path")
    return svc.purge(doc_ids=body.doc_ids, folder_paths=body.folder_paths)


@router.delete("")
def empty_trash(state: AppState = Depends(get_state)):
    scope = ScopeService(state.store)
    scope.require_folder("__root__", ScopeMode.MANAGE)
    svc = TrashService(state)
    return svc.empty()
