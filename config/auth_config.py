"""
Auth configuration.

ForgeRAG's auth is minimal and self-contained:

    auth:
      enabled: true           # false/absent = no auth (honour 127.0.0.1 binding)
      mode: db                # "db" = bearer tokens + password sessions stored
                              #         in Postgres (default)
                              # "forwarded" = trust an upstream OAuth proxy's
                              #         X-Forwarded-User header
      # ── mode=db knobs ──
      initial_password: opencraig  # applied at auto-bootstrap; first login
                                   # must change. Change via yaml only affects
                                   # fresh bootstraps, not existing admins.
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
    enabled: bool = False
    mode: Literal["db", "forwarded"] = "db"

    # --- mode=db ---
    initial_password: str = "opencraig"
    session_cookie_name: str = "opencraig_session"
    session_cookie_secure: bool = True
    password_change_revokes_other_sessions: bool = True

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
