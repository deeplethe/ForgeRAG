"""
Folder CRUD API.

Endpoints:
    GET    /api/v1/folders                   List all folders (flat)
    GET    /api/v1/folders/tree              Full tree (lazy — children of a path)
    GET    /api/v1/folders/info              Folder info by path query param
    POST   /api/v1/folders                   Create folder
    PATCH  /api/v1/folders/rename            Rename a folder by path
    POST   /api/v1/folders/move              Move folder to a new parent
    DELETE /api/v1/folders                   Soft-delete to /__trash__

All mutations flow through FolderService, which is the single
transactional boundary keeping folder_id / path / path_lower in sync.
Permission checks go through PermissionService.require_folder (currently
always-allow).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from persistence.folder_service import (
    ROOT_FOLDER_ID,
    TRASH_FOLDER_ID,
    TRASH_PATH,
    FolderAlreadyExists,
    FolderError,
    FolderIsSystemProtected,
    FolderNotFound,
    FolderService,
    InvalidFolderName,
)
from persistence.permissions import Permission, PermissionService

from ..deps import get_state
from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/folders", tags=["folders"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FolderOut(BaseModel):
    folder_id: str
    path: str
    parent_id: str | None
    name: str
    is_system: bool
    trashed: bool                # derived from path under /__trash__
    child_folders: int
    document_count: int

    model_config = {"from_attributes": True}


class FolderTreeNode(FolderOut):
    children: list["FolderTreeNode"] = Field(default_factory=list)


class CreateFolderReq(BaseModel):
    parent_path: str = Field(..., description="Parent folder path; use '/' for root")
    name: str = Field(..., min_length=1, max_length=255)


class RenameFolderReq(BaseModel):
    path: str
    new_name: str = Field(..., min_length=1, max_length=255)


class MoveFolderReq(BaseModel):
    path: str
    to_parent_path: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _folder_to_out(svc: FolderService, folder) -> FolderOut:
    trashed = folder.path.startswith(TRASH_PATH + "/") or folder.path == TRASH_PATH
    return FolderOut(
        folder_id=folder.folder_id,
        path=folder.path,
        parent_id=folder.parent_id,
        name=folder.name,
        is_system=folder.is_system,
        trashed=trashed,
        child_folders=len(svc.list_children(folder.folder_id)),
        document_count=svc.count_documents(folder.folder_id, recursive=False),
    )


def _excluded_from_user_view(folder) -> bool:
    """True if the folder is inside /__trash__ (show only via trash routes)."""
    if folder.folder_id == TRASH_FOLDER_ID:
        return True
    return folder.path.startswith(TRASH_PATH + "/")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[FolderOut])
def list_folders(
    include_trashed: bool = Query(False, description="Include items under /__trash__"),
    state: AppState = Depends(get_state),
):
    """Flat list of all folders (excluding trashed unless include_trashed)."""
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        from persistence.models import Folder
        from sqlalchemy import select

        all_folders = list(sess.execute(select(Folder).order_by(Folder.path)).scalars())
        if not include_trashed:
            all_folders = [f for f in all_folders if not _excluded_from_user_view(f)]
        return [_folder_to_out(svc, f) for f in all_folders]


@router.get("/tree", response_model=FolderTreeNode)
def get_tree(
    path: str = Query("/", description="Root of the subtree to return"),
    depth: int = Query(2, ge=1, le=6, description="How many levels to expand"),
    include_trashed: bool = Query(False),
    state: AppState = Depends(get_state),
):
    """Lazy-loaded tree — returns N levels of children below `path`."""
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            root = svc.require_by_path(path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {path!r}")

        def build(folder, levels_left: int) -> FolderTreeNode:
            out = _folder_to_out(svc, folder)
            children_models = []
            if levels_left > 0:
                for child in svc.list_children(folder.folder_id):
                    if not include_trashed and _excluded_from_user_view(child):
                        continue
                    children_models.append(build(child, levels_left - 1))
            return FolderTreeNode(**out.model_dump(), children=children_models)

        return build(root, depth)


@router.get("/info", response_model=FolderOut)
def folder_info(
    path: str = Query(..., description="Folder path, e.g. /legal/2024"),
    state: AppState = Depends(get_state),
):
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            f = svc.require_by_path(path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {path!r}")
        return _folder_to_out(svc, f)


@router.post("", response_model=FolderOut, status_code=201)
def create_folder(body: CreateFolderReq, state: AppState = Depends(get_state)):
    perm = PermissionService(state.store)
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            parent = svc.require_by_path(body.parent_path)
        except FolderNotFound:
            raise HTTPException(404, f"parent folder not found: {body.parent_path!r}")
        perm.require_folder(parent.folder_id, Permission.EDIT)
        try:
            new = svc.create(body.parent_path, body.name)
        except InvalidFolderName as e:
            raise HTTPException(422, str(e))
        except FolderAlreadyExists as e:
            raise HTTPException(409, str(e))
        return _folder_to_out(svc, new)


@router.patch("/rename", response_model=FolderOut)
def rename_folder(body: RenameFolderReq, state: AppState = Depends(get_state)):
    perm = PermissionService(state.store)
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            folder = svc.require_by_path(body.path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {body.path!r}")
        perm.require_folder(folder.folder_id, Permission.EDIT)
        try:
            updated = svc.rename(folder.folder_id, body.new_name)
        except InvalidFolderName as e:
            raise HTTPException(422, str(e))
        except FolderAlreadyExists as e:
            raise HTTPException(409, str(e))
        except FolderIsSystemProtected as e:
            raise HTTPException(403, str(e))
        return _folder_to_out(svc, updated)


@router.post("/move", response_model=FolderOut)
def move_folder(body: MoveFolderReq, state: AppState = Depends(get_state)):
    perm = PermissionService(state.store)
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            folder = svc.require_by_path(body.path)
            new_parent = svc.require_by_path(body.to_parent_path)
        except FolderNotFound as e:
            raise HTTPException(404, str(e))
        perm.require_folder(folder.folder_id, Permission.EDIT)
        perm.require_folder(new_parent.folder_id, Permission.EDIT)
        try:
            updated = svc.move(folder.folder_id, body.to_parent_path)
        except FolderAlreadyExists as e:
            raise HTTPException(409, str(e))
        except FolderIsSystemProtected as e:
            raise HTTPException(403, str(e))
        except FolderError as e:
            raise HTTPException(400, str(e))
        return _folder_to_out(svc, updated)


@router.delete("", response_model=FolderOut)
def delete_folder(
    path: str = Query(..., description="Folder path to send to trash"),
    state: AppState = Depends(get_state),
):
    """Soft-delete: move the folder (and its whole subtree) into /__trash__."""
    perm = PermissionService(state.store)
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            folder = svc.require_by_path(path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {path!r}")
        perm.require_folder(folder.folder_id, Permission.ADMIN)
        try:
            trashed = svc.move_to_trash(folder.folder_id)
        except FolderIsSystemProtected as e:
            raise HTTPException(403, str(e))
        return _folder_to_out(svc, trashed)
