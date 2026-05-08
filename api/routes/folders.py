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
from persistence.invitation_service import (
    FolderInvitationService,
    InvitationError,
    InvitationFolderMissing,
)
from persistence.models import AuthUser, Folder
from persistence.scope import ScopeMode, ScopeService

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state, require_folder_access
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


# ---------------------------------------------------------------------------
# Spaces — per-user view of the global tree
# ---------------------------------------------------------------------------
#
# See ``docs/roadmaps/per-user-spaces.md`` for design notes. In one
# sentence: every grant the principal holds becomes its own
# top-level "space"; the user's UI never surfaces the system path
# parents (``/users/``, ``/eng/``, etc.) that connect grants in
# the global tree.


class SpaceOut(BaseModel):
    space_id: str
    name: str
    abs_root: str    # for back-compat with callers that still need absolute paths
    role: str        # "rw" / "r"
    is_personal: bool


class SpaceTree(BaseModel):
    space: SpaceOut
    tree: FolderTreeNode


class SpacesResponse(BaseModel):
    spaces: list[SpaceTree]


@router.get("/spaces", response_model=SpacesResponse)
def get_spaces(
    depth: int = Query(2, ge=1, le=6, description="How many levels to expand inside each space"),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Return the principal's workspace as a flat list of spaces.

    Each space carries its own subtree starting at the grant
    root. Folder paths inside are still absolute (Phase 1 keeps
    backend ops on absolute paths); the FRONTEND treats the
    space as its tree's root and renders sub-paths relative to
    it. Phase 2 / 3 extend the translation to doc detail,
    search, citations, and chat scope picker — see roadmap.

    Empty spaces list = the user has no grants (admin needs to
    share a folder with them, or registration didn't auto-
    create their personal folder yet).
    """
    from ..auth.path_remap import PathRemap

    remap = PathRemap.build(state, principal)
    if not remap.spaces:
        return SpacesResponse(spaces=[])

    out: list[SpaceTree] = []
    with state.store.transaction() as sess:
        svc = FolderService(sess)

        def build(folder, levels_left: int) -> FolderTreeNode:
            o = _folder_to_out(svc, folder)
            children_models = []
            if levels_left > 0:
                for child in svc.list_children(folder.folder_id):
                    if _excluded_from_user_view(child):
                        continue
                    children_models.append(build(child, levels_left - 1))
            return FolderTreeNode(**o.model_dump(), children=children_models)

        for space in remap.spaces:
            try:
                root_folder = svc.require_by_path(space.abs_root)
            except FolderNotFound:
                # Grant points at a folder that's been deleted —
                # log and skip rather than 500. Garbage-collect
                # path is a separate concern (admin user-management).
                log.warning(
                    "space %s points at missing folder %s — skipping",
                    space.space_id, space.abs_root,
                )
                continue
            out.append(SpaceTree(
                space=SpaceOut(
                    space_id=space.space_id,
                    name=space.name,
                    abs_root=space.abs_root,
                    role=space.role,
                    is_personal=space.is_personal,
                ),
                tree=build(root_folder, depth),
            ))

    return SpacesResponse(spaces=out)


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
def create_folder(
    body: CreateFolderReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Create a folder under ``parent_path``. Caller must have ``rw``
    on the parent (or be admin); the new folder copies the parent's
    ``shared_with`` so the creator inherits the access they used to
    be allowed to write under there in the first place."""
    scope = ScopeService(state.store)
    # When auth is on the principal must have rw / admin under the
    # parent. When auth is off the synthetic local-admin principal
    # passes the check trivially.
    if state.cfg.auth.enabled:
        with state.store.transaction() as sess:
            parent = FolderService(sess).require_by_path(body.parent_path)
            if not state.authz.can(
                principal.user_id, parent.folder_id, "upload"
            ):
                raise HTTPException(
                    403, f"forbidden: write under {body.parent_path!r}"
                )
    with state.store.transaction() as sess:
        svc = FolderService(sess, actor_id=principal.user_id)
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
def rename_folder(
    body: RenameFolderReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Authz: caller must have ``rw`` on the folder being renamed."""
    scope = ScopeService(state.store)
    pending_ops: list[dict] = []
    with state.store.transaction() as sess:
        svc = FolderService(sess, actor_id=principal.user_id)
        try:
            folder = svc.require_by_path(body.path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {body.path!r}")
        require_folder_access(state, principal, folder.folder_id, "rename")
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
def move_folder(
    body: MoveFolderReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Authz: caller must have ``rw`` on BOTH the folder being
    moved AND the new parent folder. Cross-folder moves between
    folders the caller can't both write are rejected."""
    scope = ScopeService(state.store)
    pending_ops: list[dict] = []
    with state.store.transaction() as sess:
        svc = FolderService(sess, actor_id=principal.user_id)
        try:
            folder = svc.require_by_path(body.path)
            new_parent = svc.require_by_path(body.to_parent_path)
        except FolderNotFound as e:
            raise HTTPException(404, str(e))
        require_folder_access(state, principal, folder.folder_id, "rename")
        require_folder_access(state, principal, new_parent.folder_id, "upload")
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
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Soft-delete: move the folder (and its whole subtree) into /__trash__.

    Authz: caller must have ``rw`` (manage-level) on the folder.
    Per the action matrix in ``api/auth/authz.py``,
    ``delete_folder`` falls under MANAGE — only ``rw`` members
    pass; ``r`` members do not. Admin role bypasses."""
    scope = ScopeService(state.store)
    pending_ops: list[dict] = []
    with state.store.transaction() as sess:
        svc = FolderService(sess, actor_id=principal.user_id)
        try:
            folder = svc.require_by_path(path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {path!r}")
        require_folder_access(
            state, principal, folder.folder_id, "delete_folder"
        )
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


# ---------------------------------------------------------------------------
# Folder invitations
# ---------------------------------------------------------------------------
#
# An invitation is a one-shot signed link the owner can paste into
# whatever channel they use (no SMTP in v1). The recipient lands on
# /auth/register?invite=<token> (frontend route), the
# unauthenticated /api/v1/auth/invitations/{token}/preview shows
# them what they're accepting, and the registration / login flow
# (S4) calls FolderInvitationService.consume on completion.
#
# Routes here are owner / admin only — same gate as member CRUD.


class IssueInvitationRequest(BaseModel):
    email: str
    role: str = Field(..., pattern="^(r|rw)$")
    ttl_days: int | None = Field(None, ge=1, le=90)


class IssuedInvitationOut(BaseModel):
    invitation_id: str
    invitation_url: str
    folder_id: str
    folder_path: str
    target_email: str
    role: str
    expires_at: str  # iso 8601


class InvitationRowOut(BaseModel):
    invitation_id: str
    folder_id: str
    target_email: str
    role: str
    inviter_user_id: str
    created_at: str
    expires_at: str
    consumed_at: str | None = None
    consumed_by_user_id: str | None = None


def _invitation_url(token: str) -> str:
    """Compose the recipient-facing URL. The frontend route
    ``/auth/register?invite=<token>`` consumes the token after the
    user registers / logs in. Server-side we never store the URL,
    only the hash; the inviter copy/pastes the returned string.
    """
    return f"/auth/register?invite={token}"


@router.post(
    "/{folder_id}/invitations",
    response_model=IssuedInvitationOut,
    status_code=201,
)
def issue_folder_invitation(
    folder_id: str,
    body: IssueInvitationRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Mint an invitation link the owner copies into whatever channel
    they use. The raw token is returned exactly once."""
    _require_share_permission(state, principal, folder_id, "share")
    ttl = body.ttl_days or state.cfg.auth.invitation_ttl_days
    with state.store.transaction() as sess:
        try:
            issued = FolderInvitationService(sess).create(
                folder_id=folder_id,
                target_email=body.email,
                role=body.role,  # type: ignore[arg-type]
                inviter_user_id=principal.user_id,
                ttl_days=ttl,
            )
        except InvitationFolderMissing:
            raise HTTPException(404, f"folder not found: {folder_id!r}")
        except InvitationError as e:
            raise HTTPException(400, str(e))
    return IssuedInvitationOut(
        invitation_id=issued.invitation_id,
        invitation_url=_invitation_url(issued.token),
        folder_id=issued.folder_id,
        folder_path=issued.folder_path,
        target_email=issued.target_email,
        role=issued.role,
        expires_at=issued.expires_at.isoformat(),
    )


@router.get(
    "/{folder_id}/invitations",
    response_model=list[InvitationRowOut],
)
def list_folder_invitations(
    folder_id: str,
    include_consumed: bool = Query(False),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Outstanding invitations on this folder. Owner / admin only —
    invitee emails are sensitive."""
    _require_share_permission(state, principal, folder_id, "share")
    with state.store.transaction() as sess:
        rows = FolderInvitationService(sess).list(
            folder_id=folder_id, include_consumed=include_consumed
        )
    return [
        InvitationRowOut(
            invitation_id=r.invitation_id,
            folder_id=r.folder_id,
            target_email=r.target_email,
            role=r.role,
            inviter_user_id=r.inviter_user_id,
            created_at=r.created_at.isoformat(),
            expires_at=r.expires_at.isoformat(),
            consumed_at=r.consumed_at.isoformat() if r.consumed_at else None,
            consumed_by_user_id=r.consumed_by_user_id,
        )
        for r in rows
    ]


@router.delete(
    "/{folder_id}/invitations/{invitation_id}",
    status_code=204,
)
def revoke_folder_invitation(
    folder_id: str,
    invitation_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Hard-revoke: the token immediately stops working."""
    _require_share_permission(state, principal, folder_id, "share")
    with state.store.transaction() as sess:
        FolderInvitationService(sess).revoke(
            invitation_id=invitation_id,
            actor_user_id=principal.user_id,
        )
    return None
