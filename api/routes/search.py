"""POST /api/v1/search — semantic (embedding) search over chunks.

Distinct from ``/agent`` (the chat path that runs the full agent
loop with BM25 + vector + KG + tree + rerank + LLM): this endpoint
runs JUST the dense-embedding pass and returns ranked passages
with file/page context. Used by the dedicated Search page in the
frontend — meant to be cheap (one embed + one ANN call).

Why semantic, not BM25:
  Cross-lingual recall. The embedding model maps queries and
  passages into a shared multilingual vector space, so a Chinese
  query like ``蜜蜂`` retrieves English passages mentioning
  ``bees`` and vice versa. BM25 only matches identical tokens
  and would never bridge the two. The earlier BM25 implementation
  used a regex tokenizer (``[a-z0-9]+|[\\u4e00-\\u9fff]``) that
  also segmented Chinese one character at a time, producing noisy
  results — semantic search sidesteps that entirely.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state, resolve_path_filters
from ..schemas import ScoredChunkOut, SearchRequest, SearchResponse
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["search"])

# Snippet trim — full chunk content lives in the chunks table; the
# Search page only needs enough text to convey what was matched.
_SNIPPET_CHARS = 320
_DEFAULT_TOP_K = 30
_MAX_TOP_K = 100


@router.post("/search", response_model=SearchResponse)
def search(
    req: SearchRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
) -> SearchResponse:
    """Embed the query, run dense ANN search, hydrate hits with
    filename / path / page so the UI can render them as clickable
    rows. Path scope is honored via the vector backend's filter
    (pgvector / Chroma / Qdrant all understand ``path_prefixes``).
    """
    if not req.query.strip():
        raise HTTPException(400, "query must not be empty")

    embedder = getattr(state, "embedder", None)
    vector = getattr(state, "vector", None)
    if embedder is None or vector is None:
        # Surface a precise error instead of a generic 500 — usually
        # this means the deploy hasn't configured an embedder /
        # vector backend yet (single-user dev rigs sometimes skip it).
        raise HTTPException(503, "vector index not available")

    top_k = _DEFAULT_TOP_K
    if req.limit and req.limit.chunks is not None:
        top_k = min(int(req.limit.chunks), _MAX_TOP_K)

    # Authz: resolve the caller's requested path_filters against
    # their accessible folder set. Admin role bypasses; non-admins
    # get 403 on the first unauthorised path.
    path_prefixes = resolve_path_filters(state, principal, req.path_filters)

    t0 = time.time()
    try:
        q_vec = embedder.embed_texts([req.query])[0]
    except Exception as e:
        log.exception("embed failed")
        raise HTTPException(500, f"embedding failed: {type(e).__name__}") from e

    vfilter: dict | None = None
    if path_prefixes:
        vfilter = {"path_prefixes": list(path_prefixes)}

    try:
        # Over-fetch by 3x so the post-hydration scope filter
        # still leaves room for top_k after dropping any
        # trashed / out-of-scope rows.
        hits = vector.search(q_vec, top_k=top_k * 3, filter=vfilter)
    except Exception as e:
        log.exception("vector search failed")
        raise HTTPException(500, f"vector search failed: {type(e).__name__}") from e

    # Vector backends return either dicts or objects — normalise
    # to (chunk_id, score) tuples in original ranking order.
    raw: list[tuple[str, float]] = []
    for h in hits:
        cid = getattr(h, "chunk_id", None) or (h.get("chunk_id") if isinstance(h, dict) else None)
        sc = getattr(h, "score", None)
        if sc is None and isinstance(h, dict):
            sc = h.get("score")
        if cid:
            raw.append((cid, float(sc or 0.0)))

    chunks_out = _hydrate(state, raw, top_k=top_k)
    elapsed_ms = int((time.time() - t0) * 1000)

    return SearchResponse(
        query=req.query,
        chunks=chunks_out,
        files=None,
        stats={
            "chunk_hits": len(chunks_out),
            "file_hits": 0,
            "elapsed_ms": elapsed_ms,
            "backend": "vector",
        },
    )


def _hydrate(
    state: AppState,
    raw: list[tuple[str, float]],
    *,
    top_k: int,
) -> list[ScoredChunkOut]:
    """Bulk-hydrate ``(chunk_id, score)`` tuples into ScoredChunkOut.

    Rebuilds ranking order from the original raw list (the bulk
    fetcher may return rows in a different order). Drops rows whose
    chunk has been deleted between the ANN fetch and the hydrate —
    the vector index can lag behind the relational store briefly
    after a re-parse.
    """
    if not raw:
        return []
    chunk_ids = [cid for cid, _ in raw]
    score_by_id = {cid: sc for cid, sc in raw}

    rows = state.store.get_chunks_by_ids(chunk_ids)
    by_id = {r["chunk_id"]: r for r in rows}
    doc_ids = list({r["doc_id"] for r in rows})
    docs = state.store.get_documents_by_ids(doc_ids) if doc_ids else []
    doc_by_id = {d["doc_id"]: d for d in docs}

    out: list[ScoredChunkOut] = []
    for cid, _sc in raw:
        row = by_id.get(cid)
        if row is None:
            continue
        doc = doc_by_id.get(row["doc_id"])
        content = row.get("content") or ""
        snippet = content[:_SNIPPET_CHARS]
        if len(content) > _SNIPPET_CHARS:
            snippet += "…"
        out.append(
            ScoredChunkOut(
                chunk_id=cid,
                doc_id=row["doc_id"],
                filename=(doc or {}).get("filename") or "",
                # Documents carry their folder path; chunks don't.
                # Falling back to filename keeps the row legible
                # if doc lookup somehow misses.
                path=(doc or {}).get("path") or "",
                page_no=row.get("page_start") or 0,
                snippet=snippet,
                score=round(score_by_id[cid], 4),
                boosted_by_filename=False,
                matched_tokens=None,
            )
        )
        if len(out) >= top_k:
            break
    return out
