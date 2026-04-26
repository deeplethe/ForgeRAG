"""
Retrieval-layer OpenTelemetry helpers.

Two responsibilities:

1. ``get_tracer()`` — the tracer we use in pipeline.py, answering.pipeline,
   and anywhere else business-level spans are emitted.

2. ``RequestSpanCollector`` — a SpanProcessor that keeps finished spans in
   memory, keyed by trace_id, so the `/api/v1/query` route can grab the
   spans produced during one request and ship them to the frontend as
   raw OTel JSON.

The collector is singleton (one instance for the whole process) — it's
mounted alongside the stdout / OTLP exporters by
``config.observability.bootstrap``.

Thread-safety: ``on_end()`` is called from whichever thread ends the
span (may be a ThreadPoolExecutor worker). The internal dict + LRU
eviction are guarded by a lock. Exported spans are also still sent to
the other processors; this collector is purely additive.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, ClassVar

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor


def get_tracer() -> trace.Tracer:
    """Project-wide tracer. Acquiring the provider lazily means tests that
    never call ``observability.bootstrap()`` still get a no-op tracer."""
    return trace.get_tracer("forgerag")


# ---------------------------------------------------------------------------
# Request-scoped span collector
# ---------------------------------------------------------------------------


class RequestSpanCollector(SpanProcessor):
    """
    Accumulates finished spans per trace_id. Bounded to avoid leaking
    memory if a handler forgets to ``take()`` (e.g. mid-request crash).

    Usage:
        # Somewhere inside the /query route, AFTER starting a root span:
        root = tracer.start_as_current_span("forgerag.retrieve")
        with root as span:
            trace_id = span.get_span_context().trace_id
            # ... do work, child spans get collected automatically ...
        spans = RequestSpanCollector.singleton().take(trace_id)
        # ``spans`` is a list of ReadableSpan (already ended).
    """

    _INSTANCE: ClassVar["RequestSpanCollector | None"] = None
    _MAX_ENTRIES: ClassVar[int] = 500  # per-trace buffers kept in memory

    def __init__(self) -> None:
        self._buffers: OrderedDict[int, list[ReadableSpan]] = OrderedDict()
        self._lock = threading.Lock()

    # ── Singleton (ctor-less access for bootstrap) ───────────────────

    @classmethod
    def singleton(cls) -> "RequestSpanCollector":
        if cls._INSTANCE is None:
            cls._INSTANCE = cls()
        return cls._INSTANCE

    # ── SpanProcessor protocol ──────────────────────────────────────

    def on_start(self, span, parent_context=None) -> None:
        # We only care about ended spans. Start is a no-op.
        pass

    def on_end(self, span: ReadableSpan) -> None:
        ctx = span.get_span_context()
        if not ctx or not ctx.trace_id:
            return
        tid = ctx.trace_id
        with self._lock:
            buf = self._buffers.get(tid)
            if buf is None:
                # Evict oldest if at capacity (LRU-ish)
                if len(self._buffers) >= self._MAX_ENTRIES:
                    self._buffers.popitem(last=False)
                buf = []
                self._buffers[tid] = buf
            buf.append(span)
            # Mark trace as recently used so it survives eviction
            self._buffers.move_to_end(tid)

    def shutdown(self) -> None:
        with self._lock:
            self._buffers.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        # Nothing to flush — spans are buffered, not exported externally.
        return True

    # ── Public API used by /query route ──────────────────────────────

    def take(self, trace_id: int) -> list[ReadableSpan]:
        """Remove and return all spans collected for ``trace_id``."""
        with self._lock:
            return self._buffers.pop(trace_id, [])


# ---------------------------------------------------------------------------
# Serialisation — OTel ReadableSpan → standard dict (JSON-ready)
# ---------------------------------------------------------------------------


def span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    """
    Render one ReadableSpan as the standard OTel JSON shape. We roll our
    own (instead of relying on ``span.to_json()``) because the built-in
    output is a JSON-encoded string keyed slightly differently from what
    the Otel spec describes — and the frontend is easier to write against
    a stable, explicit schema.
    """
    ctx = span.get_span_context()
    parent = span.parent
    return {
        "trace_id": f"{ctx.trace_id:032x}" if ctx else None,
        "span_id":  f"{ctx.span_id:016x}"  if ctx else None,
        "parent_span_id": f"{parent.span_id:016x}" if parent else None,
        "name": span.name,
        "kind": str(span.kind).rsplit(".", 1)[-1] if span.kind else None,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano":   span.end_time,
        "duration_ms": (
            (span.end_time - span.start_time) / 1_000_000
            if span.start_time and span.end_time else None
        ),
        "status": {
            "code": str(span.status.status_code).rsplit(".", 1)[-1],
            "description": span.status.description,
        },
        "attributes": dict(span.attributes) if span.attributes else {},
        "events": [
            {
                "name": ev.name,
                "timestamp_unix_nano": ev.timestamp,
                "attributes": dict(ev.attributes) if ev.attributes else {},
            }
            for ev in (span.events or [])
        ],
    }


def spans_to_payload(spans: list[ReadableSpan]) -> dict[str, Any]:
    """Wrap a list of spans in the response-shape the frontend consumes."""
    return {"spans": [span_to_dict(s) for s in spans]}

