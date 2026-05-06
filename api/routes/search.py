"""POST /api/v1/search — BM25 keyword search with cross-lingual
query expansion.

Distinct from the agent path (``/agent``, which runs the full
loop with vector + KG + tree + rerank + LLM): this endpoint runs
JUST a lexical pass and returns ranked passages + a file rollup,
both with matched-token lists for keyword highlighting in the UI.

Cross-lingual is solved by a pre-pass:

  1. Detect the query's language (CJK char heuristic).
  2. Call a small LLM to translate it into the other supported
     language(s) — see ``api/search/translation.py``.
  3. Send the union (original + translations) into BM25 as a
     single space-joined query.

This keeps every property the dedicated Search page wants: BM25's
predictable ranking, ``matched_tokens`` for the keyword highlight,
file-as-primary visual via the rollup. Translation overhead is a
single small-model completion (cached LRU per query, bypassed on
config disable / failure).
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state, resolve_path_filters
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
def search(
    req: SearchRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
) -> SearchResponse:
    """Run BM25 over the chunk + filename indices, optionally
    expanding the query into other languages first via the
    Search-page translator. Returns ranked chunks + a file
    rollup (when requested via ``include=["files"]``).
    """
    if not req.query.strip():
        raise HTTPException(400, "query must not be empty")

    include = req.include or ["chunks"]
    limit_dict: dict[str, int] = {}
    if req.limit:
        if req.limit.chunks is not None:
            limit_dict["chunks"] = req.limit.chunks
        if req.limit.files is not None:
            limit_dict["files"] = req.limit.files

    # Authz: resolve / validate the caller's requested path_filters
    # against their accessible folder set. Admin role bypasses; non-
    # admins get 403 on the first unauthorised path.
    path_prefixes = resolve_path_filters(state, principal, req.path_filters)

    # Cross-lingual expansion. The translator returns the original
    # query first plus translations into the other configured
    # languages; we space-join into one BM25 query string. On any
    # error / disabled config / no-target the helper returns just
    # the original, so this is always at least a no-op.
    t_xform_ms = 0
    expanded_query = req.query
    translations: list[str] = [req.query]
    translator = getattr(state, "query_translator", None)
    if translator is not None:
        t0 = time.time()
        try:
            translations = translator.expand(req.query)
        except Exception:
            log.exception("query translation failed; falling back to original")
            translations = [req.query]
        t_xform_ms = int((time.time() - t0) * 1000)
        # de-dupe + space-join — see translation.join_for_bm25 docstring.
        from ..search.translation import join_for_bm25

        expanded_query = join_for_bm25(translations) or req.query

    result = state.unified_search.search(
        expanded_query,
        include=include,
        limit=limit_dict or None,
        filter=req.filter,
        path_prefixes=path_prefixes,
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
        # Hydrate document metadata + uploader display name in two
        # bulk queries instead of N+1 round-trips. We only need
        # what the row UI shows: created_at / updated_at /
        # uploader_user_id and the uploader's display label
        # (display_name or email-prefix or username, same fallback
        # the rest of the UI uses).
        doc_ids = [f.doc_id for f in result.files]
        docs = (
            state.store.get_documents_by_ids(doc_ids) if doc_ids else []
        )
        doc_by_id = {d["doc_id"]: d for d in docs}
        uploader_ids = sorted({
            d.get("user_id")
            for d in docs
            if d.get("user_id")
        })
        uploader_label_by_id: dict[str, str] = {}
        uploader_has_avatar_by_id: dict[str, bool] = {}
        if uploader_ids:
            from sqlalchemy import select as _select

            from persistence.models import AuthUser

            with state.store.transaction() as sess:
                rows = sess.execute(
                    _select(AuthUser).where(AuthUser.user_id.in_(uploader_ids))
                ).scalars()
                for u in rows:
                    label = (
                        u.display_name
                        or (u.email.split("@")[0] if u.email else None)
                        or u.username
                        or u.user_id
                    )
                    uploader_label_by_id[u.user_id] = label
                    uploader_has_avatar_by_id[u.user_id] = bool(u.avatar_path)

        files_out = []
        for f in result.files:
            doc = doc_by_id.get(f.doc_id) or {}
            uploader_id = doc.get("user_id")
            files_out.append(
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
                    created_at=doc.get("created_at"),
                    updated_at=doc.get("updated_at"),
                    uploader_user_id=uploader_id,
                    uploader_display_name=(
                        uploader_label_by_id.get(uploader_id) if uploader_id else None
                    ),
                    uploader_has_avatar=(
                        uploader_has_avatar_by_id.get(uploader_id, False) if uploader_id else False
                    ),
                )
            )

    # Surface the translation step in stats so the UI can show
    # how the query got expanded (useful for debugging / building
    # user trust in the cross-lingual results).
    stats = dict(result.stats or {})
    stats["translations"] = translations if len(translations) > 1 else None
    stats["translation_ms"] = t_xform_ms
    stats["expanded_query"] = expanded_query if expanded_query != req.query else None

    return SearchResponse(
        query=req.query,
        chunks=chunks_out,
        files=files_out,
        stats=stats,
    )
