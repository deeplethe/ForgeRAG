"""
/api/v1/chunks  — standalone chunk access + search + neighbors
/api/v1/blocks  — standalone block access + page-level query + image serving

Multi-user authz: every single-resource fetch goes through the
``require_*_access`` helpers in ``api.deps``. They look up the
resource, resolve its containing folder, and call
``authz.can(folder_id, "read")`` for the principal. Cross-user
access — including admin trying to read someone's private folder
content — collapses to 404 instead of 200; same code as a missing
resource so the endpoint never confirms a stranger's id is real.
This also gates the "click a citation" UX: when a user loses
access to the source folder, GETs against the underlying chunk /
block / image start returning 404 immediately.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..auth import AuthenticatedPrincipal
from ..deps import (
    get_principal,
    get_state,
    require_block_access,
    require_chunk_access,
    require_doc_access,
)
from ..schemas import BlockOut, ChunkOut
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["chunks", "blocks"])


# ---------------------------------------------------------------------------
# Global chunk search (keyword across all documents)
# NOTE: /chunks/search and /chunks/by-node must be registered BEFORE
# /chunks/{chunk_id} so FastAPI doesn't treat "search" / "by-node" as a
# chunk_id path parameter.
# ---------------------------------------------------------------------------


@router.get("/chunks/search")
def search_chunks(
    q: str = Query(..., min_length=1, description="Search keyword"),
    top_k: int = Query(20, ge=1, le=100),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """
    BM25 keyword search across all indexed chunks. Returns matches
    with scores, ordered by relevance. Fast (in-memory BM25).

    Hits from folders the caller can't read are filtered out
    post-BM25 so the search bar never leaks chunk content from
    inaccessible folders. The legacy ``/search`` route does the
    same via ``UnifiedSearcher`` + path-prefix scope; this older
    endpoint catches up here.
    """
    if state._bm25 is None:
        state.refresh_bm25()
    bm25 = state._bm25
    if bm25 is None or len(bm25) == 0:
        return {"items": [], "total": 0}
    # Over-fetch to leave headroom for the post-filter; the BM25
    # index is small enough that a 3× grab is cheap.
    raw_results = bm25.search_chunks(q, top_k * 3)
    auth_on = state.cfg.auth.enabled and principal.via != "auth_disabled"

    items: list[dict] = []
    for chunk_id, score in raw_results:
        row = state.store.get_chunk(chunk_id)
        if not row:
            continue
        if auth_on:
            doc = state.store.get_document(row["doc_id"])
            if not doc:
                continue
            folder_id = doc.get("folder_id")
            if not folder_id or not state.authz.can(
                principal.user_id, folder_id, "read"
            ):
                continue
        items.append(
            {
                "chunk_id": chunk_id,
                "score": round(score, 4),
                "doc_id": row["doc_id"],
                "node_id": row["node_id"],
                "content_type": row["content_type"],
                "page_start": row["page_start"],
                "section_path": row.get("section_path", []),
                "snippet": (row.get("content") or "")[:200],
            }
        )
        if len(items) >= top_k:
            break
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Chunks by node
# ---------------------------------------------------------------------------


@router.get("/chunks/by-block/{block_id}")
def get_chunk_by_block(
    block_id: str,
    doc_id: str = Query(..., description="Document ID to scope the search"),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Find the chunk that contains the given block_id, with its sorted position."""
    doc = require_doc_access(state, principal, doc_id)
    pv = doc["active_parse_version"]
    row = state.store.find_chunk_by_block_id(doc_id, pv, block_id)
    if not row:
        raise HTTPException(404, "no chunk contains this block")
    chunk = ChunkOut(**{k: row[k] for k in ChunkOut.model_fields if k in row})
    pos = state.store.chunk_position(doc_id, pv, row["chunk_id"])
    return {"chunk": chunk, "position": pos}


@router.get("/chunks/by-node/{node_id}", response_model=list[ChunkOut])
def get_chunks_by_node(
    node_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Filter to chunks the caller can read. A node may legally
    contain chunks from multiple docs in different folders, so we
    check each row's parent doc folder."""
    rows = state.store.get_chunks_by_node_ids([node_id])
    auth_on = state.cfg.auth.enabled and principal.via != "auth_disabled"
    if auth_on:
        rows = [
            r for r in rows
            if _doc_folder_readable(state, principal, r.get("doc_id"))
        ]
    return [ChunkOut(**{k: r[k] for k in ChunkOut.model_fields if k in r}) for r in rows]


# ---------------------------------------------------------------------------
# Single chunk
# ---------------------------------------------------------------------------


@router.get("/chunks/{chunk_id}", response_model=ChunkOut)
def get_chunk(
    chunk_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    row = require_chunk_access(state, principal, chunk_id)
    return ChunkOut(**{k: row[k] for k in ChunkOut.model_fields if k in row})


# ---------------------------------------------------------------------------
# Chunk neighbors (context browser in frontend)
# ---------------------------------------------------------------------------


@router.get("/chunks/{chunk_id}/neighbors")
def get_chunk_neighbors(
    chunk_id: str,
    before: int = Query(2, ge=0, le=10),
    after: int = Query(2, ge=0, le=10),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """
    Get surrounding chunks of the same document, ordered by chunk_id.
    Useful for showing context around a search result.
    """
    row = require_chunk_access(state, principal, chunk_id)
    doc_id = row["doc_id"]
    pv = row["parse_version"]
    all_chunks = state.store.get_chunks(doc_id, pv)
    ids = [c["chunk_id"] for c in all_chunks]
    try:
        idx = ids.index(chunk_id)
    except ValueError:
        raise HTTPException(404, "chunk not found in document")
    start = max(0, idx - before)
    end = min(len(all_chunks), idx + after + 1)
    return {
        "target_index": idx - start,
        "chunks": [ChunkOut(**{k: c[k] for k in ChunkOut.model_fields if k in c}) for c in all_chunks[start:end]],
    }


# ---------------------------------------------------------------------------
# Single block
# ---------------------------------------------------------------------------


@router.get("/blocks/{block_id}", response_model=BlockOut)
def get_block(
    block_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    row = require_block_access(state, principal, block_id)
    from .documents import _block_out

    return _block_out(row)


# ---------------------------------------------------------------------------
# Blocks by page (for PDF viewer overlay)
# ---------------------------------------------------------------------------


@router.get("/blocks/by-page/{doc_id}/{page_no}")
def get_blocks_by_page(
    doc_id: str,
    page_no: int,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """
    Get all blocks on a specific page of a document. The frontend
    PDF viewer uses this to overlay block boundaries, types, and
    highlights on the rendered page.
    """
    doc = require_doc_access(state, principal, doc_id)
    pv = doc["active_parse_version"]
    all_blocks = state.store.get_blocks(doc_id, pv)
    page_blocks = [b for b in all_blocks if b["page_no"] == page_no]
    from .documents import _block_out

    return {
        "doc_id": doc_id,
        "page_no": page_no,
        "blocks": [_block_out(b) for b in page_blocks],
    }


# ---------------------------------------------------------------------------
# Block image (serve extracted figure)
# ---------------------------------------------------------------------------


@router.get("/blocks/{block_id}/image")
def get_block_image(
    block_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """
    Serve the extracted image for a block with image_storage_key.
    Returns the raw image bytes with appropriate Content-Type.
    """
    row = require_block_access(state, principal, block_id)
    key = row.get("image_storage_key")
    if not key:
        raise HTTPException(404, "block has no image")
    try:
        data = state.blob.get(key)
    except (FileNotFoundError, KeyError):
        raise HTTPException(404, "image blob not found")
    mime = row.get("image_mime") or "image/png"
    return Response(content=data, media_type=mime)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_folder_readable(
    state: AppState,
    principal: AuthenticatedPrincipal,
    doc_id: str | None,
) -> bool:
    """Lightweight per-row read check used by list endpoints (don't
    raise — just yes/no). The single-resource paths use the
    ``require_*_access`` helpers which raise 404."""
    if not doc_id:
        return False
    doc = state.store.get_document(doc_id)
    if not doc:
        return False
    folder_id = doc.get("folder_id")
    if not folder_id:
        return False
    return state.authz.can(principal.user_id, folder_id, "read")
