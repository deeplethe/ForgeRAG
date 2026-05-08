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

    # ── Phase 2: agent-workspace bindings ──
    # When the conversation is bound to a project, ``project_id``
    # carries the binding through to project-aware tools (currently
    # only ``import_from_library``). None means "plain Q&A chat" —
    # project tools are filtered out by ``tools_for(ctx)``.
    #
    # Code execution itself no longer flows through this dispatch —
    # Hermes Agent runs inside the sandbox container and reaches
    # back to our domain tools via the MCP server route.
    project_id: str | None = None

    # Citation accumulator — chunk_id → chunk record. Populated by
    # every tool that returns chunks; the agent's final ``done()``
    # picks from this pool by chunk_id.
    citation_pool: dict[str, dict] = field(default_factory=dict)

    # Lightweight per-tool telemetry — name, latency, hit count —
    # surfaced through the SSE stream to the frontend trace.
    tool_calls_log: list[dict] = field(default_factory=list)

    # Per-turn cache for ``_list_owned_project_ids`` (Phase 2 audit
    # finding #3). Same user message can trigger several
    # import_from_library calls; without caching each one re-queries
    # the projects table. ToolContext is built once per turn and
    # discarded after — no staleness risk from caching across turns.
    # Default ``None`` = "not populated yet"; first lookup fills it.
    owned_project_ids_cache: tuple[str, ...] | None = None


def build_tool_context(
    state: AppState,
    principal: AuthenticatedPrincipal,
    *,
    requested_path_filters: list[str] | None = None,
    project_id: str | None = None,
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
    enforce_scope = auth_enabled and not is_admin
    if enforce_scope:
        # Run through authz.resolve_paths → user's spanning set or
        # validated subset. Raises UnauthorizedPath for explicit
        # asks the user can't see; the agent route catches that.
        # Note: a user with zero folder grants gets back ``[]`` —
        # we MUST preserve that as "no docs accessible", NOT collapse
        # it to None ("admin / no scope").
        raw_filters = state.authz.resolve_paths(
            principal.user_id, requested_path_filters
        )
    else:
        # Admin / auth-disabled: trust requested as-is (None means
        # no scope = the whole corpus).
        raw_filters = requested_path_filters

    if enforce_scope and not raw_filters:
        # User has zero folder access → no scope means no docs.
        # PathScopeResolver doesn't have a "deny everything" mode
        # (its empty list = "no scope"), so we short-circuit.
        accessible = build_accessible_set(
            state,
            principal.user_id,
            is_admin=False,
            auth_enabled=True,
        )
        return ToolContext(
            state=state,
            principal=principal,
            accessible=accessible,
            path_filters=[],
            allowed_doc_ids=set(),
            trashed_doc_ids=set(),
            project_id=project_id,
        )

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
        project_id=project_id,
    )


def tools_for(ctx: ToolContext) -> list:
    """Return the subset of TOOL_REGISTRY entries relevant for this
    context.

    Filtering is per-tool, not blanket — different tools have
    different prerequisites. Today only ``import_from_library``
    needs a bound project; everything else (search, KG, web, etc.)
    runs against the user's accessible-set regardless.

    Code-execution tools (bash / python / file ops) live INSIDE
    the agent's sandbox container (Hermes Agent owns them); they
    don't appear in this registry at all.

    Cleaner UX than surfacing "tool not available" errors mid-loop:
    the LLM never sees tools it can't use.
    """
    from .tools import TOOL_REGISTRY

    needs_project = {"import_from_library"}
    out = []
    for name, spec in TOOL_REGISTRY.items():
        if name in needs_project and ctx.project_id is None:
            continue
        out.append(spec)
    return out


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
    block_ids: list[str] | None = None,
    extra: dict | None = None,
) -> None:
    """Add a chunk to the citation pool. Idempotent — a chunk hit
    by multiple tools merges sources / takes max score.

    The agent's final ``done(citations=[…])`` runs ``enrich_citations``
    over the pool to compute pixel-precise highlights from
    ``block_ids`` → ``parsed_blocks.bbox``. Storing ``block_ids``
    here is what lets that enrichment run later without re-querying
    chunks.
    """
    existing = ctx.citation_pool.get(chunk_id)
    if existing is None:
        # Sequential ``c_N`` cite ID — assigned on first registration
        # and stable for the rest of the query so the LLM can refer
        # to it in its answer ("see [c_3]"). The frontend resolves
        # ``[c_N]`` markers to clickable citation chips.
        cite_id = f"c_{len(ctx.citation_pool) + 1}"
        ctx.citation_pool[chunk_id] = {
            "cite_id": cite_id,
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "content": content,
            "page_start": page_start,
            "page_end": page_end,
            "path": path,
            "score": score,
            "block_ids": list(block_ids or []),
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
    # Backfill block_ids if a later tool has them and the earlier
    # registration didn't (e.g. KG path registered first, search
    # registered second with the full chunk row).
    if block_ids and not existing.get("block_ids"):
        existing["block_ids"] = list(block_ids)
    if extra:
        for k, v in extra.items():
            existing.setdefault(k, v)


# ---------------------------------------------------------------------------
# Citation enrichment — chunk_id + block_ids → highlights + file_id
# ---------------------------------------------------------------------------
#
# Reuses the architecture the now-deleted ``retrieval/citations.py``
# established for the fixed-pipeline path. Each citation that comes
# back to the frontend needs:
#
#   * highlights: [{page_no, bbox}] — one rectangle per parsed block
#                 the chunk covers, used by the PDF viewer overlay
#                 to draw the precise highlight.
#   * file_id:    the PDF the viewer should render. For converted
#                 uploads (DOCX/PPTX/HTML/MD) that's ``pdf_file_id``;
#                 for native PDFs it's the original ``file_id``.
#                 Without this preference the viewer gets handed the
#                 .docx blob and pdfjs throws "Invalid PDF structure".
#   * source_file_id: original upload's file_id, for the "download
#                 source" button. None when source IS the PDF.
#   * source_format: "pdf" / "docx" / "pptx" / etc. Used by the
#                 frontend to label the download button.
#
# All resolved in TWO batched store calls (blocks + documents)
# regardless of how many citations are in the pool. O(1) round trips.


def enrich_citations(ctx: ToolContext) -> None:
    """Walk ``ctx.citation_pool`` and attach highlights + file_id
    fields IN PLACE. Idempotent — re-running on an already-enriched
    pool is a no-op. Safe to call once at the end of a query.

    Failures (missing blocks, missing doc rows, store errors) are
    swallowed per-citation: the citation keeps whatever fields it
    had and the frontend renders without highlights rather than
    blowing up the whole response. Logged at WARN.
    """
    pool = ctx.citation_pool
    if not pool:
        return

    # 1. Gather every block_id referenced by every chunk; batch-load
    #    once. Some citations may have empty block_ids (legacy /
    #    KG-only registrations) — they end up with empty highlights,
    #    same as a chunk whose blocks were trashed mid-session.
    wanted_block_ids: list[str] = []
    seen: set[str] = set()
    for entry in pool.values():
        for bid in entry.get("block_ids") or []:
            if bid not in seen:
                wanted_block_ids.append(bid)
                seen.add(bid)

    blocks_by_id: dict[str, dict] = {}
    if wanted_block_ids:
        try:
            for row in ctx.state.store.get_blocks_by_ids(wanted_block_ids):
                blocks_by_id[row["block_id"]] = row
        except Exception:
            log.exception("enrich_citations: get_blocks_by_ids failed")

    # 2. Gather every doc_id; resolve file_id / pdf_file_id / format
    #    in one batched lookup. ``get_documents_by_ids`` returns the
    #    same dict shape ``DocumentOut`` derives from.
    wanted_doc_ids = sorted({
        e.get("doc_id") for e in pool.values() if e.get("doc_id")
    })
    docs_by_id: dict[str, dict] = {}
    if wanted_doc_ids:
        try:
            for row in ctx.state.store.get_documents_by_ids(wanted_doc_ids):
                docs_by_id[row["doc_id"]] = row
        except Exception:
            log.exception("enrich_citations: get_documents_by_ids failed")

    # 3. Decorate each citation in place.
    for entry in pool.values():
        # Highlights from block bboxes — same shape the legacy
        # ``retrieval.citations.HighlightRect`` produced. Skip blocks
        # that didn't make it into the batch result (deleted /
        # out-of-scope).
        highlights: list[dict] = []
        for bid in entry.get("block_ids") or []:
            blk = blocks_by_id.get(bid)
            if blk is None:
                continue
            bbox = {
                "x0": blk.get("bbox_x0"),
                "y0": blk.get("bbox_y0"),
                "x1": blk.get("bbox_x1"),
                "y1": blk.get("bbox_y1"),
            }
            highlights.append({"page_no": blk.get("page_no"), "bbox": bbox})
        if highlights:
            entry["highlights"] = highlights
            # Also pin page_start to the first highlight's page if
            # the chunk row didn't carry one (defensive).
            if not entry.get("page_start"):
                entry["page_start"] = highlights[0].get("page_no")

        # file_id resolution: prefer the rendered PDF preview over
        # the raw upload. Track source_file_id only when the source
        # is genuinely different (converted uploads); native PDFs
        # leave it None so the UI doesn't show a redundant download.
        doc_row = docs_by_id.get(entry.get("doc_id") or "")
        if doc_row is None:
            continue
        pdf_fid = doc_row.get("pdf_file_id")
        orig_fid = doc_row.get("file_id")
        fmt = doc_row.get("format", "") or ""
        if pdf_fid:
            entry["file_id"] = pdf_fid
            entry["source_file_id"] = orig_fid
        else:
            entry["file_id"] = orig_fid
            entry["source_file_id"] = None
        entry["source_format"] = fmt


_CITE_ID_RE = __import__("re").compile(r"^c_\d+$")


def resolve_chunk_id(ctx: ToolContext, identifier: str) -> str:
    """Map a possibly-confused identifier to a real chunk_id.

    The hits we hand the LLM carry both ``chunk_id`` (the internal
    DB id like ``d_abc:1:cN``) and ``cite`` (a sequential display
    label like ``c_3``). Models — DeepSeek especially — sometimes
    pass the cite back as ``chunk_id`` to ``read_chunk`` /
    ``rerank``. Without resolution every read fails with
    "chunk not found: 'c_3'", the chain shows a column of red
    ``error`` chips, and the agent compounds the mistake by reading
    more cite-IDs.

    Strategy:
      * If the identifier looks like a real chunk_id (anything not
        matching ``^c_\\d+$``), pass it through unchanged.
      * If it's a ``c_N`` cite label, look it up in the pool and
        return the real chunk_id; fall back to the input on miss
        (the downstream lookup will produce the proper "not found"
        error).
    """
    if not identifier or not _CITE_ID_RE.match(identifier):
        return identifier
    for entry in ctx.citation_pool.values():
        if entry.get("cite_id") == identifier:
            real = entry.get("chunk_id")
            if real:
                return real
    return identifier


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
