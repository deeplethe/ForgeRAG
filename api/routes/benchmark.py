"""
Benchmark API endpoints.

    POST   /api/v1/benchmark/start    — start a benchmark run
    POST   /api/v1/benchmark/cancel   — cancel a running benchmark
    GET    /api/v1/benchmark/status   — poll current status / results
    GET    /api/v1/benchmark/report   — download full report as JSON
    GET    /api/v1/benchmark/reports  — list saved reports for replay

ALL endpoints are admin-only. A benchmark run consumes LLM quota
and writes per-question doc_ids + ground_truths from the corpus
to ``benchmark_results/`` — both the cost and the content are
admin concerns. Auth-disabled single-user deployments synthesise a
local-admin principal so the gate is a passthrough there.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from benchmark.report import build_report
from benchmark.runner import BenchmarkRunner

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state, require_admin
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/benchmark", tags=["benchmark"])

_runner = BenchmarkRunner()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BenchmarkStartRequest(BaseModel):
    num_questions: int = Field(default=30, ge=5, le=200)
    # Optional: replay questions from a previously saved report. When set,
    # num_questions is ignored and the run uses the exact questions (and
    # ground_truths) from that report — enables strict A/B comparison.
    # Value is either a run_id (looked up from benchmark_results/) or an
    # inline list of items [{question, ground_truth, doc_id, doc_title}, ...].
    replay_from_run_id: str | None = None
    replay_items: list[dict] | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/start")
def start_benchmark(
    req: BenchmarkStartRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    require_admin(state, principal)
    if _runner.running:
        raise HTTPException(409, "A benchmark is already running")

    replay_items = req.replay_items
    if replay_items is None and req.replay_from_run_id:
        replay_items = _load_replay_from_disk(req.replay_from_run_id)
        if not replay_items:
            raise HTTPException(
                404,
                f"No saved report found for run_id {req.replay_from_run_id!r}. Check benchmark_results/ directory.",
            )

    try:
        run_id = _runner.start(
            cfg=state.cfg,
            store=state.store,
            answering=state.answering,
            num_questions=req.num_questions,
            replay_items=replay_items,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"run_id": run_id}


def _load_replay_from_disk(run_id: str) -> list[dict] | None:
    """Look for bench_<run_id>.json in benchmark_results/ and extract the
    question / ground_truth / doc_id / doc_title fields from its items."""
    from pathlib import Path

    safe_id = "".join(c for c in run_id if c.isalnum() or c in ("-", "_"))
    if not safe_id:
        return None
    results_dir = Path("benchmark_results")
    candidate = results_dir / f"bench_{safe_id}.json"
    if not candidate.is_file():
        return None
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except Exception:
        return None
    items = data.get("items") or []
    out: list[dict] = []
    for it in items:
        q = it.get("question")
        if not q:
            continue
        out.append(
            {
                "question": q,
                "ground_truth": it.get("ground_truth", "") or "",
                "doc_id": it.get("doc_id", "") or "",
                "doc_title": it.get("doc_title", "") or "",
            }
        )
    return out or None


@router.get("/reports")
def list_reports(
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
) -> dict:
    """List available saved report files for replay. Returns just the run_id
    and some summary metrics so the UI can populate a 'replay from...' dropdown."""
    require_admin(state, principal)
    from pathlib import Path

    results_dir = Path("benchmark_results")
    out: list[dict] = []
    if results_dir.is_dir():
        for f in sorted(results_dir.glob("bench_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            br = data.get("benchmark_report") or {}
            scores = br.get("scores") or data.get("scores") or {}
            items = data.get("items") or []
            run_id = (br.get("run_id") or f.stem.replace("bench_", "") or "?")[:16]
            out.append(
                {
                    "run_id": run_id,
                    "num_items": len(items),
                    "faithfulness": scores.get("faithfulness"),
                    "answer_relevancy": scores.get("answer_relevancy"),
                    "context_precision": scores.get("context_precision"),
                    "mtime": f.stat().st_mtime,
                    "filename": f.name,
                }
            )
    return {"reports": out}


@router.post("/cancel")
def cancel_benchmark(
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    require_admin(state, principal)
    _runner.cancel()
    return {"ok": True}


@router.get("/status")
def benchmark_status(
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    require_admin(state, principal)
    return _runner.get_status()


@router.get("/report")
def download_report(
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    require_admin(state, principal)
    status = _runner.get_status()
    if status["status"] != "done":
        raise HTTPException(400, "Benchmark not complete yet")
    report = build_report(status)
    content = json.dumps(report, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="forgerag_benchmark_{status["run_id"]}.json"',
        },
    )
