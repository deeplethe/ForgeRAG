"""
Auth core — password + token + session primitives.

Public surface:

    hash_password / verify_password       — argon2id helpers
    generate_sk / hash_sk                 — token mint + sha256 hash
    AuthError                             — raise to produce 401 from middleware
    AuthenticatedPrincipal                — request.state.principal dataclass
    bootstrap_if_empty(cfg, store)        — auto-provision admin on startup
    AuthMiddleware                        — Starlette middleware enforcing auth
    generate_session_id                   — opaque session cookie value

Routes live in ``api/routes/auth.py``; this module is the plumbing.
"""

from .bootstrap import bootstrap_if_empty
from .middleware import AuthenticatedPrincipal, AuthError, AuthMiddleware
from .primitives import (
    generate_session_id,
    generate_sk,
    hash_password,
    hash_sk,
    verify_password,
)

__all__ = [
    "AuthError",
    "AuthMiddleware",
    "AuthenticatedPrincipal",
    "bootstrap_if_empty",
    "generate_session_id",
    "generate_sk",
    "hash_password",
    "hash_sk",
    "verify_password",
]
