"""
Retrieval + answer metrics.

All metrics take a ``RetrievalRun`` and return a dict with per-metric
stats. Lists of IDs are used directly (no embeddings / no LLM calls),
keeping these cheap enough to run on every CI commit.

Implemented:

    recall_at_k     — fraction of relevant chunks retrieved in top-k
    precision_at_k  — fraction of top-k that are relevant
    mrr             — Mean Reciprocal Rank (first correct hit)
    hit_rate_at_k   — fraction of queries with ≥1 relevant chunk in top-k
    doc_recall_at_k — same as recall_at_k but at document granularity

Use the LLM-judge (``forgerag.eval.LLMJudge``) for faithfulness /
answer quality / context-precision scoring.
"""

from __future__ import annotations

from statistics import mean
from typing import Any

from .dataset import EvalQuery, RetrievalRowResult, RetrievalRun


def _by_id(rows: list[RetrievalRowResult]) -> dict[str, RetrievalRowResult]:
    return {r.query_id: r for r in rows}


def _queries_and_rows(run: RetrievalRun) -> list[tuple[EvalQuery, RetrievalRowResult]]:
    rows = _by_id(run.rows)
    return [(q, rows.get(q.query_id)) for q in run.dataset.queries if q.query_id in rows]


# ---------------------------------------------------------------------------


def recall_at_k(run: RetrievalRun, k: int = 10, *, level: str = "chunk") -> dict[str, Any]:
    """
    Fraction of ground-truth items that appear in the top-k returned.
    ``level='chunk'`` (default) or ``level='doc'``.
    Queries with empty ground truth are skipped (unscorable).
    """
    per_query: list[float] = []
    denom = 0
    for q, r in _queries_and_rows(run):
        truth = q.relevant_chunk_ids if level == "chunk" else q.relevant_doc_ids
        if not truth:
            continue
        returned = (r.returned_chunk_ids if level == "chunk" else r.returned_doc_ids)[:k]
        hit = sum(1 for t in truth if t in returned)
        per_query.append(hit / len(truth))
        denom += 1
    return {
        "metric": f"recall@{k}_{level}",
        "value": mean(per_query) if per_query else 0.0,
        "n": denom,
    }


def precision_at_k(run: RetrievalRun, k: int = 10, *, level: str = "chunk") -> dict[str, Any]:
    per_query: list[float] = []
    denom = 0
    for q, r in _queries_and_rows(run):
        truth = set(q.relevant_chunk_ids if level == "chunk" else q.relevant_doc_ids)
        if not truth:
            continue
        returned = (r.returned_chunk_ids if level == "chunk" else r.returned_doc_ids)[:k]
        if not returned:
            per_query.append(0.0)
        else:
            hit = sum(1 for x in returned if x in truth)
            per_query.append(hit / len(returned))
        denom += 1
    return {
        "metric": f"precision@{k}_{level}",
        "value": mean(per_query) if per_query else 0.0,
        "n": denom,
    }


def hit_rate_at_k(run: RetrievalRun, k: int = 10, *, level: str = "chunk") -> dict[str, Any]:
    per_query: list[float] = []
    denom = 0
    for q, r in _queries_and_rows(run):
        truth = set(q.relevant_chunk_ids if level == "chunk" else q.relevant_doc_ids)
        if not truth:
            continue
        returned = set((r.returned_chunk_ids if level == "chunk" else r.returned_doc_ids)[:k])
        per_query.append(1.0 if (truth & returned) else 0.0)
        denom += 1
    return {
        "metric": f"hit_rate@{k}_{level}",
        "value": mean(per_query) if per_query else 0.0,
        "n": denom,
    }


def mrr(run: RetrievalRun, *, level: str = "chunk", k: int | None = None) -> dict[str, Any]:
    """Mean Reciprocal Rank — position of first correct hit, averaged
    across queries (0 if none)."""
    per_query: list[float] = []
    denom = 0
    for q, r in _queries_and_rows(run):
        truth = set(q.relevant_chunk_ids if level == "chunk" else q.relevant_doc_ids)
        if not truth:
            continue
        returned = r.returned_chunk_ids if level == "chunk" else r.returned_doc_ids
        if k is not None:
            returned = returned[:k]
        rr = 0.0
        for i, cid in enumerate(returned, start=1):
            if cid in truth:
                rr = 1.0 / i
                break
        per_query.append(rr)
        denom += 1
    return {
        "metric": f"mrr_{level}" + (f"@{k}" if k else ""),
        "value": mean(per_query) if per_query else 0.0,
        "n": denom,
    }


# ---------------------------------------------------------------------------


def summary(run: RetrievalRun, *, k: int = 10) -> dict[str, Any]:
    """Standard dashboard — run all the default chunk-level metrics at k."""
    return {
        "dataset": run.dataset.name,
        "n_queries": len(run.dataset),
        "n_errors": sum(1 for r in run.rows if r.error),
        "recall@k": recall_at_k(run, k=k)["value"],
        "precision@k": precision_at_k(run, k=k)["value"],
        "hit_rate@k": hit_rate_at_k(run, k=k)["value"],
        "mrr@k": mrr(run, k=k)["value"],
        "k": k,
    }
