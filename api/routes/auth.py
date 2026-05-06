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

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
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
    """Login request body.

    Email is now the canonical login identifier. Both fields are
    optional at the Pydantic layer so legacy clients that still
    POST ``{"username": "alice", ...}`` keep working through the
    transition; new clients POST ``{"email": "alice@x.com", ...}``.
    The handler uses whichever is non-empty (preferring email when
    both are sent) and looks up the account by email column first,
    falling back to the legacy username column for bootstrap admins
    whose email is NULL.
    """

    email: str | None = None
    username: str | None = None  # back-compat
    password: str

    @property
    def identifier(self) -> str:
        return (self.email or self.username or "").strip()


class ChangePasswordReq(BaseModel):
    old_password: str = ""
    new_password: str = Field(..., min_length=4, max_length=200)


class PatchMeReq(BaseModel):
    """Self-edit fields a regular user can change without admin help.

    Currently just ``display_name``. Email is the login identifier
    (admin-only mutation), role is admin-only, status is the admin
    user-management surface. Password has its own dedicated endpoint
    (``/auth/change-password``) because it needs the old password.
    """

    display_name: str | None = Field(None, max_length=64)


class TokenCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    expires_days: int | None = Field(None, ge=1, le=3650)


class TokenPatchReq(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    expires_days: int | None = None  # 0/None to clear, positive to set


class MeOut(BaseModel):
    user_id: str
    username: str          # legacy — kept for back-compat with existing clients
    email: str | None = None
    display_name: str | None = None
    role: str
    via: str
    must_change_password: bool = False
    token_id: str | None = None
    token_name: str | None = None
    session_id: str | None = None
    # Truthy when the user has uploaded an avatar. The frontend
    # constructs the image URL from the user_id, not from this
    # field — we just need a flag so the avatar component knows
    # to attempt the fetch (vs. falling back to initials
    # immediately and saving a 404 round-trip).
    has_avatar: bool = False


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
# Registration
# ---------------------------------------------------------------------------


class RegisterReq(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=200)
    display_name: str | None = Field(None, max_length=64)
    # ``username`` is no longer required from the client. The
    # legacy column is auto-populated server-side from the email
    # local-part (deduped if a collision exists). New clients
    # never send this field; old clients that still POST it have
    # their value honoured for back-compat.
    username: str | None = Field(None, min_length=3, max_length=32)
    invitation_token: str | None = None


class RegisterResp(BaseModel):
    user_id: str
    username: str
    email: str
    display_name: str | None = None
    role: str
    status: str
    redeemed_folder_path: str | None = None


@router.post("/register", response_model=RegisterResp, status_code=201)
def register(body: RegisterReq, state: AppState = Depends(get_state)):
    """Self-registration. The first successful call against an empty
    auth_users table promotes the registrant to admin (regardless of
    registration_mode); subsequent calls follow the configured mode.
    A valid invitation token always produces an active account.

    The endpoint does NOT log the new user in — they call
    ``/auth/login`` afterwards. This keeps the login session shape
    identical between bootstrapped admins and self-registered users
    and lets the registration response stay agnostic of cookie flags.
    """
    if not state.cfg.auth.enabled:
        raise HTTPException(
            400, "auth is disabled — registration has no meaning here"
        )
    from ..auth.registration import (
        EmailTaken,
        InvalidEmail,
        InvalidUsername,
        InvitationProblem,
        RegistrationClosed,
        UsernameTaken,
        WeakPassword,
        register_user,
    )

    # Derive a synthetic username for the legacy ``username`` column
    # when the client didn't send one. We collide-check inside
    # ``register_user`` so retries with a counter suffix are
    # transparent. The ``display_name`` is what the UI surfaces
    # going forward; ``username`` is kept only for back-compat.
    derived_username = body.username
    if not derived_username:
        local = body.email.split("@", 1)[0].strip() if body.email else ""
        # Sanitise: replace non-alphanum with underscore so the
        # legacy username constraint (alphanum + underscore) holds.
        import re as _re
        local = _re.sub(r"[^A-Za-z0-9_]+", "_", local)[:24] or "user"
        derived_username = local

    with state.store.transaction() as sess:
        try:
            result = register_user(
                cfg=state.cfg,
                sess=sess,
                email=body.email,
                username=derived_username,
                password=body.password,
                display_name=body.display_name,
                invitation_token=body.invitation_token,
            )
        except (InvalidEmail, InvalidUsername, WeakPassword) as e:
            raise HTTPException(400, str(e))
        except (EmailTaken, UsernameTaken) as e:
            raise HTTPException(409, str(e))
        except RegistrationClosed as e:
            raise HTTPException(403, str(e))
        except InvitationProblem as e:
            raise HTTPException(400, str(e))
    return RegisterResp(
        user_id=result.user_id,
        username=result.username,
        email=result.email,
        display_name=result.display_name,
        role=result.role,
        status=result.status,
        redeemed_folder_path=result.redeemed_folder_path,
    )


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
        # Email is the canonical identifier going forward. Look up
        # by email column first (the case for every account created
        # via /auth/register since multi-user landed); fall back to
        # the legacy ``username`` column ONLY for the bootstrap
        # admin (email is NULL there) and any single-user-era rows
        # that haven't had an email backfilled.
        ident = body.identifier
        if not ident:
            raise HTTPException(400, "missing email")
        user = sess.execute(
            select(AuthUser).where(AuthUser.email == ident.lower())
        ).scalar_one_or_none()
        if user is None:
            user = sess.execute(
                select(AuthUser).where(AuthUser.username == ident)
            ).scalar_one_or_none()
        if user is None:
            raise HTTPException(401, "invalid credentials")
        if not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "invalid credentials")
        # Status gate (after password verification — we don't leak
        # which usernames exist by returning 403 to wrong-password
        # attempts on suspended accounts). pending_approval /
        # suspended / deleted users have a valid password but cannot
        # log in. Distinct error so the frontend can render a precise
        # message instead of the generic "invalid credentials".
        if user.status != "active" or not user.is_active:
            raise HTTPException(
                403,
                {
                    "pending_approval": "account pending admin approval",
                    "suspended": "account suspended",
                    "deleted": "account deleted",
                }.get(user.status, f"account status: {user.status}"),
            )
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


def _me_response(principal: AuthenticatedPrincipal, user: AuthUser | None) -> MeOut:
    return MeOut(
        user_id=principal.user_id,
        username=principal.username,
        # New canonical identity fields. The frontend's UserMenu /
        # Settings page now keys off email + display_name (with
        # ``display_name`` falling back to email-prefix or
        # username when not set). ``username`` stays in the
        # response so old clients still parse the body.
        email=user.email if user else None,
        display_name=(
            (user.display_name if user else None)
            or (user.email.split("@")[0] if user and user.email else None)
            or principal.username
        ),
        role=principal.role,
        via=principal.via,
        must_change_password=bool(user.must_change_password if user else False),
        token_id=principal.token_id,
        token_name=principal.token_name,
        session_id=principal.session_id,
        has_avatar=bool(user and user.avatar_path),
    )


@router.get("/me", response_model=MeOut)
def me(request: Request, state: AppState = Depends(get_state)):
    principal = _require_principal(request)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, principal.user_id)
        return _me_response(principal, user)


class UsageOut(BaseModel):
    """Per-user LLM token usage totals.

    ``message_count`` counts assistant turns only — i.e. how many
    answers the user has received. Aligns with the way the
    aggregator in ``api/auth/usage.py`` filters ``role='assistant'``
    so users → conversations → messages stays one number per turn.
    """

    user_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    message_count: int


@router.get("/me/usage", response_model=UsageOut)
def me_usage(request: Request, state: AppState = Depends(get_state)):
    """The caller's own token usage. Cheap read — single SUM/COUNT
    over the (small) messages table joined to conversations.
    """
    principal = _require_principal(request)
    from ..auth.usage import user_usage

    with state.store.transaction() as sess:
        totals = user_usage(sess, principal.user_id)
    return UsageOut(
        user_id=totals.user_id or principal.user_id,
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        total_tokens=totals.total_tokens,
        message_count=totals.message_count,
    )


@router.patch("/me", response_model=MeOut)
def patch_me(
    body: PatchMeReq,
    request: Request,
    state: AppState = Depends(get_state),
):
    """Self-edit. Only fields the user can change for themselves go
    here — role / status / email stay admin-only via /admin/users.

    ``display_name`` is normalised: empty / whitespace-only resets it
    back to NULL so the /me fallback chain (email-prefix → username)
    takes over again, rather than leaving a blank string in the DB.
    """
    principal = _require_principal(request)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, principal.user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if body.display_name is not None:
            cleaned = body.display_name.strip()
            user.display_name = cleaned or None
        return _me_response(principal, user)


# ---------------------------------------------------------------------------
# Avatar
# ---------------------------------------------------------------------------
#
# Three endpoints:
#
#   POST   /auth/me/avatar              upload (multipart, replaces existing)
#   DELETE /auth/me/avatar              remove (UI falls back to initials)
#   GET    /auth/users/{user_id}/avatar serve the file (any authed user)
#
# Storage: ``./storage/avatars/<user_id>.<ext>``. The path lives on
# the AuthUser row (``avatar_path``); the ``.ext`` portion lets the
# GET handler derive Content-Type without a separate column.
#
# Validation: only image/png, image/jpeg, image/webp are accepted.
# Body capped at 2 MB — avatars don't need more, and the cap stops
# someone from filling the disk via a malicious upload. We replace
# the file in-place on every upload (one row, one file) so users
# can't accumulate orphan blobs.
#
# Cache-busting: the GET handler returns ``Cache-Control: no-cache``
# so a fresh upload shows up immediately. (We could use an Etag
# keyed on the file mtime instead, but no-cache is simpler and the
# images are tiny enough that re-fetching costs nothing.)


_AVATAR_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
_AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2 MB


def _avatars_dir(state: AppState) -> "pathlib.Path":
    """Resolve ``./storage/avatars`` from the configured storage
    root. Falls back to ``./storage/avatars`` if the cfg shape
    differs — the dir is always created on first write."""
    import pathlib

    storage_cfg = getattr(state.cfg, "storage", None)
    local_cfg = getattr(storage_cfg, "local", None) if storage_cfg else None
    blobs_root = getattr(local_cfg, "root", None) if local_cfg else None
    if blobs_root:
        # Storage root is e.g. ``./storage/blobs``; sibling
        # ``./storage/avatars`` keeps avatars out of the blob
        # store's content-addressed sha256 namespace.
        base = pathlib.Path(blobs_root).resolve().parent
    else:
        base = pathlib.Path("./storage").resolve()
    out = base / "avatars"
    out.mkdir(parents=True, exist_ok=True)
    return out


@router.post("/me/avatar", response_model=MeOut)
async def upload_my_avatar(
    request: Request,
    file: UploadFile = File(...),
    state: AppState = Depends(get_state),
):
    """Upload (or replace) the caller's avatar image."""
    principal = _require_principal(request)

    mime = (file.content_type or "").lower()
    if mime not in _AVATAR_MIME_EXT:
        raise HTTPException(
            415,
            f"unsupported avatar format {mime!r}; use PNG, JPEG, or WebP",
        )
    ext = _AVATAR_MIME_EXT[mime]

    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(
            413,
            f"avatar too large ({len(data)} bytes); max is {_AVATAR_MAX_BYTES}",
        )

    avatars_dir = _avatars_dir(state)
    out_path = avatars_dir / f"{principal.user_id}{ext}"
    # Remove any pre-existing file with a different extension so
    # the row's ``avatar_path`` and the on-disk reality stay in
    # sync (e.g. a user uploading a JPG over a previous PNG).
    for stale_ext in _AVATAR_MIME_EXT.values():
        if stale_ext == ext:
            continue
        stale = avatars_dir / f"{principal.user_id}{stale_ext}"
        if stale.exists():
            try:
                stale.unlink()
            except Exception:
                log.exception("failed to clean stale avatar %s", stale)
    out_path.write_bytes(data)

    # Path stored relative-style so the row is portable across
    # storage roots — the GET handler joins it to ``avatars_dir``
    # at serve time.
    rel_name = f"{principal.user_id}{ext}"
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, principal.user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        user.avatar_path = rel_name
        return _me_response(principal, user)


@router.delete("/me/avatar", response_model=MeOut)
def delete_my_avatar(
    request: Request,
    state: AppState = Depends(get_state),
):
    """Remove the caller's avatar. Idempotent."""
    principal = _require_principal(request)
    avatars_dir = _avatars_dir(state)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, principal.user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        if user.avatar_path:
            f = avatars_dir / user.avatar_path
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    log.exception("failed to delete avatar %s", f)
            user.avatar_path = None
        return _me_response(principal, user)


@router.get("/users/{user_id}/avatar")
def get_user_avatar(
    user_id: str,
    request: Request,
    state: AppState = Depends(get_state),
):
    """Serve another user's avatar. Authenticated-only — any
    logged-in user can view any other user's avatar (they appear
    next to one another in shared documents / chats anyway).

    Returns 404 when the target user has no avatar set; that's
    the signal the frontend's <UserAvatar> uses to fall back to
    initials.
    """
    _require_principal(request)
    with state.store.transaction() as sess:
        user = sess.get(AuthUser, user_id)
        if user is None or not user.avatar_path:
            raise HTTPException(404, "no avatar")
        rel = user.avatar_path
    f = _avatars_dir(state) / rel
    if not f.exists():
        # DB says yes, disk says no — log and treat as missing
        # rather than 500. The next upload self-heals.
        log.warning("avatar row points to missing file: %s", f)
        raise HTTPException(404, "no avatar")
    # Derive media type from extension. We restrict uploads to
    # png/jpg/webp so this stays a fixed lookup.
    ext = f.suffix.lower()
    mt = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    return FileResponse(
        path=str(f),
        media_type=mt,
        # No-cache so a fresh upload shows up without query-string
        # cache-busting on every callsite.
        headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"},
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
