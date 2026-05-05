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
Scope checks go through ScopeService.require_folder (always-allow in
single-tenant mode; the hooks are reserved for future read-only /
archive-lock flags — not ACL).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from persistence.folder_service import (
    TRASH_FOLDER_ID,
    TRASH_PATH,
    FolderAlreadyExists,
    FolderError,
    FolderIsSystemProtected,
    FolderNotFound,
    FolderService,
    InvalidFolderName,
)
from persistence.folder_share_service import FolderNotFound as ShareFolderNotFound
from persistence.folder_share_service import (
    FolderShareError,
    FolderShareService,
    MembershipConstraintError,
)
from persistence.folder_share_service import UserNotFound as ShareUserNotFound
from persistence.models import AuthUser, Folder
from persistence.scope import ScopeMode, ScopeService

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
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
    trashed: bool  # derived from path under /__trash__
    child_folders: int
    document_count: int

    model_config = {"from_attributes": True}


class FolderTreeNode(FolderOut):
    children: list[FolderTreeNode] = Field(default_factory=list)


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
        from sqlalchemy import select

        from persistence.models import Folder

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
    scope = ScopeService(state.store)
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            parent = svc.require_by_path(body.parent_path)
        except FolderNotFound:
            raise HTTPException(404, f"parent folder not found: {body.parent_path!r}")
        scope.require_folder(parent.folder_id, ScopeMode.WRITE)
        try:
            new = svc.create(body.parent_path, body.name)
        except InvalidFolderName as e:
            raise HTTPException(422, str(e))
        except FolderAlreadyExists as e:
            raise HTTPException(409, str(e))
        return _folder_to_out(svc, new)


def _apply_post_commit_cross_store(
    pending_ops: list[dict],
    state: AppState,
) -> None:
    """Run the sync-path update_paths on Chroma + Neo4j after the PG
    transaction commits. The FolderService already queued these ops
    below the ``_CROSS_STORE_SYNC_THRESHOLD``; this function is a
    thin adapter that matches the service's expected kwargs.
    """
    if not pending_ops:
        return
    import logging

    log = logging.getLogger(__name__)
    for op in pending_ops:
        try:
            if state.graph_store is not None and hasattr(state.graph_store, "update_paths"):
                state.graph_store.update_paths(op["old_prefix"], op["new_prefix"])
        except Exception as e:
            log.warning("graph update_paths sync failed for %s: %s", op, e)
        try:
            if state.vector_store is not None and hasattr(state.vector_store, "update_paths"):
                state.vector_store.update_paths(op["old_prefix"], op["new_prefix"])
        except Exception as e:
            log.warning("vector update_paths sync failed for %s: %s", op, e)


@router.patch("/rename", response_model=FolderOut)
def rename_folder(body: RenameFolderReq, state: AppState = Depends(get_state)):
    scope = ScopeService(state.store)
    pending_ops: list[dict] = []
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            folder = svc.require_by_path(body.path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {body.path!r}")
        scope.require_folder(folder.folder_id, ScopeMode.WRITE)
        try:
            updated = svc.rename(folder.folder_id, body.new_name)
        except InvalidFolderName as e:
            raise HTTPException(422, str(e))
        except FolderAlreadyExists as e:
            raise HTTPException(409, str(e))
        except FolderIsSystemProtected as e:
            raise HTTPException(403, str(e))
        out = _folder_to_out(svc, updated)
        # Capture pending cross-store ops BEFORE the session commits so we
        # can apply them AFTER — applying pre-commit would make Chroma /
        # Neo4j observe a rename that PG could still roll back.
        pending_ops = list(svc.pending_sync_ops)
    _apply_post_commit_cross_store(pending_ops, state)
    return out


@router.post("/move", response_model=FolderOut)
def move_folder(body: MoveFolderReq, state: AppState = Depends(get_state)):
    scope = ScopeService(state.store)
    pending_ops: list[dict] = []
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            folder = svc.require_by_path(body.path)
            new_parent = svc.require_by_path(body.to_parent_path)
        except FolderNotFound as e:
            raise HTTPException(404, str(e))
        scope.require_folder(folder.folder_id, ScopeMode.WRITE)
        scope.require_folder(new_parent.folder_id, ScopeMode.WRITE)
        try:
            updated = svc.move(folder.folder_id, body.to_parent_path)
        except FolderAlreadyExists as e:
            raise HTTPException(409, str(e))
        except FolderIsSystemProtected as e:
            raise HTTPException(403, str(e))
        except FolderError as e:
            raise HTTPException(400, str(e))
        out = _folder_to_out(svc, updated)
        pending_ops = list(svc.pending_sync_ops)
    _apply_post_commit_cross_store(pending_ops, state)
    return out


@router.delete("", response_model=FolderOut)
def delete_folder(
    path: str = Query(..., description="Folder path to send to trash"),
    state: AppState = Depends(get_state),
):
    """Soft-delete: move the folder (and its whole subtree) into /__trash__."""
    scope = ScopeService(state.store)
    pending_ops: list[dict] = []
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            folder = svc.require_by_path(path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {path!r}")
        scope.require_folder(folder.folder_id, ScopeMode.MANAGE)
        try:
            trashed = svc.move_to_trash(folder.folder_id)
        except FolderIsSystemProtected as e:
            raise HTTPException(403, str(e))
        out = _folder_to_out(svc, trashed)
        pending_ops = list(svc.pending_sync_ops)
    _apply_post_commit_cross_store(pending_ops, state)
    return out


# ---------------------------------------------------------------------------
# Folder membership (multi-user)
# ---------------------------------------------------------------------------
#
# All endpoints below run AuthorizationService.can(folder_id, "share")
# to gate writes — only the folder owner or admin can edit membership.
# Reads are allowed for any user with at least 'r' on the folder, so
# rw / r members can see who else they're sharing with. Privacy-
# sensitive surfaces (conversations, research) are NOT routed through
# this — they're per-user and admin doesn't bypass them.


class MemberOut(BaseModel):
    user_id: str
    username: str
    email: str | None = None
    display_name: str | None = None
    role: str  # 'owner' | 'r' | 'rw'
    source: str  # 'owner' | 'direct' | 'inherited:<folder_id>'


class AddMemberRequest(BaseModel):
    email: str = Field(..., description="Email of an existing registered user")
    role: str = Field(..., pattern="^(r|rw)$")


class UpdateMemberRequest(BaseModel):
    role: str = Field(..., pattern="^(r|rw)$")


def _require_share_permission(
    state: AppState, principal: AuthenticatedPrincipal, folder_id: str, action: str
) -> None:
    """Gate folder-membership mutations behind owner / admin.

    Reads use the milder check (READ on the folder); writes go
    through ``share`` (a MANAGE_ACTION which only owner / admin
    pass). Auth-disabled deployments skip the check entirely.
    """
    if not state.cfg.auth.enabled:
        return
    if not state.authz.can(principal.user_id, folder_id, action):
        raise HTTPException(403, f"forbidden: {action} on folder {folder_id}")


@router.get("/{folder_id}/members", response_model=list[MemberOut])
def list_folder_members(
    folder_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """List effective members. Visible to anyone with read access."""
    _require_share_permission(state, principal, folder_id, "read")
    with state.store.transaction() as sess:
        try:
            members = FolderShareService(sess).list_members(folder_id)
        except ShareFolderNotFound:
            raise HTTPException(404, f"folder not found: {folder_id!r}")
    return [MemberOut(**m.__dict__) for m in members]


@router.post("/{folder_id}/members", response_model=list[MemberOut], status_code=201)
def add_folder_member(
    folder_id: str,
    body: AddMemberRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Grant a user access to the folder by email. The cascade is
    applied to descendants."""
    _require_share_permission(state, principal, folder_id, "share")
    with state.store.transaction() as sess:
        target = sess.execute(
            select(AuthUser).where(AuthUser.email == body.email)
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(404, f"no user with email {body.email!r}")
        try:
            FolderShareService(sess).set_member_role(
                folder_id=folder_id,
                user_id=target.user_id,
                role=body.role,  # type: ignore[arg-type]
                actor_user_id=principal.user_id,
            )
        except ShareFolderNotFound:
            raise HTTPException(404, f"folder not found: {folder_id!r}")
        except MembershipConstraintError as e:
            raise HTTPException(409, str(e))
        except FolderShareError as e:
            raise HTTPException(400, str(e))
        members = FolderShareService(sess).list_members(folder_id)
    return [MemberOut(**m.__dict__) for m in members]


@router.patch(
    "/{folder_id}/members/{user_id}", response_model=list[MemberOut]
)
def update_folder_member(
    folder_id: str,
    user_id: str,
    body: UpdateMemberRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Change a member's role. Cascades the same as a fresh add."""
    _require_share_permission(state, principal, folder_id, "share")
    with state.store.transaction() as sess:
        # Reject editing rows that are inherited from an ancestor —
        # the UI should send the request to the ancestor's folder
        # instead. We detect inherited entries by checking the
        # ancestor walk; if the nearest ancestor with a grant has
        # the same role, this entry is a cascade copy.
        svc = FolderShareService(sess)
        folder = sess.get(Folder, folder_id)
        if folder is None:
            raise HTTPException(404, f"folder not found: {folder_id!r}")
        ancestor_id, ancestor_role = svc._nearest_ancestor_grant(folder, user_id)
        existing_entries = [
            e for e in (folder.shared_with or [])
            if isinstance(e, dict) and e.get("user_id") == user_id
        ]
        if existing_entries:
            current_role = existing_entries[0].get("role")
            if ancestor_role == current_role:
                raise HTTPException(
                    409,
                    f"member is inherited from ancestor folder "
                    f"{ancestor_id!r} — edit there instead",
                )
        try:
            svc.set_member_role(
                folder_id=folder_id,
                user_id=user_id,
                role=body.role,  # type: ignore[arg-type]
                actor_user_id=principal.user_id,
            )
        except MembershipConstraintError as e:
            raise HTTPException(409, str(e))
        except FolderShareError as e:
            raise HTTPException(400, str(e))
        members = svc.list_members(folder_id)
    return [MemberOut(**m.__dict__) for m in members]


@router.delete(
    "/{folder_id}/members/{user_id}", response_model=list[MemberOut]
)
def remove_folder_member(
    folder_id: str,
    user_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Drop a member. Rejected when the user still has access via
    an ancestor (the caller has to remove from the ancestor first
    or move this folder out from under it)."""
    _require_share_permission(state, principal, folder_id, "share")
    with state.store.transaction() as sess:
        svc = FolderShareService(sess)
        try:
            svc.remove_member(
                folder_id=folder_id,
                user_id=user_id,
                actor_user_id=principal.user_id,
            )
        except ShareFolderNotFound:
            raise HTTPException(404, f"folder not found: {folder_id!r}")
        except ShareUserNotFound:
            raise HTTPException(404, f"user not found: {user_id!r}")
        except MembershipConstraintError as e:
            raise HTTPException(409, str(e))
        except FolderShareError as e:
            raise HTTPException(400, str(e))
        members = svc.list_members(folder_id)
    return [MemberOut(**m.__dict__) for m in members]
