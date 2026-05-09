"""
MCP server — exposes OpenCraig's domain tools to in-container agents.

Claude Agent SDK (running inside the per-user sandbox container) reaches
back to this server over HTTP/MCP for tools that need backend state:
search the Library, walk the KG, list artifacts, import documents,
etc. Code-execution tools (bash / edit / grep / etc.) live INSIDE
the SDK itself; they don't appear here.

Wave 2.2 (this commit) is the SCAFFOLD only:

  * FastMCP instance configured for stateless HTTP transport
  * one diagnostic ``ping`` tool (zero-arg, returns server name +
    the authenticated user_id from the request context — proves
    the principal-scoping plumbing works once Wave 2.3 wires auth)
  * a ``ContextVar`` carrying the authenticated principal into
    tool handlers (the model future tools will use to enforce
    multi-user authz: each tool builds a ToolContext from the
    var's value, then runs the same dispatch path the SSE agent
    route uses today)
  * a ``mount_mcp(app)`` helper that mounts the streamable HTTP
    transport under ``/api/v1/mcp`` on the existing FastAPI app

Wave 2.3 will:

  * add an ASGI auth middleware that reads the bearer token /
    session cookie and sets the ContextVar before delegating to
    the FastMCP app
  * register the actual domain tools (``search_chunks``,
    ``search_kg``, ``search_bm25``, ``get_doc_chunks``,
    ``graph_explore``, ``retrieve_for_qa``, ``search_artifacts``,
    ``get_artifact``, ``import_from_library``) by re-using
    ``api/agent/tools.py`` handlers behind MCP-typed wrappers

Why a ContextVar (not a request-scoped Depends): MCP tool handlers
are registered as decorated coroutines on the FastMCP instance —
they run inside the SDK's protocol-dispatch coroutine, so the
FastAPI dependency-injection chain doesn't extend into them. A
ContextVar set by the auth middleware before each protocol call
DOES propagate into those coroutines (asyncio context inheritance).

Why deliberately leave auth for 2.3: the diagnostic ``ping`` tool
doesn't need a principal to prove the wiring works, and bolting
on auth without a tool that exercises it makes the auth code path
test-only — better to land them together so the regression suite
covers the real flow end-to-end.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..auth import AuthenticatedPrincipal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-request principal (Wave 2.3 wires the setter; Wave 2.2 only reads)
# ---------------------------------------------------------------------------


_current_principal: ContextVar[AuthenticatedPrincipal | None] = ContextVar(
    "mcp_current_principal", default=None,
)


def get_mcp_principal() -> AuthenticatedPrincipal | None:
    """Return the authenticated principal for the current MCP request,
    or None if the var hasn't been set (Wave 2.2 always returns None;
    Wave 2.3 wires the auth middleware that sets it)."""
    return _current_principal.get()


def set_mcp_principal(principal: AuthenticatedPrincipal | None):
    """Set the principal for the current async context. Returns the
    token so callers can ``reset_mcp_principal(token)`` after the
    request finishes — important for nested / concurrent calls.

    Public so tests + the Wave 2.3 auth middleware can both use it.
    """
    return _current_principal.set(principal)


def reset_mcp_principal(token) -> None:
    _current_principal.reset(token)


# ---------------------------------------------------------------------------
# Server instance + tools
# ---------------------------------------------------------------------------


# ``stateless_http=True``: each MCP HTTP request is independent. We
# don't need persistent server-side sessions because every tool call
# carries its own auth + arguments. Stateless mode is also what
# scales horizontally without sticky-session load balancing.
#
# ``streamable_http_path="/"``: when we ``app.mount("/api/v1/mcp",
# mcp.streamable_http_app())``, the protocol endpoint lands cleanly
# at ``/api/v1/mcp`` (not the awkward ``/api/v1/mcp/mcp``). The
# the SDK container is configured to point at exactly that URL.
#
# ``json_response=True``: clients that don't speak SSE get plain
# JSON responses. the SDK's MCP client will negotiate the right
# transport via Accept headers; this just sets the default.
mcp_server = FastMCP(
    "opencraig",
    instructions=(
        "OpenCraig domain tools: Library search (vector + BM25), "
        "knowledge-graph traversal, document retrieval, artifact "
        "lookup, and Library-to-Workspace document import. The "
        "agent's bash / edit / grep / file-ops tools are local to "
        "the sandbox container; this server is for everything that "
        "needs backend state."
    ),
    stateless_http=True,
    streamable_http_path="/",
    json_response=True,
)


@mcp_server.tool()
def ping() -> dict[str, Any]:
    """Diagnostic: confirm the MCP server is reachable and report
    which user the connection is authenticated as.

    Returns:
        ``server``: human-readable server name
        ``status``: ``"ok"`` (always, if you can call this)
        ``authenticated``: True iff a principal has been resolved
            for this request (Wave 2.3 wires this; Wave 2.2 always
            returns False)
        ``user_id``: the authenticated user's ID, or ``null``
        ``username``: convenience for log-correlation
    """
    principal = get_mcp_principal()
    out: dict[str, Any] = {
        "server": "opencraig",
        "status": "ok",
    }
    if principal is None:
        out["user_id"] = None
        out["username"] = None
        out["authenticated"] = False
    else:
        out["user_id"] = principal.user_id
        out["username"] = principal.username
        out["authenticated"] = True
    return out


# ---------------------------------------------------------------------------
# FastAPI mount + auth bridge (Wave 2.3)
# ---------------------------------------------------------------------------


class _MCPPrincipalBridge:
    """ASGI middleware that bridges FastAPI's ``request.state.principal``
    (set by the app-level ``AuthMiddleware``) into the MCP-side
    ``ContextVar``, so tool handlers see the right user.

    AuthMiddleware runs ahead of the mounted MCP app on every HTTP
    request. It sets:
      * ``request.state.principal`` → AuthenticatedPrincipal on success
      * 401 response on auth failure (we never get called)
      * synthetic ``via="auth_disabled"`` principal when auth is off

    All we do here is read that principal off the scope's State object
    and copy it into the ContextVar before letting the FastMCP app
    run. Reset the var in ``finally`` so concurrent requests can't
    leak each other's principals.

    If, despite AuthMiddleware running first, we somehow see a request
    with no principal on state, the safest move is to fall through
    with the var unset; the MCP wrappers in ``mcp_tools.py`` reject
    unauthenticated calls explicitly. This matches AuthMiddleware's
    own auth-disabled fallback semantics — it sets a synthetic
    principal rather than failing requests.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Lifespan / WebSocket etc. — no principal context needed.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        principal = _principal_from_scope(scope)
        if principal is None:
            # AuthMiddleware would normally set it; missing means
            # something upstream skipped auth. We refuse rather than
            # silently letting an unauthenticated call through.
            await _send_json_error(
                send,
                status=401,
                payload={
                    "error": {
                        "code": "unauthenticated",
                        "message": (
                            "MCP server requires an authenticated "
                            "principal — include a session cookie or "
                            "Bearer token (the in-container agent gets "
                            "one via the OPENCRAIG_API_TOKEN env var)."
                        ),
                    }
                },
            )
            return

        token = set_mcp_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_mcp_principal(token)


def _principal_from_scope(scope) -> AuthenticatedPrincipal | None:
    """Read the AuthMiddleware-set principal off the ASGI scope's
    ``state`` slot. Returns None if absent.

    Starlette stashes ``request.state`` at ``scope["state"]``; FastAPI
    middlewares mutating ``request.state.X`` show up there too. We
    read either dict-style (``state["principal"]``) or attr-style
    (``state.principal``) since Starlette's ``State`` supports both.
    """
    state = scope.get("state")
    if state is None:
        return None
    # Starlette ``State`` is attribute-style; some test paths use a
    # plain dict. Try attr first, then dict.
    principal = getattr(state, "principal", None)
    if principal is None and isinstance(state, dict):
        principal = state.get("principal")
    return principal


async def _send_json_error(send, *, status: int, payload: dict) -> None:
    import json

    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def mount_mcp(app) -> None:
    """Mount the MCP HTTP server under ``/api/v1/mcp`` on the given
    FastAPI app, with the principal-bridge ASGI middleware in front.

    Auth flow:
        request → app-level ``AuthMiddleware`` → sets
        ``request.state.principal`` → mounted MCP sub-app →
        ``_MCPPrincipalBridge`` reads it → sets ContextVar → FastMCP
        protocol handler → tool wrappers in ``mcp_tools.py`` read
        the ContextVar → dispatch into ``api/agent/dispatch.dispatch``
        with a fresh ToolContext built for that user.

    Importing ``mcp_tools`` here triggers the ``@mcp_server.tool()``
    decorators that register every domain tool. Importing inside
    the function (not at module top) avoids a circular import:
    ``mcp_tools`` imports from ``mcp_server``.
    """
    from . import mcp_tools  # registers tool decorators

    # Bind the AppState lookup so tool wrappers can build ToolContext.
    mcp_tools._set_app_state_getter(lambda: getattr(app.state, "app", None))

    inner = mcp_server.streamable_http_app()
    wrapped = _MCPPrincipalBridge(inner)
    app.mount("/api/v1/mcp", wrapped)
