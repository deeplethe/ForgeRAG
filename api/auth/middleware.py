"""
ASGI auth middleware.

Enforces auth on every request unless:

  * ``auth.enabled=false``          — skip entirely (dev / single-user local)
  * path matches ``auth.public_paths`` prefix (health, etc.)
  * path is an auth route itself   (/api/v1/auth/login, /logout)
  * method is OPTIONS               (CORS preflight)

Auth resolution order (first match wins):

  1. Bearer ``Authorization: Bearer <sk>`` — DB lookup on sha256(sk).
     Updates ``auth_tokens.last_used_at``.
  2. Session cookie ``cfg.session_cookie_name`` — DB lookup on session_id.
     Updates ``auth_sessions.last_seen_at``.
  3. ``mode=forwarded`` — trust ``cfg.forwarded_user_header`` and map to
     ``auth_users.username`` (auto-provision a user row if missing).

On success, ``request.state.principal`` is set with user_id / role /
token_id / session_id so routes can read it.

On failure, 401 with ``WWW-Authenticate: Bearer`` header.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from datetime import datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .primitives import hash_sk

log = logging.getLogger(__name__)


def _peer_in_trusted_cidrs(peer_host: str | None, cidrs: list[str]) -> bool:
    """True iff *peer_host* falls inside one of the configured CIDRs."""
    if not peer_host:
        return False
    try:
        addr = ipaddress.ip_address(peer_host)
    except ValueError:
        return False
    for raw in cidrs:
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            log.warning("auth.forwarded_trusted_proxy_cidrs: invalid CIDR %r — ignoring", raw)
            continue
        if addr in net:
            return True
    return False


class AuthError(Exception):
    """Raised from routes / helpers when the current principal can't
    perform a requested action. Middleware catches and returns 401/403."""

    def __init__(self, message: str, *, status: int = 401):
        self.status = status
        super().__init__(message)


@dataclass
class AuthenticatedPrincipal:
    """What the middleware attaches to ``request.state.principal`` after
    a successful authenticate. Routes that want to check role do
    ``if request.state.principal.role != "admin": raise AuthError(...)``.
    """

    user_id: str
    username: str
    role: str
    via: str  # "token" / "session" / "forwarded"
    token_id: str | None = None  # populated when via="token"
    session_id: str | None = None  # populated when via="session"
    token_name: str | None = None  # "alice-laptop", "ci", etc.
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Instantiated once in ``create_app`` with a reference to ``AppState``
    so it can reach the relational store. The store is expected to have
    a ``.session()`` context manager yielding an SQLAlchemy Session.
    """

    def __init__(self, app, *, state_getter):
        super().__init__(app)
        self._state_getter = state_getter

    async def dispatch(self, request: Request, call_next):
        app_state = self._state_getter(request)
        if app_state is None:
            # Startup hasn't finished — let the request through; downstream
            # handlers will produce a 503 from the app-state dependency.
            return await call_next(request)

        cfg = app_state.cfg.auth

        if not cfg.enabled:
            # Auth disabled: synthesize a local-admin principal so routes
            # that require one (``/auth/me``, ingestion endpoints, etc.)
            # behave as "already logged in" instead of returning 401.
            # The frontend then never enters the login flow on a server
            # that has no auth configured.
            request.state.principal = AuthenticatedPrincipal(
                user_id="local",
                username="local",
                role="admin",
                via="auth_disabled",
            )
            return await call_next(request)

        # ── Bypass list ──
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if path.startswith("/api/v1/auth/login") or path.startswith("/api/v1/auth/logout"):
            return await call_next(request)
        # Self-registration runs unauthenticated by definition — the
        # caller doesn't have an account yet. Rate-limiting is the
        # only defence we'd want here; deferred to a future
        # middleware that watches POST volume per source IP.
        if path == "/api/v1/auth/register":
            return await call_next(request)
        # Folder-invitation preview / consume routes run before the
        # recipient is logged in (they're following the link from
        # their email). The token in the URL is the auth.
        if path.startswith("/api/v1/auth/invitations/"):
            return await call_next(request)
        # First-boot setup wizard runs unauthenticated by design — by
        # definition the operator hasn't created an account yet, and
        # the LLM keys it sets gate every other endpoint that needs
        # real LLM compute. The endpoints themselves enforce a
        # ``configured=False`` precondition so they self-disable
        # once the deploy is past first boot.
        if path.startswith("/api/v1/setup/"):
            return await call_next(request)
        for prefix in cfg.public_paths or []:
            if path.startswith(prefix):
                return await call_next(request)

        # ── Try to authenticate ──
        try:
            principal = await _authenticate(request, cfg, app_state.store)
        except AuthError as e:
            return _unauth_response(str(e), status=e.status)
        if principal is None:
            return _unauth_response("authentication required")

        request.state.principal = principal

        # OTel: tag the ambient span with the end-user ID so downstream
        # observability shows per-user activity.
        try:
            from opentelemetry import trace as _otel_trace

            span = _otel_trace.get_current_span()
            if span and span.get_span_context().is_valid:
                span.set_attribute("enduser.id", principal.user_id)
                span.set_attribute("enduser.role", principal.role)
                if principal.token_name:
                    span.set_attribute("forgerag.token_name", principal.token_name)
                if principal.session_id:
                    span.set_attribute("forgerag.session_id", principal.session_id)
        except Exception:
            pass

        return await call_next(request)


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------


async def _authenticate(request: Request, cfg, store) -> AuthenticatedPrincipal | None:
    from sqlalchemy import and_, select, update

    from persistence.models import AuthSession, AuthToken, AuthUser

    # (1) Bearer token
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        raw = auth_header[7:].strip()
        if not raw:
            raise AuthError("empty bearer")
        tid_hash = hash_sk(raw)
        with store.transaction() as sess:
            row = sess.execute(
                select(AuthToken, AuthUser)
                .join(AuthUser, AuthUser.user_id == AuthToken.user_id)
                .where(AuthToken.token_hash == tid_hash)
            ).first()
            if not row:
                raise AuthError("invalid token")
            tok, user = row
            if tok.revoked_at is not None:
                raise AuthError("token revoked")
            if tok.expires_at is not None and tok.expires_at < datetime.utcnow():
                raise AuthError("token expired")
            if not user.is_active:
                raise AuthError("user disabled")
            # Atomic touch: only update last_used_at if the token is still
            # valid right now. A concurrent revoke that lands between our
            # SELECT and the UPDATE will have set revoked_at != NULL, so
            # this UPDATE will match 0 rows and we'll reject the request.
            now = datetime.utcnow()
            res = sess.execute(
                update(AuthToken)
                .where(
                    and_(
                        AuthToken.token_hash == tid_hash,
                        AuthToken.revoked_at.is_(None),
                    )
                )
                .values(last_used_at=now)
            )
            if (res.rowcount or 0) == 0:
                raise AuthError("token revoked")
            sess.commit()
            return AuthenticatedPrincipal(
                user_id=user.user_id,
                username=user.username,
                role=tok.role or user.role,
                via="token",
                token_id=tok.token_id,
                token_name=tok.name,
            )

    # (2) Session cookie
    if cfg.mode == "db":
        sid = request.cookies.get(cfg.session_cookie_name)
        if sid:
            with store.transaction() as sess:
                row = sess.execute(
                    select(AuthSession, AuthUser)
                    .join(AuthUser, AuthUser.user_id == AuthSession.user_id)
                    .where(AuthSession.session_id == sid)
                ).first()
                if not row:
                    raise AuthError("session not found")
                s, user = row
                if s.revoked_at is not None:
                    raise AuthError("session revoked")
                if not user.is_active:
                    raise AuthError("user disabled")
                s.last_seen_at = datetime.utcnow()
                sess.commit()
                return AuthenticatedPrincipal(
                    user_id=user.user_id,
                    username=user.username,
                    role=user.role,
                    via="session",
                    session_id=s.session_id,
                )

    # (3) Forwarded header (behind OAuth proxy)
    if cfg.mode == "forwarded":
        fwd = request.headers.get(cfg.forwarded_user_header)
        if fwd:
            # SSRF / spoofing defence: only trust the header when the
            # immediate peer is in the operator's trusted-proxy list.
            # Empty list = no source restriction (only safe with strict
            # network isolation; documented in auth_config.py).
            trusted = list(getattr(cfg, "forwarded_trusted_proxy_cidrs", []) or [])
            if trusted:
                peer = request.client.host if request.client else None
                if not _peer_in_trusted_cidrs(peer, trusted):
                    log.warning(
                        "rejecting %s header from untrusted peer %s (set auth.forwarded_trusted_proxy_cidrs to permit)",
                        cfg.forwarded_user_header,
                        peer,
                    )
                    raise AuthError("forwarded auth from untrusted source", status=403)
            # Upsert a user row on first sight — the proxy is the auth
            # source of truth; we just need a local identity for scoping
            # tokens and sessions.
            with store.transaction() as sess:
                row = sess.execute(select(AuthUser).where(AuthUser.username == fwd)).scalar_one_or_none()
                if row is None:
                    import uuid

                    default_role = getattr(cfg, "forwarded_default_role", "viewer")
                    row = AuthUser(
                        user_id=uuid.uuid4().hex[:16],
                        username=fwd,
                        password_hash="",  # never logs in with password
                        role=default_role,  # operator promotes to admin via Tokens UI
                        is_active=True,
                        must_change_password=False,
                    )
                    sess.add(row)
                    sess.commit()
                if not row.is_active:
                    raise AuthError("user disabled")
                return AuthenticatedPrincipal(
                    user_id=row.user_id,
                    username=row.username,
                    role=row.role,
                    via="forwarded",
                )

    return None


def _unauth_response(msg: str, *, status: int = 401) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse(
        {"detail": {"error": "unauthenticated", "message": msg}},
        status_code=status,
        headers=headers,
    )
