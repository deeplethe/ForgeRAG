"""
FastAPI dependency providers.

Routes receive the AppState through these so the container lives
as a request-scoped dependency and tests can override it via
`app.dependency_overrides`.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from .auth import AuthenticatedPrincipal, UnauthorizedPath
from .state import AppState


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "app", None)
    if state is None:
        raise HTTPException(status_code=503, detail="app state not initialized")
    return state


# Type alias-ish: routes use `state: AppState = Depends(get_state)`
StateDep = Depends(get_state)


def get_principal(request: Request) -> AuthenticatedPrincipal:
    """The authenticated user for the current request.

    Set by ``AuthMiddleware`` on every request that survives the
    bypass list. When ``auth.enabled=false`` the middleware
    synthesises a ``via='auth_disabled'`` principal with role=admin
    so single-user deploys behave like before. Routes that need
    user identity declare this as a dependency rather than reading
    ``request.state`` directly.
    """
    p = getattr(request.state, "principal", None)
    if p is None:
        # Should be unreachable when middleware is mounted — but
        # routes that ride on top of authz must fail closed if the
        # middleware was somehow skipped.
        raise HTTPException(status_code=401, detail="authentication required")
    return p


def resolve_path_filters(
    state: AppState,
    principal: AuthenticatedPrincipal,
    requested: list[str] | None,
) -> list[str] | None:
    """Run the multi-user authz path resolver for a search-bearing
    request. Returns the concrete ``path_filters`` list to plumb
    into retrieval, or raises 403 on the first unauthorised path.

    Returns the raw list when auth is disabled (single-user fallback)
    so the existing single-admin behaviour is preserved bit-for-bit
    on deployments that haven't enabled auth.
    """
    if not state.cfg.auth.enabled:
        # Single-user / dev mode: no authz; trust the caller's list.
        return requested

    try:
        return state.authz.resolve_paths(principal.user_id, requested)
    except UnauthorizedPath as e:
        raise HTTPException(
            status_code=403,
            detail={"error": "unauthorized_path", "path": e.path},
        ) from e
