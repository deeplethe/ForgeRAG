"""
Benchmark runner — orchestrates testset generation, QA execution, and scoring.

Runs in a background thread; exposes a singleton so only one benchmark
can execute at a time.  The API layer polls `get_status()` for progress.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkItem:
    """Single QA evaluation item."""

    idx: int
    question: str
    ground_truth: str = ""
    doc_id: str = ""
    doc_title: str = ""
    # Filled after QA execution
    answer: str = ""
    contexts: list[str] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    latency_ms: int = 0
    # Per-retrieval-path top-5 chunk ids — lets us answer "which path found
    # the right chunk?" when investigating CP=0 items post-hoc.
    path_top_ids: dict[str, list[str]] = field(default_factory=dict)
    # Filled after scoring
    faithfulness: float | None = None
    relevancy: float | None = None
    context_precision: float | None = None
    error: str = ""


@dataclass
class BenchmarkStatus:
    """Full state snapshot returned to the frontend."""

    run_id: str = ""
    status: str = "idle"  # idle | generating | running | scoring | done | cancelled | error
    phase: str = ""  # human-readable phase label
    total: int = 0
    completed: int = 0
    started_at: float = 0.0
    elapsed_ms: int = 0
    estimated_remaining_ms: int = 0
    # Scores (populated when done)
    scores: dict[str, Any] = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)
    config_snapshot: dict = field(default_factory=dict)
    error: str = ""
    # Method description
    method: str = (
        "Benchmark generates questions from your ingested documents using an LLM, "
        "then runs each question through the full retrieval + generation pipeline. "
        "Answers are scored by an LLM judge on three dimensions:\n"
        "• Faithfulness — does the answer only use information from retrieved context?\n"
        "• Answer Relevancy — does the answer address the question?\n"
        "• Context Precision — are the retrieved chunks relevant to the question?"
    )

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Singleton runner
# ---------------------------------------------------------------------------


class BenchmarkRunner:
    """Singleton benchmark executor.  Thread-safe status access."""

    _instance: BenchmarkRunner | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = BenchmarkStatus()

    # -- public API --

    @property
    def running(self) -> bool:
        return self._status.status in ("generating", "running", "scoring")

    def get_status(self) -> dict:
        with self._lock:
            s = self._status
            if s.started_at and self.running:
                s.elapsed_ms = int((time.time() - s.started_at) * 1000)
            return s.to_dict()

    def start(
        self,
        *,
        cfg,
        store,
        answering,
        num_questions: int = 30,
        replay_items: list | None = None,
    ):
        """
        Start a benchmark run.

        Parameters
        ----------
        replay_items
            If provided, skip the question-generation phase and reuse these
            questions verbatim (typically loaded from a prior run's report).
            Each entry is a dict with at least {question, ground_truth,
            doc_id, doc_title} — i.e. the BenchmarkItem dict shape. Lets
            users perform strict A/B comparisons (same questions, changed
            config) instead of sampling new questions each time.
        """
        with self._lock:
            if self.running:
                raise RuntimeError("A benchmark is already running")
            self._cancel.clear()
            initial_total = len(replay_items) if replay_items else num_questions
            self._status = BenchmarkStatus(
                run_id=uuid.uuid4().hex[:12],
                status="generating" if not replay_items else "running",
                phase=(
                    f"Replaying {initial_total} questions from prior run…"
                    if replay_items
                    else "Generating test questions from documents…"
                ),
                total=initial_total,
                started_at=time.time(),
            )
        self._thread = threading.Thread(
            target=self._run,
            args=(cfg, store, answering, num_questions),
            kwargs={"replay_items": replay_items},
            daemon=True,
        )
        self._thread.start()
        return self._status.run_id

    def cancel(self):
        if self.running:
            self._cancel.set()

    # -- internal --

    def _update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self._status, k, v)
            if self._status.started_at:
                self._status.elapsed_ms = int((time.time() - self._status.started_at) * 1000)

    def _estimate_remaining(self, completed: int, total: int):
        if completed <= 0:
            return 0
        elapsed = time.time() - self._status.started_at
        per_item = elapsed / completed
        return int((total - completed) * per_item * 1000)

    def _run(self, cfg, store, answering, num_questions: int, *, replay_items: list | None = None):
        from .metrics import score_items
        from .report import sanitize_config
        from .testset import generate_testset

        items: list[BenchmarkItem] = []
        try:
            # ── Phase 1: generate test questions (skipped on replay) ──
            if replay_items is not None:
                log.info("benchmark: replaying %d questions from prior run", len(replay_items))
                items = [
                    BenchmarkItem(
                        idx=i,
                        question=(it.get("question") if isinstance(it, dict) else getattr(it, "question", "")),
                        ground_truth=(
                            it.get("ground_truth", "") if isinstance(it, dict) else getattr(it, "ground_truth", "")
                        ),
                        doc_id=(it.get("doc_id", "") if isinstance(it, dict) else getattr(it, "doc_id", "")),
                        doc_title=(it.get("doc_title", "") if isinstance(it, dict) else getattr(it, "doc_title", "")),
                    )
                    for i, it in enumerate(replay_items)
                ]
            else:
                log.info("benchmark: generating %d test questions", num_questions)
                items = generate_testset(
                    store=store,
                    cfg=cfg,
                    num_questions=num_questions,
                    cancel=self._cancel,
                    progress_cb=lambda done, total: self._update(
                        completed=done,
                        total=total,
                        phase=f"Generating questions ({done}/{total})…",
                        estimated_remaining_ms=self._estimate_remaining(done, total),
                    ),
                )
            if self._cancel.is_set():
                self._update(status="cancelled", phase="Cancelled")
                return

            actual_total = len(items)
            self._update(
                status="running",
                phase="Running queries…",
                total=actual_total,
                completed=0,
            )

            # ── Phase 2: execute QA for each item ──
            for i, item in enumerate(items):
                if self._cancel.is_set():
                    self._update(status="cancelled", phase="Cancelled")
                    return
                try:
                    t0 = time.time()
                    answer = answering.ask(item.question)
                    item.latency_ms = int((time.time() - t0) * 1000)
                    item.answer = answer.text
                    # Capture both raw chunk snippets AND the synthesized KG
                    # context (entities + relations injected into the prompt).
                    # The LLM judge needs to see the full prompt input;
                    # otherwise answers grounded in KG synthesis look like
                    # hallucinations and faithfulness / context_precision
                    # are under-reported.
                    item.contexts = [c.snippet or "" for c in answer.citations_all if c.snippet]
                    kg_ctx = (answer.stats or {}).get("kg_context") or {}
                    for e in kg_ctx.get("entities", []):
                        desc = (e.get("description") or "").strip()
                        if desc:
                            item.contexts.append(f"[KG entity] {e.get('name', '')}: {desc}")
                    for r in kg_ctx.get("relations", []):
                        desc = (r.get("description") or "").strip()
                        if desc:
                            item.contexts.append(f"[KG relation] {r.get('source', '')} → {r.get('target', '')}: {desc}")
                    item.citations = [
                        {"citation_id": c.citation_id, "doc_id": c.doc_id, "page_no": c.page_no, "snippet": c.snippet}
                        for c in answer.citations_used
                    ]
                    # Capture per-path top-5 chunk ids for attribution analysis
                    # (e.g. "for CP=0 items, which path actually had the GT chunk?")
                    stats_dict = answer.stats or {}
                    for _k in ("bm25_top_ids", "vector_top_ids", "tree_top_ids", "kg_top_ids"):
                        v = stats_dict.get(_k)
                        if v:
                            item.path_top_ids[_k.replace("_top_ids", "")] = list(v)
                except Exception as e:
                    item.error = str(e)
                    log.warning("benchmark item %d failed: %s", i, e)

                self._update(
                    completed=i + 1,
                    phase=f"Running queries ({i + 1}/{actual_total})…",
                    estimated_remaining_ms=self._estimate_remaining(i + 1, actual_total),
                )

            # ── Phase 3: score answers ──
            if self._cancel.is_set():
                self._update(status="cancelled", phase="Cancelled")
                return

            self._update(
                status="scoring",
                phase="Scoring answers with LLM judge…",
                completed=0,
                total=actual_total,
            )
            score_items(
                items=items,
                cfg=cfg,
                cancel=self._cancel,
                progress_cb=lambda done, total: self._update(
                    completed=done,
                    total=total,
                    phase=f"Scoring ({done}/{total})…",
                    estimated_remaining_ms=self._estimate_remaining(done, total),
                ),
            )

            if self._cancel.is_set():
                self._update(status="cancelled", phase="Cancelled")
                return

            # ── Phase 4: aggregate ──
            scored = [it for it in items if it.faithfulness is not None]
            n = max(len(scored), 1)
            scores = {
                "faithfulness": round(sum(it.faithfulness or 0 for it in scored) / n, 3),
                "answer_relevancy": round(sum(it.relevancy or 0 for it in scored) / n, 3),
                "context_precision": round(sum(it.context_precision or 0 for it in scored) / n, 3),
                "avg_latency_ms": round(sum(it.latency_ms for it in items) / max(len(items), 1)),
                "total_items": len(items),
                "scored_items": len(scored),
                "failed_items": sum(1 for it in items if it.error),
            }

            self._update(
                status="done",
                phase="Benchmark complete",
                scores=scores,
                items=[asdict(it) for it in items],
                config_snapshot=sanitize_config(cfg),
            )
            log.info("benchmark done: %s", json.dumps(scores))

        except Exception:
            log.exception("benchmark failed")
            self._update(
                status="error",
                phase="Error",
                error=traceback.format_exc(),
                items=[asdict(it) for it in items],
            )
