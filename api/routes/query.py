"""
POST /api/v1/query — retrieval + answer generation (normal + SSE streaming)
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from parser.schema import Citation

from ..deps import get_state
from ..schemas import CitationOut, HighlightOut, QueryRequest, QueryResponse
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["query"])


def _citation_out(c: Citation) -> CitationOut:
    return CitationOut(
        citation_id=c.citation_id,
        doc_id=c.doc_id,
        file_id=c.file_id,
        parse_version=c.parse_version,
        page_no=c.page_no,
        highlights=[HighlightOut(page_no=h.page_no, bbox=h.bbox) for h in c.highlights],
        snippet=c.snippet,
        score=c.score,
        open_url=c.open_url,
    )


@router.post("/query")
def query(req: QueryRequest, state: AppState = Depends(get_state)):
    if not req.query or not req.query.strip():
        raise HTTPException(400, "query must not be empty")

    if req.stream:
        return _stream_response(req, state)
    return _normal_response(req, state)


# ---------------------------------------------------------------------------
# Normal (non-streaming)
# ---------------------------------------------------------------------------


def _inject_path_filter(req: QueryRequest) -> dict | None:
    """
    Merge path_filter into the retrieval filter dict under the
    reserved key '_path_filter'. RetrievalPipeline reads it to
    build a doc_id whitelist.
    """
    if not req.path_filter:
        return req.filter
    merged = dict(req.filter or {})
    merged["_path_filter"] = req.path_filter
    return merged


def _normal_response(req: QueryRequest, state: AppState) -> QueryResponse:
    try:
        answer = state.answering.ask(
            req.query,
            filter=_inject_path_filter(req),
            conversation_id=req.conversation_id,
        )
    except Exception as e:
        log.exception("query failed")
        raise HTTPException(500, f"query failed: {e}")
    return QueryResponse(
        query=answer.query,
        text=answer.text,
        citations_used=[_citation_out(c) for c in answer.citations_used],
        citations_all=[_citation_out(c) for c in answer.citations_all],
        model=answer.model,
        finish_reason=answer.finish_reason,
        stats=answer.stats,
        trace=answer.stats.get("retrieval", {}).get("trace"),
    )


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------


def _stream_response(req: QueryRequest, state: AppState) -> StreamingResponse:
    """
    Server-Sent Events stream. Three event types:

        event: retrieval
        data: {"vector_hits": 30, "tree_hits": 20, "citations_all": [...]}

        event: delta
        data: {"text": "The answer"}     (repeated, one per token batch)

        event: done
        data: {"text": "full answer...", "citations_used": [...], "stats": {...}}

    Frontend usage (JS):
        const es = new EventSource(...)   // or fetch + ReadableStream
        // see below for fetch-based approach since POST isn't supported by EventSource
    """

    def _generate():
        try:
            for event in state.answering.ask_stream(
                req.query,
                filter=_inject_path_filter(req),
                conversation_id=req.conversation_id,
            ):
                event_type = event.get("event", "delta")
                data = json.dumps(event.get("data", {}), ensure_ascii=False, default=str)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except Exception as e:
            log.exception("stream query failed")
            error_data = json.dumps({"error": str(e)})
            yield f"event: error\ndata: {error_data}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
