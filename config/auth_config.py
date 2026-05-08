"""
Auth configuration.

OpenCraig's auth is minimal and self-contained:

    auth:
      enabled: true           # default; set false to disable auth (honour 127.0.0.1 binding)
      mode: db                # "db" = bearer tokens + password sessions stored
                              #         in Postgres (default)
                              # "forwarded" = trust an upstream OAuth proxy's
                              #         X-Forwarded-User header
      # ── mode=db knobs ──
      initial_password: ""         # empty (default) = NO pre-provisioned admin;
                                   # the first user to /auth/register is auto-
                                   # promoted to admin. Set this to a string
                                   # only if you want a pre-baked ``admin`` row
                                   # at first boot — affects fresh bootstraps
                                   # only, never existing admins.
      session_cookie_name: opencraig_session
      session_cookie_secure: true  # set false only for http://localhost dev
      password_change_revokes_other_sessions: true

      # ── mode=forwarded knobs ──
      forwarded_user_header: X-Forwarded-User

Tokens + sessions are in DB tables (``auth_users``, ``auth_tokens``,
``auth_sessions``); there's no yaml-token list. See
``docs/auth.md`` for the full operator guide.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AuthConfig(BaseModel):
    enabled: bool = True
    mode: Literal["db", "forwarded"] = "db"

    # --- mode=db ---
    # Empty default = no pre-provisioned admin row at boot. The
    # FIRST account created via /auth/register is then auto-
    # promoted to admin (registration.py's first-user override).
    # Set this to a non-empty string ONLY if you want a baked-in
    # ``admin`` user with this password — useful for some CI /
    # docker-compose flows but unnecessary for the typical
    # self-host where the operator registers themselves first.
    initial_password: str = ""
    session_cookie_name: str = "opencraig_session"
    session_cookie_secure: bool = True
    password_change_revokes_other_sessions: bool = True

    # --- multi-user registration ---
    # ``open``       — anyone with a valid email can register and use
    #                  the system immediately. Suitable for trusted
    #                  internal deployments only.
    # ``approval``   — registration creates a ``pending_approval`` row;
    #                  an admin must explicitly approve before the
    #                  user can log in. Default for self-host.
    # ``invite_only``— only invitations from existing members can lead
    #                  to a new account. Plain registration is rejected
    #                  unless accompanied by a valid invitation token.
    # Special case (always-on): when ``auth_users`` has no active admin
    # row, the FIRST successful registration is auto-promoted to admin
    # and active, regardless of this mode. Covers the empty-deploy
    # bootstrap path without requiring ``initial_password``.
    registration_mode: Literal["open", "approval", "invite_only"] = "approval"
    # Soft expiry on folder invitation links. Recipients must register /
    # accept within this window or the link 410s.
    invitation_ttl_days: int = 7

    # --- mode=forwarded ---
    forwarded_user_header: str = "X-Forwarded-User"
    # SSRF / spoofing defence for ``mode=forwarded``: only accept the
    # forwarded header when the immediate peer (request.client.host)
    # matches one of these CIDRs. Empty = ACCEPT FROM ANYONE — only safe
    # if the server is bound to 127.0.0.1 with the proxy on the same host
    # AND no other process can reach the bind address. Recommended for
    # any non-toy deployment: list the proxy's egress IP(s) explicitly.
    forwarded_trusted_proxy_cidrs: list[str] = Field(
        default_factory=list,
        description=(
            "CIDR(s) the upstream OAuth proxy connects from. The "
            "X-Forwarded-User header is honoured only when the request's "
            "immediate client.host matches one of these. Empty = accept "
            "from any source (only safe with strict network isolation)."
        ),
    )
    # New auto-provisioned forwarded users start with this role rather than
    # 'admin' — the operator can promote later via the Tokens UI.
    forwarded_default_role: Literal["admin", "viewer"] = "viewer"

    # Paths that bypass auth even when enabled (health probes, static assets).
    # Matched as path prefix. /api/v1/auth/login obviously also bypasses
    # (hardcoded) so users can actually log in.
    public_paths: list[str] = Field(
        default_factory=lambda: ["/api/v1/health"],
        description="URL path prefixes that bypass auth (health probes etc.)",
    )
