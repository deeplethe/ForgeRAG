"""
Admin-only user management.

Endpoints (all under ``/api/v1/admin``, all require an authenticated
principal with ``role='admin'``):

    GET    /admin/users                  list users (filterable by status / role)
    GET    /admin/users/{id}             single user detail
    POST   /admin/users/{id}/approve     pending_approval -> active
    POST   /admin/users/{id}/suspend     active -> suspended
    POST   /admin/users/{id}/reactivate  suspended -> active
    PATCH  /admin/users/{id}             change role / display_name
    DELETE /admin/users/{id}             hard-delete the user

Hard-delete cascades / nulls per the schema's ON DELETE rules:
  * conversations  → CASCADE     (privacy hygiene)
  * documents.owner_user_id  → SET NULL (audit-only column)
  * files.owner_user_id  → SET NULL (audit-only column)
  * audit_log.actor_id  → string column, untouched (audit trail
                          survives the user row going away)
  * folders.shared_with  → manually swept here (JSON column, no FK)

The current admin cannot delete or suspend themselves — refusing
self-suspension prevents the "I locked myself out" foot-gun.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from persistence.models import AuthUser

from ..auth import AuthenticatedPrincipal
from ..deps import get_state
from ..state import AppState

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AdminUserOut(BaseModel):
    user_id: str
    username: str
    email: str | None = None
    display_name: str | None = None
    role: str
    status: str
    is_active: bool
    created_at: Any = None
    last_login_at: Any = None


class UpdateUserReq(BaseModel):
    role: Literal["admin", "user"] | None = None
    display_name: str | None = Field(None, max_length=64)


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def _require_admin(request: Request, state: AppState) -> AuthenticatedPrincipal:
    """Every endpoint here is admin-only. When auth is disabled the
    middleware synthesises a local-admin principal, so the gate is
    a passthrough — single-user dev setups continue to see all the
    admin endpoints. When auth is enabled, role must be 'admin'."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(401, "not authenticated")
    if state.cfg.auth.enabled and principal.role != "admin":
        raise HTTPException(403, "admin role required")
    return principal


def _user_to_out(user: AuthUser) -> AdminUserOut:
    return AdminUserOut(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    request: Request,
    status: str | None = Query(
        None,
        description="Filter by status (active|pending_approval|suspended|deleted)",
    ),
    role: str | None = Query(None, description="Filter by role (admin|user)"),
    state: AppState = Depends(get_state),
):
    _require_admin(request, state)
    with state.store.transaction() as sess:
        stmt = select(AuthUser).order_by(AuthUser.created_at.desc())
        if status:
            stmt = stmt.where(AuthUser.status == status)
        if role:
            stmt = stmt.where(AuthUser.role == role)
        rows = list(sess.execute(stmt).scalars())
    return [_user_to_out(u) for u in rows]


@router.get("/users/{user_id}", response_model=AdminUserOut)
def get_user(
    user_id: str,
    request: Request,
    state: AppState = Depends(get_state),
):
    _require_admin(request, state)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        return _user_to_out(user)


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


@router.post("/users/{user_id}/approve", response_model=AdminUserOut)
def approve_user(
    user_id: str,
    request: Request,
    state: AppState = Depends(get_state),
):
    """pending_approval → active. Idempotent on already-active users."""
    _require_admin(request, state)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if user.status == "active":
            return _user_to_out(user)
        if user.status not in ("pending_approval",):
            raise HTTPException(
                409, f"can only approve from pending_approval, got {user.status!r}"
            )
        user.status = "active"
        user.is_active = True
        return _user_to_out(user)


@router.post("/users/{user_id}/suspend", response_model=AdminUserOut)
def suspend_user(
    user_id: str,
    request: Request,
    state: AppState = Depends(get_state),
):
    """active → suspended. Login is rejected with a precise 403 from
    the login endpoint while suspended."""
    principal = _require_admin(request, state)
    if principal.user_id == user_id:
        raise HTTPException(400, "cannot suspend yourself")
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if user.status == "suspended":
            return _user_to_out(user)
        user.status = "suspended"
        user.is_active = False
        # Revoke all sessions so the user is kicked off any active
        # browsers immediately (they'd otherwise keep their cookie
        # working until next request).
        from persistence.models import AuthSession

        active_sessions = sess.execute(
            select(AuthSession).where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
        ).scalars().all()
        now = datetime.utcnow()
        for s in active_sessions:
            s.revoked_at = now
        return _user_to_out(user)


@router.post("/users/{user_id}/reactivate", response_model=AdminUserOut)
def reactivate_user(
    user_id: str,
    request: Request,
    state: AppState = Depends(get_state),
):
    """suspended → active. Pending users use /approve instead."""
    _require_admin(request, state)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if user.status == "active":
            return _user_to_out(user)
        if user.status != "suspended":
            raise HTTPException(
                409,
                f"can only reactivate from suspended, got {user.status!r}",
            )
        user.status = "active"
        user.is_active = True
        return _user_to_out(user)


# ---------------------------------------------------------------------------
# Update / delete
# ---------------------------------------------------------------------------


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(
    user_id: str,
    body: UpdateUserReq,
    request: Request,
    state: AppState = Depends(get_state),
):
    """Change role / display_name. Demoting yourself from admin
    requires another admin to do it — prevents the last-admin lock-out."""
    principal = _require_admin(request, state)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if body.role is not None:
            if (
                principal.user_id == user_id
                and user.role == "admin"
                and body.role != "admin"
            ):
                raise HTTPException(
                    400, "cannot demote yourself; ask another admin"
                )
            user.role = body.role
        if body.display_name is not None:
            user.display_name = body.display_name
        return _user_to_out(user)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: str,
    request: Request,
    state: AppState = Depends(get_state),
):
    """Hard-delete the user.

    Cascade behaviour comes from the schema:

      * conversations.user_id   → CASCADE (rows deleted)
      * documents.owner_user_id → SET NULL (audit-only column)
      * files.owner_user_id     → SET NULL
      * auth_tokens             → CASCADE (issued tokens revoked
                                  by row deletion)
      * auth_sessions           → CASCADE (cookies invalidated)
      * audit_log.actor_id      → untouched string; trail survives

    Plus a folder-shared_with sweep we have to do in Python because
    ``shared_with`` is a JSON column with no FK: every folder that
    listed this user gets their entry removed. Done in the same
    transaction as the user delete.
    """
    principal = _require_admin(request, state)
    if principal.user_id == user_id:
        raise HTTPException(400, "cannot delete yourself")
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, user_id)
        if user is None:
            return None  # idempotent

        # Strip the user from every folder.shared_with. Folder count
        # is small (hundreds in practice); no GIN index needed.
        from sqlalchemy import select as _select

        from persistence.models import Folder

        folders = sess.execute(_select(Folder)).scalars().all()
        for f in folders:
            sw = f.shared_with or []
            if any(
                isinstance(e, dict) and e.get("user_id") == user_id
                for e in sw
            ):
                f.shared_with = [
                    e for e in sw
                    if not (isinstance(e, dict) and e.get("user_id") == user_id)
                ]

        sess.delete(user)
    return None
