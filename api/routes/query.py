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
    from retrieval.pipeline import RetrievalError
    from retrieval.telemetry import RequestSpanCollector, spans_to_payload

    try:
        answer = state.answering.ask(
            req.query,
            filter=_inject_path_filter(req),
            conversation_id=req.conversation_id,
            overrides=req.overrides,
        )
    except RetrievalError as e:
        # Upstream dependency failed (LLM / embedder / KG store / reranker).
        # 502 = "gateway got a bad response from something it relies on",
        # distinguishing infra faults from our own 500s.
        log.warning("retrieval path %r failed: %s", e.path, e)
        raise HTTPException(
            502,
            detail={"error": "retrieval_failed", "path": e.path, "message": str(e)},
        )
    except Exception as e:
        log.exception("query failed")
        raise HTTPException(500, f"query failed: {e}")

    # Pull OTel spans collected during this request; ship as raw JSON for
    # the frontend trace viewer. The root ``forgerag.answer`` span has
    # already ended by the time we get here, so the collector holds the
    # complete tree.
    trace_payload = None
    _tid = answer.stats.pop("otel_trace_id_int", None)
    if _tid:
        try:
            spans = RequestSpanCollector.singleton().take(_tid)
            trace_payload = spans_to_payload(spans)
        except Exception:
            log.exception("failed to collect OTel trace spans")

    return QueryResponse(
        query=answer.query,
        text=answer.text,
        citations_used=[_citation_out(c) for c in answer.citations_used],
        citations_all=[_citation_out(c) for c in answer.citations_all],
        model=answer.model,
        finish_reason=answer.finish_reason,
        stats=answer.stats,
        trace=trace_payload,
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

    from retrieval.pipeline import RetrievalError

    def _generate():
        try:
            for event in state.answering.ask_stream(
                req.query,
                filter=_inject_path_filter(req),
                conversation_id=req.conversation_id,
                overrides=req.overrides,
            ):
                event_type = event.get("event", "delta")
                data = json.dumps(event.get("data", {}), ensure_ascii=False, default=str)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except RetrievalError as e:
            # SSE can't change HTTP status mid-stream, so we emit a
            # structured error event and let the client decide. The
            # ``path`` field tells the UI which component died.
            log.warning("stream retrieval path %r failed: %s", e.path, e)
            error_data = json.dumps(
                {
                    "error": "retrieval_failed",
                    "path": e.path,
                    "message": str(e),
                }
            )
            yield f"event: error\ndata: {error_data}\n\n"
        except Exception as e:
            log.exception("stream query failed")
            error_data = json.dumps({"error": "internal", "message": str(e)})
            yield f"event: error\ndata: {error_data}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
