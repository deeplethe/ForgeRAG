"""
Project CRUD + member management.

DEPRECATED (v1.0 folder-as-cwd refactor): the Project entity was the
pre-folder-as-cwd home for "the agent's workspace for a piece of work."
v1.0 replaces this with the per-user workdir tree at
``<user_workdirs_root>/<user_id>/`` — chats carry a ``cwd_path`` instead
of a ``project_id``. These endpoints are kept rendering for already-
existing project-bound conversations and the legacy ProjectDetail UI;
new code should use the workdir API (``/api/v1/workdir/...``) and the
folder-as-cwd Chat banner.

Endpoints:
    GET    /api/v1/projects                      List projects visible to caller
    POST   /api/v1/projects                      Create
    GET    /api/v1/projects/{project_id}         Detail
    PATCH  /api/v1/projects/{project_id}         Rename / edit description
    DELETE /api/v1/projects/{project_id}         Soft-delete to /__trash__/

    GET    /api/v1/projects/{project_id}/members              List
    POST   /api/v1/projects/{project_id}/members              Invite (by email)
    PATCH  /api/v1/projects/{project_id}/members/{user_id}    Change role
    DELETE /api/v1/projects/{project_id}/members/{user_id}    Remove

All mutations flow through ``ProjectService``, which owns the
relational lifecycle AND the on-disk workdir scaffolding under
``<cfg.agent.projects_root>/<project_id>/``.

Authz follows the same project-scoped pattern as folders: the
service's ``can_access(project, user_id, action)`` is the single
source of truth — owner has everything, ``rw`` members can read +
write, ``r`` members can only read; admin role bypasses globally.
Membership *mutations* (share / unshare / role change) are owner-or-
admin only — strictly tighter than folder shares because project
access is more sensitive (live agent execution + private workdirs).
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from persistence.models import AuthUser, Project
from persistence.project_service import (
    InvalidProjectName,
    InvalidProjectRole,
    ProjectError,
    ProjectMemberConflict,
    ProjectMemberNotFound,
    ProjectNotFound,
    ProjectService,
)

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MemberOut(BaseModel):
    user_id: str
    username: str
    email: str | None = None
    display_name: str | None = None
    role: str  # 'owner' | 'r' (read-only viewer; no 'rw' — see service docs)


class ProjectOut(BaseModel):
    project_id: str
    name: str
    description: str | None
    workdir_path: str
    owner_user_id: str
    owner_username: str | None = None
    role: str  # caller's effective role: 'owner' | 'rw' | 'r' | 'admin'
    member_count: int
    trashed: bool
    created_at: str | None
    updated_at: str | None
    last_active_at: str | None


class CreateProjectReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=4096)


class UpdateProjectReq(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=4096)


class AddMemberReq(BaseModel):
    email: str = Field(..., description="Email of an existing registered user")
    # Read-only share is the only sharing mode projects support. The
    # field is kept for forward-compatibility (Phase 6+ might surface
    # other roles via UI) but the only accepted value is 'r'.
    role: str = Field(default="r", pattern="^r$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _projects_root(state: AppState) -> Path:
    """Resolve the on-disk root for project workdirs from config.
    Falls back to ``./storage/projects`` when the operator hasn't
    customised ``agent.projects_root`` (the AgentConfig default)."""
    return Path(getattr(state.cfg.agent, "projects_root", "./storage/projects"))


def _is_admin(state: AppState, principal: AuthenticatedPrincipal) -> bool:
    """Auth-disabled deployments synthesise a local-admin principal —
    treat as admin so single-user dev setups behave like before. With
    auth on, admin role on auth_users is the bypass."""
    if not state.cfg.auth.enabled:
        return True
    return principal.role == "admin" or principal.via == "auth_disabled"


def _effective_role(
    proj: Project,
    user_id: str,
    *,
    is_admin: bool,
) -> str:
    """The caller's effective role on ``proj`` — drives UI affordances."""
    if proj.owner_user_id == user_id:
        return "owner"
    if is_admin:
        return "admin"
    for m in proj.shared_with or []:
        if (m or {}).get("user_id") == user_id:
            return m.get("role", "r")
    return "r"


def _project_to_out(
    proj: Project,
    *,
    role: str,
    owner_username: str | None,
) -> ProjectOut:
    return ProjectOut(
        project_id=proj.project_id,
        name=proj.name,
        description=proj.description,
        workdir_path=proj.workdir_path,
        owner_user_id=proj.owner_user_id,
        owner_username=owner_username,
        role=role,
        member_count=len(proj.shared_with or []),
        trashed=proj.trashed_metadata is not None,
        created_at=proj.created_at.isoformat() if proj.created_at else None,
        updated_at=proj.updated_at.isoformat() if proj.updated_at else None,
        last_active_at=(
            proj.last_active_at.isoformat() if proj.last_active_at else None
        ),
    )


def _resolve_project_or_404(
    svc: ProjectService,
    project_id: str,
) -> Project:
    """Load a project row; raise 404 (never 403) on miss/no-access."""
    try:
        return svc.require(project_id)
    except ProjectNotFound:
        raise HTTPException(404, "project not found")


def _username_for(sess, user_id: str | None) -> str | None:
    if not user_id:
        return None
    user = sess.get(AuthUser, user_id)
    return user.username if user else None


# ---------------------------------------------------------------------------
# Routes — project CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ProjectOut])
def list_projects(
    include_trashed: bool = False,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Projects the caller can see: owned + shared. Admins see all."""
    is_admin = _is_admin(state, principal)
    with state.store.transaction() as sess:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        rows = svc.list_for_user(
            principal.user_id,
            is_admin=is_admin,
            include_trashed=include_trashed,
        )
        # Pre-fetch owner usernames in one pass so the list endpoint
        # stays O(1) DB calls regardless of project count.
        owner_ids = {p.owner_user_id for p in rows}
        owners: dict[str, str] = {}
        if owner_ids:
            for u in sess.execute(
                select(AuthUser).where(AuthUser.user_id.in_(owner_ids))
            ).scalars():
                owners[u.user_id] = u.username
        out = []
        for p in rows:
            out.append(
                _project_to_out(
                    p,
                    role=_effective_role(
                        p, principal.user_id, is_admin=is_admin
                    ),
                    owner_username=owners.get(p.owner_user_id),
                )
            )
        return out


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    body: CreateProjectReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Create a project. The caller becomes the owner; the on-disk
    workdir is scaffolded with the soft-conventional layout
    (``inputs/`` / ``outputs/`` / ``scratch/`` / ``.agent-state/``)
    plus a generated ``README.md``."""
    with state.store.transaction() as sess:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        try:
            proj = svc.create(
                name=body.name,
                owner_user_id=principal.user_id,
                description=body.description,
            )
        except InvalidProjectName as e:
            raise HTTPException(422, str(e))
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return _project_to_out(
            proj,
            role="owner",
            owner_username=_username_for(sess, proj.owner_user_id),
        )


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    is_admin = _is_admin(state, principal)
    with state.store.transaction() as sess:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        proj = _resolve_project_or_404(svc, project_id)
        if not svc.can_access(
            proj, principal.user_id, "read", is_admin=is_admin
        ):
            raise HTTPException(404, "project not found")
        return _project_to_out(
            proj,
            role=_effective_role(
                proj, principal.user_id, is_admin=is_admin
            ),
            owner_username=_username_for(sess, proj.owner_user_id),
        )


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str,
    body: UpdateProjectReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Rename + edit description. Caller needs ``write`` (owner /
    rw / admin)."""
    is_admin = _is_admin(state, principal)
    with state.store.transaction() as sess:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        proj = _resolve_project_or_404(svc, project_id)
        if not svc.can_access(
            proj, principal.user_id, "write", is_admin=is_admin
        ):
            raise HTTPException(404, "project not found")
        try:
            if body.name is not None:
                proj = svc.rename(project_id, body.name)
            if body.description is not None:
                proj = svc.update_description(project_id, body.description)
        except InvalidProjectName as e:
            raise HTTPException(422, str(e))
        return _project_to_out(
            proj,
            role=_effective_role(
                proj, principal.user_id, is_admin=is_admin
            ),
            owner_username=_username_for(sess, proj.owner_user_id),
        )


@router.delete("/{project_id}", response_model=ProjectOut)
def delete_project(
    project_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Soft-delete: move both the row's marker AND the on-disk
    workdir into the projects __trash__ folder. Owner / admin only —
    this is intentionally tighter than the folders' delete gate
    because losing a project workdir loses agent state, not just a
    folder of docs."""
    is_admin = _is_admin(state, principal)
    with state.store.transaction() as sess:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        proj = _resolve_project_or_404(svc, project_id)
        if not svc.can_access(
            proj, principal.user_id, "delete", is_admin=is_admin
        ):
            raise HTTPException(404, "project not found")
        try:
            proj = svc.move_to_trash(project_id)
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return _project_to_out(
            proj,
            role=_effective_role(
                proj, principal.user_id, is_admin=is_admin
            ),
            owner_username=_username_for(sess, proj.owner_user_id),
        )


# ---------------------------------------------------------------------------
# Routes — membership
# ---------------------------------------------------------------------------


def _enrich_members(sess, proj: Project) -> list[MemberOut]:
    """Compose owner + shared_with into the wire shape, joining
    auth_users for username / email / display_name."""
    out: list[MemberOut] = []
    user_ids = [proj.owner_user_id] + [
        (m or {}).get("user_id")
        for m in (proj.shared_with or [])
        if (m or {}).get("user_id")
    ]
    rows = {
        u.user_id: u
        for u in sess.execute(
            select(AuthUser).where(AuthUser.user_id.in_(user_ids))
        ).scalars()
    }
    owner = rows.get(proj.owner_user_id)
    if owner is not None:
        out.append(
            MemberOut(
                user_id=owner.user_id,
                username=owner.username,
                email=owner.email,
                display_name=owner.display_name,
                role="owner",
            )
        )
    for m in proj.shared_with or []:
        uid = (m or {}).get("user_id")
        if not uid:
            continue
        u = rows.get(uid)
        if u is None:
            # Stale grant referencing a deleted user — skip rather
            # than 500. The unshare path will purge it on next edit.
            continue
        out.append(
            MemberOut(
                user_id=u.user_id,
                username=u.username,
                email=u.email,
                display_name=u.display_name,
                role=m.get("role", "r"),
            )
        )
    return out


@router.get("/{project_id}/members", response_model=list[MemberOut])
def list_members(
    project_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """List effective members. Visible to anyone with read access on
    the project."""
    is_admin = _is_admin(state, principal)
    with state.store.transaction() as sess:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        proj = _resolve_project_or_404(svc, project_id)
        if not svc.can_access(
            proj, principal.user_id, "read", is_admin=is_admin
        ):
            raise HTTPException(404, "project not found")
        return _enrich_members(sess, proj)


@router.post(
    "/{project_id}/members",
    response_model=list[MemberOut],
    status_code=201,
)
def add_member(
    project_id: str,
    body: AddMemberReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Invite an existing registered user to the project by email.
    Owner / admin only — strictly tighter than folder shares.
    """
    is_admin = _is_admin(state, principal)
    with state.store.transaction() as sess:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        proj = _resolve_project_or_404(svc, project_id)
        if not svc.can_access(
            proj, principal.user_id, "share", is_admin=is_admin
        ):
            raise HTTPException(404, "project not found")
        target = sess.execute(
            select(AuthUser).where(AuthUser.email == body.email)
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(404, f"no user with email {body.email!r}")
        try:
            proj = svc.add_or_update_member(
                project_id, user_id=target.user_id, role=body.role
            )
        except InvalidProjectRole as e:
            raise HTTPException(422, str(e))
        except ProjectMemberConflict as e:
            raise HTTPException(409, str(e))
        except ProjectMemberNotFound:
            raise HTTPException(404, "user not found")
        return _enrich_members(sess, proj)


# NOTE: there is no PATCH /{project_id}/members/{user_id} for role
# changes. Read-only is the only role; toggling someone's role to
# something else is not a supported operation. To "promote" a viewer
# to a different role you'd have to remove + re-add — and today there
# is no other role to promote them to. When Phase 6+ adds new roles
# (or owner transfer) the patch route lands then.


@router.delete(
    "/{project_id}/members/{user_id}",
    response_model=list[MemberOut],
)
def remove_member(
    project_id: str,
    user_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Drop a member. Owner / admin only. Removing the owner is
    rejected — owner transfer is not a Phase-0 feature."""
    is_admin = _is_admin(state, principal)
    with state.store.transaction() as sess:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        proj = _resolve_project_or_404(svc, project_id)
        if not svc.can_access(
            proj, principal.user_id, "share", is_admin=is_admin
        ):
            raise HTTPException(404, "project not found")
        try:
            proj = svc.remove_member(project_id, user_id)
        except ProjectMemberConflict as e:
            raise HTTPException(409, str(e))
        except ProjectMemberNotFound:
            raise HTTPException(404, "user not found")
        return _enrich_members(sess, proj)
