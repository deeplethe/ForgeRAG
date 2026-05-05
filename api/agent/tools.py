"""
Tool registry for the agentic chat.

Each ``ToolSpec`` has:

  * ``name`` — what the LLM emits in ``tool_use.name``
  * ``description`` — one-paragraph sentence sent in the LLM's
    tool catalogue. Optimise for "when should I call this".
  * ``params_schema`` — JSON-schema-shaped dict for arg validation
    + sent to the LLM as the tool's input schema.
  * ``handler`` — ``(params: dict, ctx: ToolContext) -> dict``. The
    return dict is what the LLM sees as the tool result; chunk-
    bearing tools should also call ``register_chunk`` to seed the
    citation pool.

This v1 ships three tools — the minimum to validate the dispatch
+ authz pipeline:

    search_bm25   — keyword / lexical (best for filename-y queries
                    and exact-term lookup)
    search_vector — semantic / dense embedding (best for paraphrase
                    + cross-lingual)
    read_chunk    — pull a single chunk's full content by chunk_id

graph_explore / read_tree / web_search / rerank / expand_query
land in subsequent commits.

Result shape contract for search-style tools:

    {
      "hits": [
        {"chunk_id", "doc_id", "doc_name", "page", "score", "snippet"}
      ]
    }

``snippet`` is capped at 200 chars — the agent calls ``read_chunk``
to pull full content. This bounds context-window usage when the
agent runs 3 searches in parallel and gets back 60 hits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .dispatch import (
    DispatchError,
    ToolContext,
    doc_passes_scope,
    register_chunk,
)

# Snippet length sent back to the LLM in search-style tool results.
# 200 chars ~50 tokens — enough for the LLM to triage relevance
# without bloating context. Full content lives in the citation pool
# and is fetched on demand via ``read_chunk``.
_SNIPPET_CHARS = 200

# Default top_k for search-style tools when the LLM doesn't override.
# Matches the historical fixed-pipeline values so quality is on par
# from day one.
_DEFAULT_TOP_K = 20

# Hard upper bound on top_k regardless of what the LLM asks for —
# defense against the agent over-fetching and blowing context.
_MAX_TOP_K = 50


# ---------------------------------------------------------------------------
# ToolSpec
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    params_schema: dict
    handler: Callable[[dict, ToolContext], dict]

    def to_anthropic_tool(self) -> dict:
        """Render in the shape the Anthropic tools API expects.

        Same schema works for Anthropic, OpenAI, and litellm's
        unified tool format — the wrapper at the agent loop just
        repacks the outer envelope per provider.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.params_schema,
        }


# ---------------------------------------------------------------------------
# Tool: search_bm25
# ---------------------------------------------------------------------------


def _handle_search_bm25(params: dict, ctx: ToolContext) -> dict:
    bm25 = getattr(ctx.state, "_bm25", None)
    if bm25 is None or len(bm25) == 0:
        # No index built — surface as an error so the agent can
        # try a different tool rather than silently no-op.
        return DispatchError(
            error="BM25 index not available", tool="search_bm25"
        ).to_result()

    query = params["query"]
    top_k = min(int(params.get("top_k", _DEFAULT_TOP_K)), _MAX_TOP_K)

    raw = bm25.search_chunks(
        query, top_k=top_k * 3, allowed_doc_ids=ctx.allowed_doc_ids
    )
    return _hydrate_hits(raw, top_k=top_k, source="bm25", ctx=ctx)


# ---------------------------------------------------------------------------
# Tool: search_vector
# ---------------------------------------------------------------------------


def _handle_search_vector(params: dict, ctx: ToolContext) -> dict:
    embedder = getattr(ctx.state, "embedder", None)
    vector = getattr(ctx.state, "vector", None)
    if embedder is None or vector is None:
        return DispatchError(
            error="vector index not available", tool="search_vector"
        ).to_result()

    query = params["query"]
    top_k = min(int(params.get("top_k", _DEFAULT_TOP_K)), _MAX_TOP_K)

    # Embed once. ``embed_texts`` always returns a list — keep the
    # contract symmetric with the BM25 path.
    try:
        q_vec = embedder.embed_texts([query])[0]
    except Exception as e:
        return DispatchError(
            error=f"embedding failed: {type(e).__name__}",
            tool="search_vector",
        ).to_result()

    # Path scope goes via the vector backend's filter — pgvector /
    # Chroma / Neo4j all understand the ``path_prefixes`` key. Empty
    # / None means no scope.
    vfilter: dict | None = None
    if ctx.path_filters:
        vfilter = {"path_prefixes": list(ctx.path_filters)}

    try:
        hits = vector.search(q_vec, top_k=top_k * 3, filter=vfilter)
    except Exception as e:
        return DispatchError(
            error=f"vector search failed: {type(e).__name__}",
            tool="search_vector",
        ).to_result()

    # Vector backends may return objects with ``.chunk_id`` /
    # ``.score`` / ``.doc_id`` rather than tuples — normalise.
    raw: list[tuple[str, float]] = []
    for h in hits:
        cid = getattr(h, "chunk_id", None) or (h.get("chunk_id") if isinstance(h, dict) else None)
        sc = getattr(h, "score", None)
        if sc is None and isinstance(h, dict):
            sc = h.get("score")
        if cid:
            raw.append((cid, float(sc or 0.0)))
    return _hydrate_hits(raw, top_k=top_k, source="vector", ctx=ctx)


# ---------------------------------------------------------------------------
# Tool: read_chunk
# ---------------------------------------------------------------------------


def _handle_read_chunk(params: dict, ctx: ToolContext) -> dict:
    chunk_id = params["chunk_id"]
    row = ctx.state.store.get_chunk(chunk_id)
    if row is None:
        return DispatchError(
            error=f"chunk not found: {chunk_id!r}", tool="read_chunk"
        ).to_result()
    if not doc_passes_scope(ctx, row.get("doc_id")):
        # Same 404-equivalent treatment as the per-resource read
        # routes — never confirm existence of out-of-scope chunks.
        return DispatchError(
            error=f"chunk not found: {chunk_id!r}", tool="read_chunk"
        ).to_result()

    register_chunk(
        ctx,
        chunk_id,
        doc_id=row["doc_id"],
        content=row.get("content") or "",
        page_start=row.get("page_start"),
        page_end=row.get("page_end"),
        path=row.get("path"),
        source="read_chunk",
    )
    return {
        "chunk_id": chunk_id,
        "doc_id": row["doc_id"],
        "path": row.get("path"),
        "page_start": row.get("page_start"),
        "page_end": row.get("page_end"),
        "content": row.get("content") or "",
    }


# ---------------------------------------------------------------------------
# Shared hydration — convert (chunk_id, score) into the LLM result
# shape AND seed the citation pool.
# ---------------------------------------------------------------------------


def _hydrate_hits(
    raw: list[tuple[str, float]],
    *,
    top_k: int,
    source: str,
    ctx: ToolContext,
) -> dict:
    """Bulk-hydrate a list of ``(chunk_id, score)`` tuples into the
    search-tool result shape, applying scope filtering as we go.

    Over-fetched candidates outside scope are dropped silently —
    the BM25 / vector backends already pre-filter (allowed_doc_ids
    or path_prefixes), so this is the second-line defence against
    a backend that doesn't honor scope or that returns a trashed
    doc.
    """
    if not raw:
        return {"hits": []}

    chunk_ids = [cid for cid, _ in raw]
    score_by_id = {cid: sc for cid, sc in raw}

    rows = ctx.state.store.get_chunks_by_ids(chunk_ids)
    # ``get_chunks_by_ids`` may return rows in a different order;
    # rebuild ranking from the original ``raw`` list.
    by_id = {r["chunk_id"]: r for r in rows}
    doc_ids = {r["doc_id"] for r in rows}
    docs = ctx.state.store.get_documents_by_ids(list(doc_ids)) if doc_ids else []
    doc_name_by_id = {d["doc_id"]: d.get("filename") or d.get("path") or "" for d in docs}

    hits: list[dict] = []
    for cid, _sc in raw:
        row = by_id.get(cid)
        if row is None:
            continue
        if not doc_passes_scope(ctx, row.get("doc_id")):
            continue
        content = row.get("content") or ""
        snippet = content[:_SNIPPET_CHARS]
        if len(content) > _SNIPPET_CHARS:
            snippet += "…"
        register_chunk(
            ctx,
            cid,
            doc_id=row["doc_id"],
            content=content,
            page_start=row.get("page_start"),
            page_end=row.get("page_end"),
            path=row.get("path"),
            score=score_by_id[cid],
            source=source,
        )
        hits.append(
            {
                "chunk_id": cid,
                "doc_id": row["doc_id"],
                "doc_name": doc_name_by_id.get(row["doc_id"], ""),
                "page": row.get("page_start"),
                "score": round(score_by_id[cid], 4),
                "snippet": snippet,
            }
        )
        if len(hits) >= top_k:
            break
    return {"hits": hits}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_BM25_SPEC = ToolSpec(
    name="search_bm25",
    description=(
        "Keyword / lexical search over the corpus. Best for filename-ish "
        "queries, exact-term lookup, and cases where you want chunks that "
        "contain the user's actual words. Returns the top hits as "
        "{chunk_id, doc_id, doc_name, page, score, snippet}. Call "
        "read_chunk(chunk_id) to fetch full content of a hit. Default "
        "top_k=20."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search string — usually pass the user's words verbatim.",
            },
            "top_k": {
                "type": "integer",
                "description": f"Number of hits to return. Default {_DEFAULT_TOP_K}, max {_MAX_TOP_K}.",
            },
        },
        "required": ["query"],
    },
    handler=_handle_search_bm25,
)


_VECTOR_SPEC = ToolSpec(
    name="search_vector",
    description=(
        "Semantic / dense-embedding search over the corpus. Best for "
        "paraphrased questions, cross-lingual lookup, and conceptual "
        "queries where the user's wording differs from the source. "
        "Returns the top hits as {chunk_id, doc_id, doc_name, page, "
        "score, snippet}. Call read_chunk(chunk_id) for full content. "
        "Default top_k=20."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search string — phrase it in natural language.",
            },
            "top_k": {
                "type": "integer",
                "description": f"Number of hits to return. Default {_DEFAULT_TOP_K}, max {_MAX_TOP_K}.",
            },
        },
        "required": ["query"],
    },
    handler=_handle_search_vector,
)


_READ_CHUNK_SPEC = ToolSpec(
    name="read_chunk",
    description=(
        "Fetch a single chunk's full content by chunk_id. Use this to "
        "expand a search snippet into the full passage when you need "
        "the exact text to ground an answer. Returns {chunk_id, doc_id, "
        "path, page_start, page_end, content}."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "chunk_id": {
                "type": "string",
                "description": "A chunk_id returned by search_bm25 / search_vector.",
            }
        },
        "required": ["chunk_id"],
    },
    handler=_handle_read_chunk,
)


TOOL_REGISTRY: dict[str, ToolSpec] = {
    _BM25_SPEC.name: _BM25_SPEC,
    _VECTOR_SPEC.name: _VECTOR_SPEC,
    _READ_CHUNK_SPEC.name: _READ_CHUNK_SPEC,
}
