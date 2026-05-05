"""
Central tool dispatcher.

One function — ``dispatch(tool_name, params, ctx)`` — is the only path
a tool gets called through. Every retrieval invariant lives here so
each individual ToolSpec handler can stay focused on its primitive's
mechanics.

The contract:

    1. Resolve ToolSpec from the registry. Unknown name → DispatchError
       returned to the LLM (NOT a Python exception — the agent can read
       the error and try a different tool).

    2. Validate params against the spec's JSON schema. Bad params →
       DispatchError.

    3. Run the handler with the typed ToolContext. The handler:
         - reads ``ctx.allowed_doc_ids`` to scope its retrieval
         - returns a serialisable dict (NOT raw retrieval objects —
           anything heading to the LLM must be JSON)
         - registers any chunks it returns into ``ctx.citation_pool``
           via the helpers below

    4. Catch handler exceptions. Convert to DispatchError with a
       short message — the agent shouldn't see Python tracebacks; it
       should see "tool failed, try another approach". Log the
       exception server-side so we can debug.

The single ToolContext is built once at the start of each agent
loop run via ``build_tool_context`` — the path-scope resolution
+ accessible-set construction is heavy (one SQL query each) so it
gets cached and reused across every tool call in the same query.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..auth import (
    AccessibleSet,
    AuthenticatedPrincipal,
    build_accessible_set,
)
from ..state import AppState

log = logging.getLogger(__name__)


@dataclass
class DispatchError:
    """Returned to the LLM as a tool result when a call can't proceed.

    The agent loop renders this into a tool_result message so the
    LLM can recover (e.g. "unknown tool" → pick a different tool;
    "invalid params" → re-emit with fixed shape).
    """

    error: str
    tool: str | None = None

    def to_result(self) -> dict:
        out = {"error": self.error}
        if self.tool:
            out["tool"] = self.tool
        return out


@dataclass
class ToolContext:
    """Per-query state passed to every tool invocation.

    Construct once at agent-loop entry; mutated in place as tools
    contribute citations to the pool.
    """

    state: AppState
    principal: AuthenticatedPrincipal

    # Authz / scope — resolved once at entry.
    accessible: AccessibleSet
    """User's accessible doc set. Used by KG visibility filter +
    chunk-existence checks. ``is_admin=True`` short-circuits."""

    path_filters: list[str] | None
    """Resolved scope as a flat prefix list. ``None`` = no scope
    (admin / auth disabled / user with `/` grant)."""

    allowed_doc_ids: set[str] | None
    """Scope as a doc_id whitelist for path-unaware backends
    (BM25 / NetworkX KG). ``None`` = no scope."""

    trashed_doc_ids: set[str] = field(default_factory=set)
    """Always exclude these — doc may be in user's accessible set
    but be currently trashed."""

    # Citation accumulator — chunk_id → chunk record. Populated by
    # every tool that returns chunks; the agent's final ``done()``
    # picks from this pool by chunk_id.
    citation_pool: dict[str, dict] = field(default_factory=dict)

    # Lightweight per-tool telemetry — name, latency, hit count —
    # surfaced through the SSE stream to the frontend trace.
    tool_calls_log: list[dict] = field(default_factory=list)


def build_tool_context(
    state: AppState,
    principal: AuthenticatedPrincipal,
    *,
    requested_path_filters: list[str] | None = None,
) -> ToolContext:
    """Resolve scope + accessible set + trashed set for one query.

    The same logic used to live at the entry of
    ``retrieval.pipeline.RetrievalPipeline.run`` (PathScopeResolver
    + KG accessible set). We reuse it here so the agent inherits the
    multi-user invariants for free.

    ``requested_path_filters`` is the optional user-supplied scope
    narrowing (same shape that ``/api/v1/query`` accepted). When
    ``None``, falls back to the user's full accessible set.
    """
    auth_enabled = state.cfg.auth.enabled
    is_admin = principal.role == "admin"

    # Path scope resolution. Reuse PathScopeResolver so we don't
    # diverge from the (now-deprecated) fixed pipeline's contract
    # while we transition.
    from retrieval.components.path_scope import PathScopeResolver

    resolver = PathScopeResolver(state.store)
    raw_filters: list[str] | None = None
    if auth_enabled and not is_admin:
        # Run through authz.resolve_paths → user's spanning set or
        # validated subset. Raises UnauthorizedPath for explicit
        # asks the user can't see; the agent route catches that.
        raw_filters = state.authz.resolve_paths(
            principal.user_id, requested_path_filters
        )
    else:
        # Admin / auth-disabled: trust requested as-is (None means
        # no scope = the whole corpus).
        raw_filters = requested_path_filters

    scope = resolver.run({"_path_filters": raw_filters} if raw_filters else None)

    accessible = build_accessible_set(
        state,
        principal.user_id,
        is_admin=is_admin,
        auth_enabled=auth_enabled and principal.via != "auth_disabled",
    )

    return ToolContext(
        state=state,
        principal=principal,
        accessible=accessible,
        path_filters=scope.path_prefixes or None,
        allowed_doc_ids=scope.allowed_doc_ids,
        trashed_doc_ids=scope.trashed_doc_ids,
    )


def dispatch(tool_name: str, params: dict, ctx: ToolContext) -> dict:
    """Run a single tool call. Returns a dict the agent loop will
    serialise as a ``tool_result`` content block.

    Failures (unknown tool, bad params, handler exceptions) come
    back as ``{"error": "...", "tool": "..."}`` rather than raising
    — the agent should be able to read the error and recover.
    """
    from .tools import TOOL_REGISTRY

    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        return DispatchError(
            error=f"unknown tool: {tool_name!r}", tool=tool_name
        ).to_result()

    # Param validation — minimal, schema-driven. Hand-rolled instead
    # of pulling jsonschema as a hard dep; we only need required-key +
    # type checks for now.
    err = _validate_params(spec.params_schema, params)
    if err is not None:
        return DispatchError(error=err, tool=tool_name).to_result()

    t0 = time.time()
    try:
        result = spec.handler(params, ctx)
    except Exception as e:
        # Agent shouldn't see tracebacks; server logs them.
        log.exception("tool %s raised", tool_name)
        result = DispatchError(
            error=f"tool {tool_name!r} failed: {type(e).__name__}",
            tool=tool_name,
        ).to_result()
    latency_ms = int((time.time() - t0) * 1000)

    ctx.tool_calls_log.append(
        {
            "tool": tool_name,
            "latency_ms": latency_ms,
            "params": params,
            # Result shape varies — store a small summary instead of
            # the full payload (full goes to the agent loop separately).
            "result_summary": _summarise_result(result),
        }
    )
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_params(schema: dict, params: dict) -> str | None:
    """Lightweight required-key + type check. Returns an error
    string or None on success.

    We deliberately avoid the full ``jsonschema`` dep — every
    real param this v1 takes is one of: string, int, list[str].
    Adding a schema lib is overkill until we need oneOf / anyOf.
    """
    if not isinstance(params, dict):
        return f"params must be a dict, got {type(params).__name__}"

    props = schema.get("properties", {})
    required = set(schema.get("required", []))

    for key in required:
        if key not in params:
            return f"missing required param: {key!r}"

    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for key, value in params.items():
        if key not in props:
            return f"unknown param: {key!r}"
        expected = props[key].get("type")
        if expected is None:
            continue
        py_type = type_map.get(expected)
        if py_type is not None and not isinstance(value, py_type):
            return (
                f"param {key!r} must be {expected}, got "
                f"{type(value).__name__}"
            )
    return None


def _summarise_result(result: Any) -> dict:
    """Compact result summary for the per-tool trace event.

    The full result goes back to the LLM as tool_result content;
    this is just for the SSE trace + server-side logging — keep it
    tiny.
    """
    if isinstance(result, dict):
        if "error" in result:
            return {"error": result["error"]}
        if "hits" in result and isinstance(result["hits"], list):
            return {"hit_count": len(result["hits"])}
        if "chunk_id" in result:
            return {"chunk_id": result["chunk_id"]}
    return {}


# ---------------------------------------------------------------------------
# Citation pool helpers (used by tool handlers)
# ---------------------------------------------------------------------------


def register_chunk(
    ctx: ToolContext,
    chunk_id: str,
    *,
    doc_id: str,
    content: str,
    page_start: int | None = None,
    page_end: int | None = None,
    path: str | None = None,
    score: float | None = None,
    source: str = "",
    extra: dict | None = None,
) -> None:
    """Add a chunk to the citation pool. Idempotent — a chunk hit
    by multiple tools merges sources / takes max score.

    The agent's final ``done(citations=[id...])`` picks from this
    pool to attach citations to the answer; the chunk → bbox
    rendering downstream is unchanged.
    """
    existing = ctx.citation_pool.get(chunk_id)
    if existing is None:
        ctx.citation_pool[chunk_id] = {
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "content": content,
            "page_start": page_start,
            "page_end": page_end,
            "path": path,
            "score": score,
            "sources": {source} if source else set(),
            **(extra or {}),
        }
        return
    # Merge — chunk seen via another tool earlier. Update score to
    # max so rerank-style ordering picks up the strongest signal.
    if score is not None:
        prev = existing.get("score")
        if prev is None or score > prev:
            existing["score"] = score
    if source:
        existing.setdefault("sources", set()).add(source)
    if extra:
        for k, v in extra.items():
            existing.setdefault(k, v)


def doc_passes_scope(ctx: ToolContext, doc_id: str | None) -> bool:
    """True iff a doc is in scope: not trashed AND inside the
    caller's accessible set (or no-scope = pass everything).

    Used by every tool that hydrates retrieval results — scope is
    centralised here so a future change (e.g. additional excluded
    state) lands in one place.
    """
    if not doc_id:
        return False
    if doc_id in ctx.trashed_doc_ids:
        return False
    if ctx.allowed_doc_ids is None:
        return True
    return doc_id in ctx.allowed_doc_ids
