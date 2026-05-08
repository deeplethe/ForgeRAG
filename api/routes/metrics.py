"""
Aggregate metrics over existing persistence tables.

Readonly dashboard backing — no new tables. Aggregates come from:

  * ``query_traces``  — one row per served query, ``trace_json`` holds the
    full OTel span tree from which we mine per-path timings + LLM tokens.
  * ``documents``     — ingestion status + per-phase timestamps + error_message.

All aggregation is computed in Python on top of plain SELECTs so the routes
work uniformly across SQLite / Postgres / DuckDB / etc. The percentile and
time-bucket SQL we used to use (``percentile_cont WITHIN GROUP``,
``date_bin``, ``COUNT(*) FILTER``) is Postgres-only; rewriting in Python
costs ~ms per few-thousand rows — fine for the dashboard window sizes
(24h / 7d / 30d) we expose. If anyone ever runs OpenCraig at the scale
where 30-day rollups are too slow in Python, switching back to SQL on
the Postgres-only fast path is a single dialect check.

    GET  /api/v1/metrics/summary?range=24h
    GET  /api/v1/metrics/query/latency?range=24h
    GET  /api/v1/metrics/query/tokens?range=24h
    GET  /api/v1/metrics/query/path-timing?range=24h
    GET  /api/v1/metrics/query/slow?range=24h&limit=10
    GET  /api/v1/metrics/ingestion/recent-failures?limit=10
"""

from __future__ import annotations

import json
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
# client-side resampling. ``timedelta`` (not raw strings) so we can do
# Python-side bucketing without parsing.
_RANGE_TO_BUCKET: dict[str, tuple[timedelta, timedelta]] = {
    "24h": (timedelta(minutes=15), timedelta(hours=24)),
    "7d": (timedelta(hours=1), timedelta(days=7)),
    "30d": (timedelta(hours=6), timedelta(days=30)),
}

# Bucket-snap anchor — every bucket boundary is anchor + k * bucket_size.
# Picking a fixed past-anchor (instead of "now") so the same wall-clock
# time always lands in the same bucket across requests.
_BUCKET_ANCHOR = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _range_cutoff(rng: RangeKey) -> datetime:
    _, delta = _RANGE_TO_BUCKET[rng]
    return datetime.now(timezone.utc) - delta


def _bucket_size(rng: RangeKey) -> timedelta:
    return _RANGE_TO_BUCKET[rng][0]


def _bucket_ts(ts: datetime, bucket: timedelta) -> datetime:
    """Snap ``ts`` down to the nearest ``bucket`` boundary, anchor-relative.

    Naive datetimes (SQLite returns these from ``DateTime`` columns) are
    treated as UTC. Returns a tz-aware UTC datetime for consistent JSON
    serialisation with Postgres TIMESTAMPTZ columns.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta_s = (ts - _BUCKET_ANCHOR).total_seconds()
    bucket_s = bucket.total_seconds()
    n = int(delta_s // bucket_s)
    return _BUCKET_ANCHOR + timedelta(seconds=n * bucket_s)


def _percentiles(values: list[float], qs: list[float]) -> list[float | None]:
    """Linear-interpolation percentiles, semantics matching Postgres
    ``percentile_cont`` (a.k.a. numpy ``linear`` method): for a sorted
    sample of length n, the q-th percentile sits at fractional index
    q*(n-1) and is interpolated between the two neighbouring ranks.

    Returns ``None`` for any q when ``values`` is empty so callers can
    bind the result straight into nullable response fields.
    """
    if not values:
        return [None] * len(qs)
    sv = sorted(values)
    n = len(sv)
    out: list[float | None] = []
    for q in qs:
        if n == 1:
            out.append(float(sv[0]))
            continue
        pos = q * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        out.append(float(sv[lo] + frac * (sv[hi] - sv[lo])))
    return out


# ---------------------------------------------------------------------------
# Per-dialect raw-row coercion
#
# Raw ``text()`` SQL bypasses SQLAlchemy's type marshaling, so JSON and
# DateTime columns come back in whatever representation the underlying
# DB driver hands over: dict on Postgres (JSONB native), str on SQLite
# (JSON-as-TEXT). Same story for DateTime — Postgres returns datetime,
# SQLite returns ISO string. The ORM ``Mapped`` types only kick in
# through ``session.execute(select(Model))``, not through ``text(...)``.
# ---------------------------------------------------------------------------


def _coerce_dict(v: Any) -> dict:
    if v is None or v == "":
        return {}
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


def _coerce_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            # ``fromisoformat`` doesn't accept the trailing ``Z`` shorthand
            # before 3.11; normalise just in case the SQLite driver emits it.
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Span walkers  — extract per-path timings / token usage from trace_json
# ---------------------------------------------------------------------------

_PATH_SPANS = {
    "qu": ("opencraig.query_understanding",),
    "bm25": ("opencraig.bm25_path",),
    "vector": ("opencraig.vector_path",),
    "tree": ("opencraig.tree_path",),
    "kg": ("opencraig.kg_path",),
    "rrf": ("opencraig.rrf_merge",),
    "expand": ("opencraig.expansion",),
    "rerank": ("opencraig.rerank",),
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
    cutoff = _range_cutoff(range)
    with state.store._session() as s:
        # Pull the columns we need; aggregate in Python so the route
        # works on any backend (no percentile_cont, no FILTER).
        trace_rows = s.execute(
            text("SELECT total_ms, trace_json FROM query_traces WHERE timestamp >= :cutoff"),
            {"cutoff": cutoff},
        ).all()

        ing_rows = s.execute(
            text("SELECT status FROM documents WHERE COALESCE(updated_at, created_at) >= :cutoff"),
            {"cutoff": cutoff},
        ).all()

    durations = [float(r.total_ms) for r in trace_rows if r.total_ms is not None]
    p50, p95 = _percentiles(durations, [0.5, 0.95])

    tokens_total = 0
    for r in trace_rows:
        usage = _extract_llm_usage(_coerce_dict(r.trace_json).get("spans") or [])
        for m in usage.values():
            tokens_total += m["prompt_tokens"] + m["completion_tokens"]

    ok = sum(1 for r in ing_rows if r.status == "ready")
    failed = sum(1 for r in ing_rows if r.status == "error")
    in_progress = sum(1 for r in ing_rows if r.status not in (None, "ready", "error"))

    n = len(trace_rows)
    _, delta = _RANGE_TO_BUCKET[range]
    hours = delta.total_seconds() / 3600.0
    return MetricsSummary(
        range=range,
        queries=n,
        p50_ms=p50,
        p95_ms=p95,
        queries_per_hour=(n / hours) if hours else 0,
        tokens_total=tokens_total,
        ingest_ok=ok,
        ingest_failed=failed,
        ingest_in_progress=in_progress,
    )


@router.get("/query/latency", response_model=list[LatencyPoint])
def query_latency(
    range: RangeKey = Query("24h"),
    state: AppState = Depends(get_state),
):
    cutoff = _range_cutoff(range)
    bucket = _bucket_size(range)
    with state.store._session() as s:
        rows = s.execute(
            text("SELECT timestamp, total_ms FROM query_traces WHERE timestamp >= :cutoff"),
            {"cutoff": cutoff},
        ).all()

    by_bucket: dict[datetime, list[float]] = {}
    for r in rows:
        ts = _coerce_dt(r.timestamp)
        if r.total_ms is None or ts is None:
            continue
        b = _bucket_ts(ts, bucket)
        by_bucket.setdefault(b, []).append(float(r.total_ms))

    out: list[LatencyPoint] = []
    for ts in sorted(by_bucket):
        vals = by_bucket[ts]
        p50, p95 = _percentiles(vals, [0.5, 0.95])
        out.append(LatencyPoint(ts=ts, count=len(vals), p50_ms=p50, p95_ms=p95))
    return out


@router.get("/query/tokens", response_model=list[TokensPoint])
def query_tokens(
    range: RangeKey = Query("24h"),
    state: AppState = Depends(get_state),
):
    cutoff = _range_cutoff(range)
    bucket = _bucket_size(range)
    with state.store._session() as s:
        rows = s.execute(
            text("SELECT timestamp, trace_json FROM query_traces WHERE timestamp >= :cutoff LIMIT 5000"),
            {"cutoff": cutoff},
        ).all()

    # Fold (bucket, model) → tokens client-side. We were doing the JSON
    # walk in Python anyway; the bucket assignment is one extra op per row.
    agg: dict[tuple[datetime, str], dict[str, int]] = {}
    for r in rows:
        ts = _coerce_dt(r.timestamp)
        if ts is None:
            continue
        b = _bucket_ts(ts, bucket)
        usage = _extract_llm_usage(_coerce_dict(r.trace_json).get("spans") or [])
        for model, tok in usage.items():
            key = (b, model)
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
    cutoff = _range_cutoff(range)
    with state.store._session() as s:
        rows = s.execute(
            text("SELECT trace_json FROM query_traces WHERE timestamp >= :cutoff LIMIT 2000"),
            {"cutoff": cutoff},
        ).all()

    buckets: dict[str, list[float]] = {k: [] for k in _PATH_SPANS}
    for (tj,) in rows:
        per = _extract_per_path_ms(_coerce_dict(tj).get("spans") or [])
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
            ts_val = _coerce_dt(r.timestamp)
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
