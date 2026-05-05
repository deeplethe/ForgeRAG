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


# ---------------------------------------------------------------------------
# Per-resource access helpers
#
# Used by single-resource GET routes (chunk/block/document/file) to
# enforce folder authz on the way out. The pattern is consistent:
#
#   1. Look up the resource row.
#   2. Resolve its folder_id (via doc_id where applicable).
#   3. Check ``can(folder_id, "read")`` for the principal.
#   4. On miss / no-access, raise 404 — same status as a missing
#      resource. Never confirm a stranger's id exists.
#
# Auth-disabled deployments skip the authz check; the synthetic
# ``local`` admin is trusted with everything (preserving legacy
# single-user behaviour).
# ---------------------------------------------------------------------------


def require_doc_access(
    state: AppState,
    principal: AuthenticatedPrincipal,
    doc_id: str,
    action: str = "read",
) -> dict:
    """Fetch a document row and verify the principal has the requested
    action on its containing folder. Raises 404 on missing or
    unauthorised. Returns the row (saves the caller a second lookup).
    """
    row = state.store.get_document(doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    if state.cfg.auth.enabled and principal.via != "auth_disabled":
        folder_id = row.get("folder_id")
        if not folder_id or not state.authz.can(
            principal.user_id, folder_id, action
        ):
            raise HTTPException(404, "document not found")
    return row


def require_chunk_access(
    state: AppState,
    principal: AuthenticatedPrincipal,
    chunk_id: str,
    action: str = "read",
) -> dict:
    """Fetch a chunk row and verify access via its parent document's
    folder. Same 404-on-no-access pattern as ``require_doc_access``.
    """
    row = state.store.get_chunk(chunk_id)
    if row is None:
        raise HTTPException(404, "chunk not found")
    if state.cfg.auth.enabled and principal.via != "auth_disabled":
        doc_id = row.get("doc_id")
        if not doc_id:
            raise HTTPException(404, "chunk not found")
        try:
            require_doc_access(state, principal, doc_id, action)
        except HTTPException:
            raise HTTPException(404, "chunk not found")
    return row


def require_block_access(
    state: AppState,
    principal: AuthenticatedPrincipal,
    block_id: str,
    action: str = "read",
) -> dict:
    """Fetch a block row and verify access via its parent document's
    folder. ``store.get_block`` returns the row dict (or None);
    block_id semantics are identical to chunk_id."""
    row = state.store.get_block(block_id)
    if row is None:
        raise HTTPException(404, "block not found")
    if state.cfg.auth.enabled and principal.via != "auth_disabled":
        doc_id = row.get("doc_id")
        if not doc_id:
            raise HTTPException(404, "block not found")
        try:
            require_doc_access(state, principal, doc_id, action)
        except HTTPException:
            raise HTTPException(404, "block not found")
    return row


def require_file_access(
    state: AppState,
    principal: AuthenticatedPrincipal,
    file_id: str,
    action: str = "read",
) -> dict:
    """Fetch a file row and verify access. Files can be referenced by
    multiple ``Document`` rows (one per parse_version, or per upload
    target); the user passes if they have access to the folder of
    ANY referencing doc, OR they're the original uploader (the
    audit-only ``files.owner_user_id``). Files with no referencing
    doc and no recorded uploader are admin-only.
    """
    row = state.store.get_file(file_id)
    if row is None:
        raise HTTPException(404, "file not found")
    if not (state.cfg.auth.enabled and principal.via != "auth_disabled"):
        return row

    # Original uploader path.
    if row.get("owner_user_id") == principal.user_id:
        return row

    # Any referencing doc lives in an accessible folder?
    docs = state.store.find_documents_by_file_id(file_id)
    for d in docs or []:
        folder_id = d.get("folder_id")
        if folder_id and state.authz.can(principal.user_id, folder_id, action):
            return row

    raise HTTPException(404, "file not found")
