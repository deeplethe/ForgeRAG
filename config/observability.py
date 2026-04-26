"""
Observability / OpenTelemetry bootstrap.

ForgeRAG emits OTel spans for every retrieval phase, every LLM call, every
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
    service_name: str = "forgerag"
    exporter: Literal["stdout", "otlp", "none"] = Field(
        "stdout",
        description=(
            "Where to send finished spans. ``stdout`` prints to the server "
            "console (zero setup). ``otlp`` sends via OTLP/HTTP to "
            "``otlp_endpoint`` (point at Langfuse / Jaeger / Phoenix / etc.). "
            "``none`` disables external export (the per-request collector "
            "still runs so the /query route can return trace JSON)."
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
        1.0, ge=0.0, le=1.0,
        description="Parent-based sampling ratio. Lower to reduce volume in prod.",
    )


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
    if cfg.exporter == "stdout":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        log.info("observability: stdout exporter active")
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
        litellm.success_callback = list(set(list(litellm.success_callback or []) + ["otel"]))
        litellm.failure_callback = list(set(list(litellm.failure_callback or []) + ["otel"]))
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
