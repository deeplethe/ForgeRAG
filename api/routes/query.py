"""
POST /api/v1/query — retrieval + answer generation (normal + SSE streaming)
"""

from __future__ import annotations

import contextlib
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
    Merge ``path_filters`` (list) into the retrieval filter dict under
    the reserved key '_path_filters'. RetrievalPipeline reads it to
    build the OR'd path-prefix scope and per-prefix doc_id whitelist.

    The schema's model_validator already promotes the legacy
    ``path_filter`` (str) into ``path_filters``, so this helper only
    has to look at the plural form.
    """
    if not req.path_filters:
        return req.filter
    merged = dict(req.filter or {})
    merged["_path_filters"] = list(req.path_filters)
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
            gen_overrides=req.generation_overrides,
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

    Decoupled producer/consumer: the heavy lifting (retrieval + LLM
    generation + DB persistence) runs in a background thread. The HTTP
    response streams events out of a Queue. When the client disconnects
    mid-stream, only the consumer dies — the producer thread keeps
    iterating ``ask_stream`` to completion so the answer + trace + turn
    save still happen. The user can close the tab and the answer will
    be in the DB when they come back.
    """

    import queue as _q
    import threading

    from retrieval.pipeline import RetrievalError

    # Reasonable cap so a runaway producer can't OOM us if the consumer
    # is gone and events keep accumulating. If full, ``put`` blocks; the
    # producer just slows down to whatever rate events are being drained
    # at — and once the consumer is detached the consumer thread (route
    # generator) is gone, so the queue saturates and ``put`` waits
    # forever. To avoid that, drop oldest events on overflow when no
    # consumer is reading: see ``client_alive``.
    Q_MAX = 256
    events_q: _q.Queue = _q.Queue(maxsize=Q_MAX)
    SENTINEL = object()
    client_alive = threading.Event()
    client_alive.set()

    def _producer():
        try:
            for event in state.answering.ask_stream(
                req.query,
                filter=_inject_path_filter(req),
                conversation_id=req.conversation_id,
                overrides=req.overrides,
                gen_overrides=req.generation_overrides,
            ):
                if client_alive.is_set():
                    # Block briefly; if the consumer is still reading
                    # and just hasn't drained yet, this throttles us
                    # without dropping anything.
                    try:
                        events_q.put(event, timeout=5)
                    except _q.Full:
                        # Consumer has stalled; drop to keep producer
                        # alive (its own finally will still persist).
                        pass
                # else: consumer gone — silently discard events but
                # KEEP iterating so ask_stream's finally block runs and
                # persists the assistant message + trace.
        except RetrievalError as e:
            log.warning("stream retrieval path %r failed: %s", e.path, e)
            if client_alive.is_set():
                with contextlib.suppress(_q.Full):
                    events_q.put(("__error__", {
                        "error": "retrieval_failed",
                        "path": e.path,
                        "message": str(e),
                    }), timeout=1)
        except Exception as e:
            log.exception("stream query failed in producer")
            if client_alive.is_set():
                with contextlib.suppress(_q.Full):
                    events_q.put(("__error__", {
                        "error": "internal",
                        "message": str(e),
                    }), timeout=1)
        finally:
            with contextlib.suppress(_q.Full):
                events_q.put(SENTINEL, timeout=1)

    threading.Thread(target=_producer, daemon=True, name="ask_stream_producer").start()

    def _generate():
        try:
            while True:
                item = events_q.get()
                if item is SENTINEL:
                    return
                if isinstance(item, tuple) and item and item[0] == "__error__":
                    yield f"event: error\ndata: {json.dumps(item[1])}\n\n"
                    continue
                event_type = item.get("event", "delta")
                data = json.dumps(item.get("data", {}), ensure_ascii=False, default=str)
                yield f"event: {event_type}\ndata: {data}\n\n"
        finally:
            # Mark consumer dead so producer drops further events instead
            # of blocking on a full queue. Producer keeps running to
            # completion regardless.
            client_alive.clear()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
