"""
/api/v1/chunks  — standalone chunk access + search + neighbors
/api/v1/blocks  — standalone block access + page-level query + image serving
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from ..deps import get_state
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
):
    """
    BM25 keyword search across all indexed chunks. Returns matches
    with scores, ordered by relevance. Fast (in-memory BM25).
    """
    if state._bm25 is None:
        state.refresh_bm25()
    bm25 = state._bm25
    if bm25 is None or len(bm25) == 0:
        return {"items": [], "total": 0}
    results = bm25.search_chunks(q, top_k)
    items = []
    for chunk_id, score in results:
        row = state.store.get_chunk(chunk_id)
        if row:
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
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Chunks by node
# ---------------------------------------------------------------------------


@router.get("/chunks/by-block/{block_id}")
def get_chunk_by_block(
    block_id: str,
    doc_id: str = Query(..., description="Document ID to scope the search"),
    state: AppState = Depends(get_state),
):
    """Find the chunk that contains the given block_id, with its sorted position."""
    doc = state.store.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
    pv = doc["active_parse_version"]
    row = state.store.find_chunk_by_block_id(doc_id, pv, block_id)
    if not row:
        raise HTTPException(404, "no chunk contains this block")
    chunk = ChunkOut(**{k: row[k] for k in ChunkOut.model_fields if k in row})
    pos = state.store.chunk_position(doc_id, pv, row["chunk_id"])
    return {"chunk": chunk, "position": pos}


@router.get("/chunks/by-node/{node_id}", response_model=list[ChunkOut])
def get_chunks_by_node(node_id: str, state: AppState = Depends(get_state)):
    rows = state.store.get_chunks_by_node_ids([node_id])
    return [ChunkOut(**{k: r[k] for k in ChunkOut.model_fields if k in r}) for r in rows]


# ---------------------------------------------------------------------------
# Single chunk
# ---------------------------------------------------------------------------


@router.get("/chunks/{chunk_id}", response_model=ChunkOut)
def get_chunk(chunk_id: str, state: AppState = Depends(get_state)):
    row = state.store.get_chunk(chunk_id)
    if not row:
        raise HTTPException(404, "chunk not found")
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
):
    """
    Get surrounding chunks of the same document, ordered by chunk_id.
    Useful for showing context around a search result.
    """
    row = state.store.get_chunk(chunk_id)
    if not row:
        raise HTTPException(404, "chunk not found")
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
def get_block(block_id: str, state: AppState = Depends(get_state)):
    row = state.store.get_block(block_id)
    if not row:
        raise HTTPException(404, "block not found")
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
):
    """
    Get all blocks on a specific page of a document. The frontend
    PDF viewer uses this to overlay block boundaries, types, and
    highlights on the rendered page.
    """
    doc = state.store.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "document not found")
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
def get_block_image(block_id: str, state: AppState = Depends(get_state)):
    """
    Serve the extracted image for a block with image_storage_key.
    Returns the raw image bytes with appropriate Content-Type.
    """
    row = state.store.get_block(block_id)
    if not row:
        raise HTTPException(404, "block not found")
    key = row.get("image_storage_key")
    if not key:
        raise HTTPException(404, "block has no image")
    try:
        data = state.blob.get(key)
    except (FileNotFoundError, KeyError):
        raise HTTPException(404, "image blob not found")
    mime = row.get("image_mime") or "image/png"
    return Response(content=data, media_type=mime)
