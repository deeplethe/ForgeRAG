"""
Auth HTTP surface.

    POST   /api/v1/auth/login               username + password → session cookie
    POST   /api/v1/auth/logout              revoke current session
    POST   /api/v1/auth/change-password     old + new → rotate + (optional) revoke others
    GET    /api/v1/auth/me                  current principal details

    # Token management (admin only when multi-user lands; single-user all allowed)
    GET    /api/v1/auth/tokens              list SKs (metadata only)
    POST   /api/v1/auth/tokens              create SK → returns raw ONCE
    DELETE /api/v1/auth/tokens/{id}         revoke
    PATCH  /api/v1/auth/tokens/{id}         rename / change expires

    # Session management
    GET    /api/v1/auth/sessions            list active sessions for this user
    DELETE /api/v1/auth/sessions/{id}       revoke one (can't revoke current from here)
    POST   /api/v1/auth/sessions/sign-out-others   revoke all sessions except current
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from persistence.models import AuthSession, AuthToken, AuthUser

from ..auth import (
    AuthenticatedPrincipal,
    generate_session_id,
    generate_sk,
    hash_password,
    hash_sk,
    verify_password,
)
from ..auth.primitives import hash_prefix, needs_rehash
from ..deps import get_state
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _require_principal(request: Request) -> AuthenticatedPrincipal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(401, "not authenticated")
    return principal


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LoginReq(BaseModel):
    username: str
    password: str


class ChangePasswordReq(BaseModel):
    old_password: str = ""
    new_password: str = Field(..., min_length=4, max_length=200)


class TokenCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    expires_days: int | None = Field(None, ge=1, le=3650)


class TokenPatchReq(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    expires_days: int | None = None  # 0/None to clear, positive to set


class MeOut(BaseModel):
    user_id: str
    username: str
    role: str
    via: str
    must_change_password: bool = False
    token_id: str | None = None
    token_name: str | None = None
    session_id: str | None = None


class TokenOut(BaseModel):
    token_id: str
    name: str
    role: str
    hash_prefix: str
    created_at: Any = None
    last_used_at: Any = None
    expires_at: Any = None
    revoked_at: Any = None


class SessionOut(BaseModel):
    session_id: str
    created_at: Any = None
    last_seen_at: Any = None
    ip: str | None = None
    user_agent: str | None = None
    is_current: bool = False


# ---------------------------------------------------------------------------
# Login / logout / change password / me
# ---------------------------------------------------------------------------


@router.post("/login")
def login(
    body: LoginReq,
    request: Request,
    response: Response,
    state: AppState = Depends(get_state),
):
    if not state.cfg.auth.enabled:
        # Auth is off — every request already runs as the synthetic
        # "local" admin principal (see AuthMiddleware). Returning 200
        # here lets the frontend's standard login flow succeed without
        # special-casing this server mode; no session cookie is set
        # because the middleware grants access unconditionally.
        return {
            "user_id": "local",
            "username": "local",
            "role": "admin",
            "must_change_password": False,
        }

    with state.store.transaction() as sess:
        user = sess.execute(select(AuthUser).where(AuthUser.username == body.username)).scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(401, "invalid credentials")
        if not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "invalid credentials")
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(body.password)

        user.last_login_at = datetime.utcnow()

        sid = generate_session_id()
        sess.add(
            AuthSession(
                session_id=sid,
                user_id=user.user_id,
                ip=(request.client.host if request.client else None),
                user_agent=request.headers.get("user-agent", "")[:500],
            )
        )

        out = {
            "user_id": user.user_id,
            "username": user.username,
            "role": user.role,
            "must_change_password": user.must_change_password,
        }

    cfg = state.cfg.auth
    response.set_cookie(
        key=cfg.session_cookie_name,
        value=sid,
        httponly=True,
        secure=cfg.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return out


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    state: AppState = Depends(get_state),
):
    principal = getattr(request.state, "principal", None)
    if principal and principal.session_id:
        with state.store.transaction() as sess:
            row = sess.get(AuthSession, principal.session_id)
            if row is not None and row.revoked_at is None:
                row.revoked_at = datetime.utcnow()
    response.delete_cookie(key=state.cfg.auth.session_cookie_name, path="/")
    return {"status": "logged_out"}


@router.post("/change-password")
def change_password(
    body: ChangePasswordReq,
    request: Request,
    state: AppState = Depends(get_state),
):
    principal = _require_principal(request)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, principal.user_id)
        if user is None:
            raise HTTPException(404, "user not found")

        # Allow old-password skip on the forced first-change flow OR when
        # the admin explicitly provided no old password and must_change
        # is true. Otherwise old_password is required.
        if not user.must_change_password:
            if not body.old_password:
                raise HTTPException(400, "old_password required")
            if not verify_password(body.old_password, user.password_hash):
                raise HTTPException(401, "old password incorrect")

        user.password_hash = hash_password(body.new_password)
        user.must_change_password = False
        user.password_changed_at = datetime.utcnow()

        # Revoke all OTHER sessions (keep the current one so the user
        # doesn't get bounced back to login right after changing).
        if state.cfg.auth.password_change_revokes_other_sessions:
            keep_sid = principal.session_id
            active = (
                sess.execute(
                    select(AuthSession).where(
                        AuthSession.user_id == user.user_id,
                        AuthSession.revoked_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            for s in active:
                if s.session_id != keep_sid:
                    s.revoked_at = datetime.utcnow()

    return {"status": "password_changed"}


@router.get("/me", response_model=MeOut)
def me(request: Request, state: AppState = Depends(get_state)):
    principal = _require_principal(request)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, principal.user_id)
        return MeOut(
            user_id=principal.user_id,
            username=principal.username,
            role=principal.role,
            via=principal.via,
            must_change_password=bool(user.must_change_password if user else False),
            token_id=principal.token_id,
            token_name=principal.token_name,
            session_id=principal.session_id,
        )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@router.get("/tokens", response_model=list[TokenOut])
def list_tokens(request: Request, state: AppState = Depends(get_state)):
    principal = _require_principal(request)
    with state.store.transaction() as sess:
        rows = (
            sess.execute(
                select(AuthToken).where(AuthToken.user_id == principal.user_id).order_by(AuthToken.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [
            TokenOut(
                token_id=r.token_id,
                name=r.name,
                role=r.role,
                hash_prefix=r.hash_prefix,
                created_at=r.created_at,
                last_used_at=r.last_used_at,
                expires_at=r.expires_at,
                revoked_at=r.revoked_at,
            )
            for r in rows
        ]


@router.post("/tokens")
def create_token(
    body: TokenCreateReq,
    request: Request,
    state: AppState = Depends(get_state),
):
    principal = _require_principal(request)
    from datetime import timedelta

    raw = generate_sk()
    expires_at = None
    if body.expires_days:
        expires_at = datetime.utcnow() + timedelta(days=body.expires_days)

    with state.store.transaction() as sess:
        sess.add(
            AuthToken(
                token_id=_new_id(),
                user_id=principal.user_id,
                name=body.name,
                token_hash=hash_sk(raw),
                hash_prefix=hash_prefix(raw),
                role=principal.role,
                expires_at=expires_at,
            )
        )
    # The raw token appears in the response exactly once and is never
    # retrievable again — the caller MUST save it now.
    return {
        "name": body.name,
        "token": raw,
        "hash_prefix": hash_prefix(raw),
        "expires_at": expires_at,
        "warning": "Save this token — it cannot be retrieved again.",
    }


@router.delete("/tokens/{token_id}")
def revoke_token(
    token_id: str,
    request: Request,
    state: AppState = Depends(get_state),
):
    principal = _require_principal(request)
    with state.store.transaction() as sess:
        row = sess.get(AuthToken, token_id)
        if row is None or row.user_id != principal.user_id:
            raise HTTPException(404, "token not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.utcnow()
    return {"revoked": token_id}


@router.patch("/tokens/{token_id}", response_model=TokenOut)
def patch_token(
    token_id: str,
    body: TokenPatchReq,
    request: Request,
    state: AppState = Depends(get_state),
):
    from datetime import timedelta

    principal = _require_principal(request)
    with state.store.transaction() as sess:
        row = sess.get(AuthToken, token_id)
        if row is None or row.user_id != principal.user_id:
            raise HTTPException(404, "token not found")
        if row.revoked_at is not None:
            raise HTTPException(409, "token already revoked")
        if body.name is not None:
            row.name = body.name
        if body.expires_days is not None:
            row.expires_at = datetime.utcnow() + timedelta(days=body.expires_days) if body.expires_days > 0 else None
        return TokenOut(
            token_id=row.token_id,
            name=row.name,
            role=row.role,
            hash_prefix=row.hash_prefix,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(request: Request, state: AppState = Depends(get_state)):
    principal = _require_principal(request)
    with state.store.transaction() as sess:
        rows = (
            sess.execute(
                select(AuthSession)
                .where(
                    AuthSession.user_id == principal.user_id,
                    AuthSession.revoked_at.is_(None),
                )
                .order_by(AuthSession.last_seen_at.desc())
            )
            .scalars()
            .all()
        )
        out = []
        for s in rows:
            out.append(
                SessionOut(
                    session_id=s.session_id,
                    created_at=s.created_at,
                    last_seen_at=s.last_seen_at,
                    ip=s.ip,
                    user_agent=s.user_agent,
                    is_current=(s.session_id == principal.session_id),
                )
            )
        return out


@router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: str,
    request: Request,
    state: AppState = Depends(get_state),
):
    principal = _require_principal(request)
    if session_id == principal.session_id:
        raise HTTPException(400, "use POST /auth/logout to end your own session")
    with state.store.transaction() as sess:
        row = sess.get(AuthSession, session_id)
        if row is None or row.user_id != principal.user_id:
            raise HTTPException(404, "session not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.utcnow()
    return {"revoked": session_id}


@router.post("/sessions/sign-out-others")
def sign_out_others(request: Request, state: AppState = Depends(get_state)):
    principal = _require_principal(request)
    with state.store.transaction() as sess:
        active = (
            sess.execute(
                select(AuthSession).where(
                    AuthSession.user_id == principal.user_id,
                    AuthSession.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        count = 0
        for s in active:
            if s.session_id != principal.session_id:
                s.revoked_at = datetime.utcnow()
                count += 1
    return {"revoked": count}


# ---------------------------------------------------------------------------
# Folder invitations — recipient-side
# ---------------------------------------------------------------------------
#
# The owner mints a token via POST /folders/{id}/invitations; the
# recipient hits these routes with the raw token in the URL. They
# bypass the middleware (the token is the auth) — the endpoints
# themselves rate-limit by token uniqueness + expiry + one-shot
# consumption.


class InvitationPreviewOut(BaseModel):
    invitation_id: str
    folder_id: str
    folder_path: str
    target_email: str
    role: str
    expires_at: str
    inviter_username: str
    inviter_email: str | None = None


@router.get(
    "/invitations/{token}/preview", response_model=InvitationPreviewOut
)
def preview_invitation(token: str, state: AppState = Depends(get_state)):
    """Pre-redemption summary so the recipient confirms what they're
    accepting before clicking through. Bypasses auth — the token
    in the URL identifies the invitation."""
    from persistence.invitation_service import (
        FolderInvitationService,
        InvitationAlreadyConsumed,
        InvitationExpired,
        InvitationFolderMissing,
        InvitationNotFound,
    )

    with state.store.transaction() as sess:
        try:
            preview = FolderInvitationService(sess).preview(token)
        except InvitationNotFound:
            raise HTTPException(404, "invalid or revoked invitation")
        except InvitationExpired:
            raise HTTPException(410, "invitation expired")
        except InvitationAlreadyConsumed:
            raise HTTPException(409, "invitation already used")
        except InvitationFolderMissing:
            raise HTTPException(410, "folder no longer exists")
    return InvitationPreviewOut(
        invitation_id=preview.invitation_id,
        folder_id=preview.folder_id,
        folder_path=preview.folder_path,
        target_email=preview.target_email,
        role=preview.role,
        expires_at=preview.expires_at.isoformat(),
        inviter_username=preview.inviter_username,
        inviter_email=preview.inviter_email,
    )


# Consume runs as a normal authenticated route (the recipient must
# have a session). It's exposed at the same prefix for symmetry —
# but since it's after registration / login, the middleware bypass
# above doesn't apply (the user has a session by now). To consume
# without the middleware caring, we still bypass and require an
# explicit ``user_id`` in the body — the registration / login route
# in S4 will be the canonical caller and pass its just-minted user.
class ConsumeInvitationRequest(BaseModel):
    token: str
    user_id: str


@router.post("/invitations/consume", response_model=InvitationPreviewOut)
def consume_invitation(
    body: ConsumeInvitationRequest, state: AppState = Depends(get_state)
):
    """Apply an invitation's grant to the redeemer. Called by the
    registration / login flow once the recipient has an
    authenticated identity. The token is one-shot — replaying the
    same token against this endpoint is rejected.
    """
    from persistence.invitation_service import (
        FolderInvitationService,
        InvitationAlreadyConsumed,
        InvitationError,
        InvitationExpired,
        InvitationFolderMissing,
        InvitationNotFound,
    )

    with state.store.transaction() as sess:
        try:
            preview = FolderInvitationService(sess).consume(
                token=body.token, redeemer_user_id=body.user_id
            )
        except InvitationNotFound:
            raise HTTPException(404, "invalid or revoked invitation")
        except InvitationExpired:
            raise HTTPException(410, "invitation expired")
        except InvitationAlreadyConsumed:
            raise HTTPException(409, "invitation already used")
        except InvitationFolderMissing:
            raise HTTPException(410, "folder no longer exists")
        except InvitationError as e:
            raise HTTPException(400, str(e))
    return InvitationPreviewOut(
        invitation_id=preview.invitation_id,
        folder_id=preview.folder_id,
        folder_path=preview.folder_path,
        target_email=preview.target_email,
        role=preview.role,
        expires_at=preview.expires_at.isoformat(),
        inviter_username=preview.inviter_username,
        inviter_email=preview.inviter_email,
    )
