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

Current tools:

    search_bm25    — keyword / lexical
    search_vector  — semantic / dense embedding
    read_chunk     — pull full content of one chunk by chunk_id
    graph_explore  — knowledge graph entity + relation lookup,
                     visibility-filtered

read_tree / web_search / rerank land in subsequent commits.

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

from ..auth import filter_entity, filter_relation
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
        """Render in the shape the Anthropic native tools API expects.

        Used when calling Anthropic SDK directly. The agent loop uses
        ``to_openai_tool`` because litellm normalises every provider
        through the OpenAI tool format internally.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.params_schema,
        }

    def to_openai_tool(self) -> dict:
        """Render in the OpenAI / litellm unified tool format.

        litellm translates from this shape to whichever native
        format the underlying provider needs (Anthropic tool_use,
        OpenAI function-call, etc.) — so the agent loop only knows
        one envelope.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
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


# ---------------------------------------------------------------------------
# Tool: graph_explore
# ---------------------------------------------------------------------------
#
# Knowledge graph access via name search. Different from chunk
# retrieval: returns LLM-synthesised entity / relation descriptions
# instead of raw passages. Useful for "who is X" / "how does X relate
# to Y" queries where the answer benefits from cross-document
# synthesis.
#
# Visibility (S5.3) is **stricter** here than on the API surface —
# partial AND hidden both drop. Same reasoning as
# retrieval/kg_path.py::_scope_kg_context: LLM context can't render
# the "1/3 sources visible" banner that the UI shows for partial
# entries, and a name-only entry would risk leaking entity existence
# without contributing useful text.
#
# Each entity / relation carries up to ``_GRAPH_CHUNK_PREVIEW`` source
# chunk_ids back to the LLM so it can call ``read_chunk`` on the
# specific chunk that grounds a claim.

_GRAPH_DEFAULT_TOP_K = 5
_GRAPH_MAX_TOP_K = 20
_GRAPH_CHUNK_PREVIEW = 3


def _handle_graph_explore(params: dict, ctx: ToolContext) -> dict:
    gs = getattr(ctx.state, "graph_store", None)
    if gs is None:
        return DispatchError(
            error="knowledge graph not configured", tool="graph_explore"
        ).to_result()

    query = params["query"]
    top_k = min(
        int(params.get("top_k", _GRAPH_DEFAULT_TOP_K)), _GRAPH_MAX_TOP_K
    )

    try:
        # Over-fetch — visibility filter drops partial / hidden.
        candidates = gs.search_entities(query, top_k=top_k * 3)
    except Exception as e:
        return DispatchError(
            error=f"graph search failed: {type(e).__name__}",
            tool="graph_explore",
        ).to_result()

    out_entities: list[dict] = []
    accepted_eids: set[str] = set()
    name_cache: dict[str, str] = {}

    for ent in candidates:
        ent_dict = {
            "entity_id": ent.entity_id,
            "name": ent.name,
            "entity_type": ent.entity_type,
            "description": ent.description,
            "source_doc_ids": sorted(ent.source_doc_ids),
            "source_chunk_ids": sorted(ent.source_chunk_ids),
        }
        # Filter — full only. ``filter_entity`` returns
        # ``(None, None)`` for hidden, ``(dict, Visibility)`` for
        # partial, ``(dict, None)`` for full. Drop the first two.
        filtered, vis = filter_entity(ent_dict, accessible=ctx.accessible)
        if filtered is None or vis is not None:
            continue
        accepted_eids.add(ent.entity_id)
        name_cache[ent.entity_id] = ent.name
        out_entities.append(
            {
                "entity_id": ent.entity_id,
                "name": ent.name,
                "type": ent.entity_type,
                "description": filtered["description"],
                # Cap chunk preview — full source_chunk_ids list can
                # be hundreds for hub entities; LLM only needs a few
                # to ground citations.
                "source_chunk_ids": filtered["source_chunk_ids"][
                    :_GRAPH_CHUNK_PREVIEW
                ],
            }
        )
        if len(out_entities) >= top_k:
            break

    if not out_entities:
        return {"entities": [], "relations": []}

    # Pull relations involving the accepted entities. Dedup by
    # relation_id — a relation between two accepted entities will
    # come back from both endpoints' get_relations call.
    relations_out: list[dict] = []
    seen_rids: set[str] = set()

    for ent_data in out_entities:
        eid = ent_data["entity_id"]
        try:
            relations = gs.get_relations(eid)
        except Exception:
            continue
        for rel in relations:
            if rel.relation_id in seen_rids:
                continue
            rel_dict = {
                "relation_id": rel.relation_id,
                "source_entity": rel.source_entity,
                "target_entity": rel.target_entity,
                "keywords": rel.keywords,
                "description": rel.description,
                "source_doc_ids": sorted(rel.source_doc_ids),
                "source_chunk_ids": sorted(rel.source_chunk_ids),
            }
            filtered_rel = filter_relation(rel_dict, accessible=ctx.accessible)
            if filtered_rel is None:
                continue
            # Partial relations come back with description=None;
            # treat same as hidden in agent context.
            if not filtered_rel.get("description"):
                continue
            seen_rids.add(rel.relation_id)

            # Resolve endpoint names — query the graph for any
            # endpoint whose entity didn't make our top-k cut.
            for endpoint in ("source_entity", "target_entity"):
                other_eid = filtered_rel[endpoint]
                if other_eid in name_cache:
                    continue
                try:
                    other_ent = gs.get_entity(other_eid)
                    name_cache[other_eid] = (
                        other_ent.name if other_ent else other_eid
                    )
                except Exception:
                    name_cache[other_eid] = other_eid

            relations_out.append(
                {
                    "source": name_cache.get(
                        rel.source_entity, rel.source_entity
                    ),
                    "target": name_cache.get(
                        rel.target_entity, rel.target_entity
                    ),
                    "keywords": filtered_rel["keywords"],
                    "description": filtered_rel["description"],
                    "source_chunk_ids": filtered_rel.get(
                        "source_chunk_ids", []
                    )[:_GRAPH_CHUNK_PREVIEW],
                }
            )

    return {"entities": out_entities, "relations": relations_out}


_GRAPH_EXPLORE_SPEC = ToolSpec(
    name="graph_explore",
    description=(
        "Knowledge graph search by entity name. Returns matched entities + "
        "their relations as LLM-synthesised descriptions across all "
        "(accessible) source documents. Use this when the user asks "
        "about a specific concept / person / company and you want "
        "high-level synthesis instead of raw chunks. Each entry carries "
        "source_chunk_ids — call read_chunk on a specific id to ground a "
        "citation. Default top_k=5."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Entity name or topic to look up.",
            },
            "top_k": {
                "type": "integer",
                "description": (
                    f"Number of entities to return. Default "
                    f"{_GRAPH_DEFAULT_TOP_K}, max {_GRAPH_MAX_TOP_K}."
                ),
            },
        },
        "required": ["query"],
    },
    handler=_handle_graph_explore,
)


TOOL_REGISTRY: dict[str, ToolSpec] = {
    _BM25_SPEC.name: _BM25_SPEC,
    _VECTOR_SPEC.name: _VECTOR_SPEC,
    _READ_CHUNK_SPEC.name: _READ_CHUNK_SPEC,
    _GRAPH_EXPLORE_SPEC.name: _GRAPH_EXPLORE_SPEC,
}
