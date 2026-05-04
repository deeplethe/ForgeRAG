"""POST /api/v1/search — BM25-only keyword search (chunks default, files opt-in).

Distinct from ``/query``: this endpoint runs pure lexical search (no
vector / KG / tree / rerank / LLM) and returns ranked hits with
matched-token lists so the UI can highlight keywords. ``/query`` is
where the answering pipeline + structural retrieval lives.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_state
from ..schemas import (
    ChunkMatchOut,
    FileHitOut,
    ScoredChunkOut,
    SearchRequest,
    SearchResponse,
)
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/search", response_model=SearchResponse)
def search(req: SearchRequest, state: AppState = Depends(get_state)) -> SearchResponse:
    """Run BM25 keyword search. Returns chunks by default; opt-in
    ``include=["files"]`` adds a file-level rollup view.

    Distinct from ``/query``:

      * ``/query`` runs the full retrieval pipeline (BM25 + vector +
        KG + tree + RRF + rerank) and synthesises an LLM answer with
        citation IDs. Used by Chat.
      * ``/search`` runs pure BM25 over chunk text + filenames, returns
        ranked hits with matched-token lists for keyword highlighting.
        Used by the Workspace search bar — meant to be cheap and fast.
    """
    if not req.query.strip():
        raise HTTPException(400, "query must not be empty")

    # Adapt include[] — the searcher silently falls back to ["chunks"]
    # for empty / unrecognised values, matching its docstring contract.
    include = req.include or ["chunks"]
    limit_dict: dict[str, int] = {}
    if req.limit:
        if req.limit.chunks is not None:
            limit_dict["chunks"] = req.limit.chunks
        if req.limit.files is not None:
            limit_dict["files"] = req.limit.files

    result = state.unified_search.search(
        req.query,
        include=include,
        limit=limit_dict or None,
        filter=req.filter,
        path_prefixes=req.path_filters,
        overrides=req.overrides,
    )

    chunks_out = [
        ScoredChunkOut(
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            filename=c.filename,
            path=c.path,
            page_no=c.page_no,
            snippet=c.snippet,
            score=c.score,
            boosted_by_filename=c.boosted_by_filename,
            matched_tokens=c.matched_tokens,
        )
        for c in result.chunks
    ]

    files_out: list[FileHitOut] | None = None
    if result.files is not None:
        files_out = [
            FileHitOut(
                doc_id=f.doc_id,
                filename=f.filename,
                path=f.path,
                format=f.format,
                score=f.score,
                matched_in=f.matched_in,
                best_chunk=ChunkMatchOut(
                    chunk_id=f.best_chunk.chunk_id,
                    snippet=f.best_chunk.snippet,
                    page_no=f.best_chunk.page_no,
                    score=f.best_chunk.score,
                    matched_tokens=f.best_chunk.matched_tokens,
                ) if f.best_chunk else None,
                filename_tokens=f.filename_tokens,
            )
            for f in result.files
        ]

    return SearchResponse(
        query=req.query,
        chunks=chunks_out,
        files=files_out,
        stats=result.stats,
    )
