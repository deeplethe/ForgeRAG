"""
Aggregate metrics over existing persistence tables.

Readonly dashboard backing — no new tables. Aggregates come from:

  * ``query_traces``  — one row per served query, ``trace_json`` holds the
    full OTel span tree from which we mine per-path timings + LLM tokens.
  * ``documents``     — ingestion status + per-phase timestamps + error_message.

Postgres-native SQL is used for histogram buckets and percentiles; SQLite is
only supported for test code paths, so we return HTTP 503 if the active
backend isn't Postgres.

    GET  /api/v1/metrics/summary?range=24h
    GET  /api/v1/metrics/query/latency?range=24h
    GET  /api/v1/metrics/query/tokens?range=24h
    GET  /api/v1/metrics/query/path-timing?range=24h
    GET  /api/v1/metrics/query/slow?range=24h&limit=10
    GET  /api/v1/metrics/ingestion/recent-failures?limit=10
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from ..deps import get_state
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


# ---------------------------------------------------------------------------
# Range / bucket resolution
# ---------------------------------------------------------------------------

RangeKey = Literal["24h", "7d", "30d"]

# Each range maps to a bucket size for time-series charts. Picking a bucket
# size that yields ~50-100 points keeps the charts readable without needing
# client-side resampling.
_RANGE_TO_BUCKET = {
    "24h": ("15 minutes", timedelta(hours=24)),
    "7d": ("1 hour", timedelta(days=7)),
    "30d": ("6 hours", timedelta(days=30)),
}


def _range_cutoff(rng: RangeKey) -> datetime:
    _, delta = _RANGE_TO_BUCKET[rng]
    return datetime.now(timezone.utc) - delta


def _bucket_width(rng: RangeKey) -> str:
    return _RANGE_TO_BUCKET[rng][0]


def _require_postgres(state: AppState) -> None:
    """Metrics queries rely on Postgres JSON operators + percentile_cont."""
    dialect = state.store._sessionmaker.kw["bind"].dialect.name if state.store._sessionmaker else "?"
    if dialect != "postgresql":
        raise HTTPException(
            503,
            detail=f"metrics endpoints require a Postgres backend (got {dialect!r})",
        )


# ---------------------------------------------------------------------------
# Span walkers  — extract per-path timings / token usage from trace_json
# ---------------------------------------------------------------------------

_PATH_SPANS = {
    "qu": ("forgerag.query_understanding",),
    "bm25": ("forgerag.bm25_path",),
    "vector": ("forgerag.vector_path",),
    "tree": ("forgerag.tree_path",),
    "kg": ("forgerag.kg_path",),
    "rrf": ("forgerag.rrf_merge",),
    "expand": ("forgerag.expansion",),
    "rerank": ("forgerag.rerank",),
}

_LLM_MODEL_ATTR_KEYS = ("gen_ai.request.model", "llm.model", "gen_ai.response.model")
_PROMPT_TOK_KEYS = ("gen_ai.usage.prompt_tokens", "llm.prompt_tokens", "gen_ai.usage.input_tokens")
_COMPLETION_TOK_KEYS = ("gen_ai.usage.completion_tokens", "llm.completion_tokens", "gen_ai.usage.output_tokens")


def _first_attr(attrs: dict, keys: tuple) -> Any | None:
    for k in keys:
        v = attrs.get(k)
        if v is not None:
            return v
    return None


def _span_duration_ms(span: dict) -> float:
    if span.get("duration_ms") is not None:
        return float(span["duration_ms"])
    try:
        return (int(span["end_time_unix_nano"]) - int(span["start_time_unix_nano"])) / 1e6
    except Exception:
        return 0.0


def _extract_per_path_ms(spans: list[dict]) -> dict[str, float]:
    """Sum duration per canonical path key (bm25/vector/…)."""
    out: dict[str, float] = {}
    if not spans:
        return out
    for key, names in _PATH_SPANS.items():
        for sp in spans:
            if sp.get("name") in names:
                out[key] = _span_duration_ms(sp)
                break
    return out


def _extract_llm_usage(spans: list[dict]) -> dict[str, dict[str, int]]:
    """Map model → {prompt_tokens, completion_tokens, calls}."""
    out: dict[str, dict[str, int]] = {}
    for sp in spans or []:
        attrs = sp.get("attributes") or {}
        model = _first_attr(attrs, _LLM_MODEL_ATTR_KEYS)
        if not model:
            continue
        p = _first_attr(attrs, _PROMPT_TOK_KEYS)
        c = _first_attr(attrs, _COMPLETION_TOK_KEYS)
        if p is None and c is None:
            continue
        b = out.setdefault(model, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
        b["prompt_tokens"] += int(p or 0)
        b["completion_tokens"] += int(c or 0)
        b["calls"] += 1
    return out


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MetricsSummary(BaseModel):
    range: RangeKey
    queries: int
    p50_ms: float | None = None
    p95_ms: float | None = None
    queries_per_hour: float
    tokens_total: int
    ingest_ok: int
    ingest_failed: int
    ingest_in_progress: int


class LatencyPoint(BaseModel):
    ts: datetime
    p50_ms: float | None = None
    p95_ms: float | None = None
    count: int


class TokensPoint(BaseModel):
    ts: datetime
    model: str
    prompt_tokens: int
    completion_tokens: int


class PathTiming(BaseModel):
    key: str
    label: str
    avg_ms: float
    p95_ms: float
    samples: int


class SlowQuery(BaseModel):
    trace_id: str
    ts: datetime
    query: str
    total_ms: int
    answer_model: str | None
    citations: int


class IngestionFailure(BaseModel):
    doc_id: str
    file_name: str | None
    folder_path: str | None
    format: str | None
    error_message: str | None
    ts: datetime


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=MetricsSummary)
def metrics_summary(
    range: RangeKey = Query("24h"),
    state: AppState = Depends(get_state),
):
    _require_postgres(state)
    cutoff = _range_cutoff(range)
    with state.store._session() as s:
        # Queries + percentiles in one round-trip
        row = s.execute(
            text(
                """
                SELECT
                  COUNT(*)                                                           AS n,
                  percentile_cont(0.5)  WITHIN GROUP (ORDER BY total_ms)             AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)             AS p95
                FROM query_traces
                WHERE timestamp >= :cutoff
                """
            ),
            {"cutoff": cutoff},
        ).one()
        n = row.n or 0
        p50 = float(row.p50) if row.p50 is not None else None
        p95 = float(row.p95) if row.p95 is not None else None

        # Ingest outcome buckets
        ing = s.execute(
            text(
                """
                SELECT
                  COUNT(*) FILTER (WHERE status = 'ready')                           AS ok,
                  COUNT(*) FILTER (WHERE status = 'error')                           AS failed,
                  COUNT(*) FILTER (WHERE status IS NOT NULL
                                   AND status NOT IN ('ready', 'error'))             AS in_progress
                FROM documents
                WHERE COALESCE(updated_at, created_at) >= :cutoff
                """
            ),
            {"cutoff": cutoff},
        ).one()

        # Token totals — walk spans in a bounded window
        tokens_total = 0
        rows = s.execute(
            text("SELECT trace_json FROM query_traces WHERE timestamp >= :cutoff LIMIT 2000"),
            {"cutoff": cutoff},
        ).all()
        for (tj,) in rows:
            usage = _extract_llm_usage((tj or {}).get("spans") or [])
            for m in usage.values():
                tokens_total += m["prompt_tokens"] + m["completion_tokens"]

    _, delta = _RANGE_TO_BUCKET[range]
    hours = delta.total_seconds() / 3600.0
    return MetricsSummary(
        range=range,
        queries=n,
        p50_ms=p50,
        p95_ms=p95,
        queries_per_hour=(n / hours) if hours else 0,
        tokens_total=tokens_total,
        ingest_ok=ing.ok or 0,
        ingest_failed=ing.failed or 0,
        ingest_in_progress=ing.in_progress or 0,
    )


@router.get("/query/latency", response_model=list[LatencyPoint])
def query_latency(
    range: RangeKey = Query("24h"),
    state: AppState = Depends(get_state),
):
    _require_postgres(state)
    cutoff = _range_cutoff(range)
    bucket = _bucket_width(range)
    with state.store._session() as s:
        rows = s.execute(
            text(
                f"""
                SELECT
                  date_bin(INTERVAL '{bucket}', timestamp, TIMESTAMPTZ '2000-01-01') AS bucket,
                  COUNT(*)                                                           AS n,
                  percentile_cont(0.5)  WITHIN GROUP (ORDER BY total_ms)             AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)             AS p95
                FROM query_traces
                WHERE timestamp >= :cutoff
                GROUP BY bucket
                ORDER BY bucket
                """
            ),
            {"cutoff": cutoff},
        ).all()
    return [
        LatencyPoint(
            ts=r.bucket,
            count=r.n or 0,
            p50_ms=float(r.p50) if r.p50 is not None else None,
            p95_ms=float(r.p95) if r.p95 is not None else None,
        )
        for r in rows
    ]


@router.get("/query/tokens", response_model=list[TokensPoint])
def query_tokens(
    range: RangeKey = Query("24h"),
    state: AppState = Depends(get_state),
):
    _require_postgres(state)
    cutoff = _range_cutoff(range)
    bucket = _bucket_width(range)
    with state.store._session() as s:
        rows = s.execute(
            text(
                f"""
                SELECT
                  date_bin(INTERVAL '{bucket}', timestamp, TIMESTAMPTZ '2000-01-01') AS bucket,
                  trace_json
                FROM query_traces
                WHERE timestamp >= :cutoff
                LIMIT 5000
                """
            ),
            {"cutoff": cutoff},
        ).all()

    # Fold JSON client-side: (bucket, model) → tokens
    agg: dict[tuple[datetime, str], dict[str, int]] = {}
    for r in rows:
        usage = _extract_llm_usage((r.trace_json or {}).get("spans") or [])
        for model, tok in usage.items():
            key = (r.bucket, model)
            box = agg.setdefault(key, {"prompt_tokens": 0, "completion_tokens": 0})
            box["prompt_tokens"] += tok["prompt_tokens"]
            box["completion_tokens"] += tok["completion_tokens"]
    points = [TokensPoint(ts=ts, model=model, **tok) for (ts, model), tok in agg.items()]
    points.sort(key=lambda p: (p.ts, p.model))
    return points


@router.get("/query/path-timing", response_model=list[PathTiming])
def query_path_timing(
    range: RangeKey = Query("24h"),
    state: AppState = Depends(get_state),
):
    _require_postgres(state)
    cutoff = _range_cutoff(range)
    with state.store._session() as s:
        rows = s.execute(
            text("SELECT trace_json FROM query_traces WHERE timestamp >= :cutoff LIMIT 2000"),
            {"cutoff": cutoff},
        ).all()

    buckets: dict[str, list[float]] = {k: [] for k in _PATH_SPANS}
    for (tj,) in rows:
        per = _extract_per_path_ms((tj or {}).get("spans") or [])
        for k, v in per.items():
            if v > 0:
                buckets[k].append(v)

    out: list[PathTiming] = []
    labels = {
        "qu": "QU",
        "bm25": "BM25",
        "vector": "Vector",
        "kg": "KG",
        "tree": "Tree",
        "rrf": "RRF",
        "expand": "Expand",
        "rerank": "Rerank",
    }
    for key, samples in buckets.items():
        if not samples:
            continue
        samples.sort()
        n = len(samples)
        avg = sum(samples) / n
        p95 = samples[min(n - 1, int(0.95 * n))]
        out.append(PathTiming(key=key, label=labels.get(key, key), avg_ms=avg, p95_ms=p95, samples=n))
    # Stable order matching the Simulation page
    order = list(_PATH_SPANS.keys())
    out.sort(key=lambda p: order.index(p.key))
    return out


@router.get("/query/slow")
def query_slow(
    range: RangeKey = Query("24h"),
    limit: int = Query(10, ge=1, le=50),
    state: AppState = Depends(get_state),
):
    log.info("query_slow invoked: range=%s limit=%s", range, limit)
    _require_postgres(state)
    cutoff = _range_cutoff(range)
    try:
        with state.store._session() as s:
            rows = s.execute(
                text(
                    """
                    SELECT trace_id, timestamp, query, total_ms, answer_model, citations_used
                    FROM query_traces
                    WHERE timestamp >= :cutoff
                    ORDER BY total_ms DESC
                    LIMIT :lim
                    """
                ),
                {"cutoff": cutoff, "lim": limit},
            ).all()
    except Exception as e:
        log.exception("query_slow DB failed")
        raise HTTPException(500, detail=f"db: {type(e).__name__}: {e}")

    out = []
    try:
        for r in rows:
            cits = r.citations_used
            if isinstance(cits, str):
                import json as _json

                try:
                    cits = _json.loads(cits)
                except Exception:
                    cits = []
            ts_val = r.timestamp
            out.append(
                {
                    "trace_id": r.trace_id,
                    "ts": ts_val.isoformat() if ts_val is not None else None,
                    "query": (r.query or "")[:300],
                    "total_ms": r.total_ms or 0,
                    "answer_model": r.answer_model,
                    "citations": len(cits) if isinstance(cits, list) else 0,
                }
            )
    except Exception as e:
        log.exception("query_slow serialize failed")
        raise HTTPException(500, detail=f"ser: {type(e).__name__}: {e}")
    return out


@router.get("/ingestion/recent-failures", response_model=list[IngestionFailure])
def ingestion_recent_failures(
    limit: int = Query(10, ge=1, le=50),
    state: AppState = Depends(get_state),
):
    _require_postgres(state)
    with state.store._session() as s:
        rows = s.execute(
            text(
                """
                SELECT doc_id, filename, path, format, error_message,
                       COALESCE(updated_at, created_at) AS ts
                FROM documents
                WHERE status = 'error'
                ORDER BY ts DESC
                LIMIT :lim
                """
            ),
            {"lim": limit},
        ).all()
    return [
        IngestionFailure(
            doc_id=r.doc_id,
            file_name=r.filename,
            folder_path=r.path,
            format=r.format,
            error_message=r.error_message,
            ts=r.ts,
        )
        for r in rows
    ]
