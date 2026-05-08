"""
Observability / OpenTelemetry bootstrap.

OpenCraig emits OTel spans for every retrieval phase, every LLM call, every
HTTP route, every SQL query. The user decides where those spans go by
setting ``observability.exporter`` in yaml — stdout (default, zero setup),
OTLP HTTP to any backend (Langfuse / Jaeger / Phoenix / Datadog / ...),
or none at all.

We also mount a ``RequestSpanCollector`` alongside the user's chosen
exporter so the `/api/v1/query` route can take a snapshot of the spans
produced during one request and ship them to the frontend as raw OTel
JSON. The frontend renders the trace viewer from that data directly —
no server-side shaping.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class ObservabilityConfig(BaseModel):
    enabled: bool = True
    service_name: str = "opencraig"
    exporter: Literal["stdout_compact", "stdout", "otlp", "none"] = Field(
        "stdout_compact",
        description=(
            "Where to send finished spans. ``stdout_compact`` (default) "
            "prints one filtered, condensed line per span "
            "(``[12ms] GET /api/v1/folders OK``); framework noise "
            "(httpcore TCP/TLS handshakes, sub-ms SQL internals) is "
            "dropped. ``stdout`` is the OpenTelemetry standard "
            "ConsoleSpanExporter — full multi-line JSON dump per span "
            "(very verbose; useful for piping to a JSON parser or "
            "deep-debugging a single trace). ``otlp`` sends via "
            "OTLP/HTTP to ``otlp_endpoint`` (Langfuse / Jaeger / "
            "Phoenix / etc.) for real observability dashboards. "
            "``none`` disables external export entirely; the per-"
            "request collector still feeds the frontend trace viewer "
            "either way."
        ),
    )
    otlp_endpoint: str | None = Field(
        None,
        description=(
            "OTLP/HTTP endpoint URL when exporter=otlp. "
            "Examples: http://localhost:4318/v1/traces (local collector), "
            "https://langfuse.example.com/api/public/otel/v1/traces."
        ),
    )
    otlp_headers: dict[str, str] | None = Field(
        None,
        description="Extra headers for OTLP — typically Auth tokens for SaaS.",
    )
    sample_ratio: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Parent-based sampling ratio. Lower to reduce volume in prod.",
    )


# ---------------------------------------------------------------------------
# Compact span exporter
# ---------------------------------------------------------------------------


# Span name prefixes whose spans are filtered out from compact stdout —
# they're framework noise (TCP / TLS handshake breakdown, SQL prepare /
# cursor / commit-step internals, internal litellm raw events) that
# drown the actually-interesting request / retrieval / LLM spans.
_NOISE_PREFIXES = (
    "connect_tcp",
    "start_tls",
    "send_request_headers",
    "send_request_body",
    "receive_response_headers",
    "receive_response_body",
    "connect",  # SQLAlchemy session connect — fires before every query
    "raw_gen_ai_request",  # litellm internal pre-request event
)

# Spans below this duration are dropped — sub-ms ORM internals aren't
# actionable and just bury the useful entries.
_MIN_DURATION_MS = 1.0


def _condense_sql(stmt: str) -> str:
    """``SELECT a, b, c, ... FROM users WHERE ...`` → ``SELECT FROM users``."""
    import re

    s = stmt.split("\n", 1)[0].strip()
    m = re.match(r"^\s*(SELECT|INSERT|UPDATE|DELETE)\b.*?\bFROM\s+(\w+)", s, re.IGNORECASE)
    if m:
        return f"{m.group(1).upper()} {m.group(2)}"
    m = re.match(r"^\s*(INSERT INTO|UPDATE)\s+(\w+)", s, re.IGNORECASE)
    if m:
        return f"{m.group(1).upper()} {m.group(2)}"
    return s[:60]


def _format_span_compact(span) -> str | None:
    """Format an OTel span as one line; return None to suppress noisy spans."""
    name = span.name or "<unnamed>"
    if any(name.startswith(p) for p in _NOISE_PREFIXES):
        return None

    duration_ms = (span.end_time - span.start_time) / 1_000_000  # ns → ms
    if duration_ms < _MIN_DURATION_MS:
        return None

    status_code = span.status.status_code.name if span.status else "UNSET"
    status_marker = {"OK": "OK", "ERROR": "ERR", "UNSET": "  "}.get(status_code, status_code)

    attrs = dict(span.attributes or {})

    # SQL spans get a condensed ``OP table_name`` rendering — the SQLAlchemy
    # span name already encodes the DB path, which is redundant noise.
    if "db.system" in attrs:
        stmt = attrs.get("db.statement", "")
        if stmt:
            return f"[{duration_ms:6.1f}ms] {status_marker} {_condense_sql(str(stmt))}"
        return f"[{duration_ms:6.1f}ms] {status_marker} {name}"

    # HTTP spans
    if "http.method" in attrs:
        target = attrs.get("http.route") or attrs.get("http.url") or attrs.get("http.target") or ""
        return f"[{duration_ms:6.1f}ms] {status_marker} {attrs['http.method']} {target}"

    # LLM / generative-AI spans
    if "gen_ai.request.model" in attrs:
        return f"[{duration_ms:6.1f}ms] {status_marker} {name} model={attrs['gen_ai.request.model']}"

    # Everything else: just span name
    return f"[{duration_ms:6.1f}ms] {status_marker} {name}"


class _CompactSpanExporter:
    """One-line-per-span exporter. Drop-in replacement for ConsoleSpanExporter
    when the user just wants a quick activity readout, not full JSON."""

    def export(self, spans):
        import sys

        from opentelemetry.sdk.trace.export import SpanExportResult

        for s in spans:
            line = _format_span_compact(s)
            if line is not None:
                print(line, file=sys.stderr, flush=True)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


_initialised = False


def bootstrap(cfg: ObservabilityConfig) -> None:
    """
    Configure the global OTel TracerProvider. Idempotent — safe to call twice
    (subsequent calls are a no-op; avoids double-init under uvicorn --reload).
    """
    global _initialised
    if _initialised:
        return

    if not cfg.enabled:
        log.info("observability disabled (cfg.observability.enabled=false)")
        _initialised = True
        return

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

    resource = Resource.create({"service.name": cfg.service_name})
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBasedTraceIdRatio(cfg.sample_ratio),
    )

    # ── External exporter ──
    if cfg.exporter == "stdout_compact":
        provider.add_span_processor(BatchSpanProcessor(_CompactSpanExporter()))
        log.info("observability: stdout_compact exporter active")
    elif cfg.exporter == "stdout":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        log.info("observability: stdout exporter active (full JSON)")
    elif cfg.exporter == "otlp":
        if not cfg.otlp_endpoint:
            raise ValueError("observability.exporter=otlp requires otlp_endpoint")
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=cfg.otlp_endpoint,
                    headers=cfg.otlp_headers or {},
                )
            )
        )
        log.info("observability: OTLP exporter → %s", cfg.otlp_endpoint)
    else:  # "none"
        log.info("observability: no external exporter (per-request collector only)")

    # ── Per-request collector (for /query trace payload) ──
    from retrieval.telemetry import RequestSpanCollector

    provider.add_span_processor(RequestSpanCollector.singleton())

    trace.set_tracer_provider(provider)

    # ── LiteLLM → OTel bridge ──
    # LiteLLM emits spans on every completion/embedding call with token +
    # cost + model attributes. One-line hookup, no extra pip package needed.
    try:
        import litellm

        litellm.success_callback = list(set([*list(litellm.success_callback or []), "otel"]))
        litellm.failure_callback = list(set([*list(litellm.failure_callback or []), "otel"]))
        log.info("observability: LiteLLM OTel callback enabled")
    except Exception as e:
        log.warning("could not enable LiteLLM OTel callback: %s", e)

    _initialised = True


def instrument_app(app) -> None:
    """
    Wire auto-instrumentation onto the running FastAPI app. Call AFTER
    bootstrap() so the global tracer provider is ready.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    log.info("auto-instrumentation: FastAPI + HTTPX")


def instrument_sqlalchemy(engine) -> None:
    """
    Wire SQLAlchemy instrumentation on a specific engine. Called from
    Store.connect() so we only instrument the engine we actually use.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine)
    log.info("auto-instrumentation: SQLAlchemy on %s", engine.url.drivername)
