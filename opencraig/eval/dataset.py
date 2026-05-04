"""
Eval datasets + run containers.

``Dataset`` is just "a list of ``EvalQuery``" with JSONL I/O helpers.
``RetrievalRun`` is the result of calling some retrieve-function on
every query in the dataset; it's the argument the metric functions
consume.

Shapes are deliberately loose — your retrieve-function can return an
``Answer`` (from ``opencraig.client``), a plain dict, or any object with
``citations_used`` / ``citations_all`` / ``text`` — the adapters try in
that order. When in doubt, stick to lists of chunk IDs in
``RetrievalRun.rows``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EvalQuery:
    """One test case. ``relevant_*`` lists are the ground truth used by
    the ``metrics`` module."""

    query_id: str
    query: str
    relevant_chunk_ids: list[str] = field(default_factory=list)
    relevant_doc_ids: list[str] = field(default_factory=list)
    expected_answer: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> EvalQuery:
        return cls(
            query_id=str(d.get("query_id") or d.get("id") or ""),
            query=d["query"],
            relevant_chunk_ids=list(d.get("relevant_chunk_ids") or []),
            relevant_doc_ids=list(d.get("relevant_doc_ids") or []),
            expected_answer=d.get("expected_answer"),
            tags=list(d.get("tags") or []),
            metadata=dict(d.get("metadata") or {}),
        )


@dataclass
class Dataset:
    queries: list[EvalQuery]
    name: str = ""

    def __len__(self) -> int:
        return len(self.queries)

    def __iter__(self):
        return iter(self.queries)

    @classmethod
    def from_jsonl(cls, path: str | Path, name: str | None = None) -> Dataset:
        path = Path(path)
        rows = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
        return cls(
            queries=[EvalQuery.from_dict(r) for r in rows],
            name=name or path.stem,
        )

    @classmethod
    def from_records(cls, rows: Iterable[dict], name: str = "") -> Dataset:
        return cls(queries=[EvalQuery.from_dict(r) for r in rows], name=name)


# ---------------------------------------------------------------------------


@dataclass
class RetrievalRowResult:
    """Per-query result captured during a run."""

    query_id: str
    query: str
    returned_chunk_ids: list[str]
    returned_doc_ids: list[str]
    answer_text: str | None = None
    error: str | None = None
    raw: Any = None  # original return value, kept for debugging / metrics


@dataclass
class RetrievalRun:
    """Outputs of running a retrieve-function across a Dataset."""

    dataset: Dataset
    rows: list[RetrievalRowResult]

    # ── Executor ────────────────────────────────────────────────────────

    @classmethod
    def execute(
        cls,
        dataset: Dataset,
        retrieve: Callable[[EvalQuery], Any],
        *,
        on_progress: Callable[[int, int, EvalQuery], None] | None = None,
    ) -> RetrievalRun:
        """
        Runs ``retrieve(query)`` for every query in ``dataset``. ``retrieve``
        may return:

          * a ``opencraig.client.Answer`` — uses its ``citations_used`` /
            ``citations_all`` / ``text``
          * a plain dict in the same shape as the /query response
          * any iterable of objects with a ``.chunk_id`` / ``.doc_id``
            attribute (e.g. ``list[ScoredChunk]`` from a local pipeline)

        Errors are caught and recorded in ``RetrievalRowResult.error`` so
        one bad query doesn't abort the run.
        """
        rows: list[RetrievalRowResult] = []
        total = len(dataset)
        for i, q in enumerate(dataset.queries):
            if on_progress is not None:
                on_progress(i, total, q)
            try:
                raw = retrieve(q)
                chunk_ids, doc_ids, text = _extract_hits(raw)
                rows.append(
                    RetrievalRowResult(
                        query_id=q.query_id,
                        query=q.query,
                        returned_chunk_ids=chunk_ids,
                        returned_doc_ids=doc_ids,
                        answer_text=text,
                        raw=raw,
                    )
                )
            except Exception as e:
                rows.append(
                    RetrievalRowResult(
                        query_id=q.query_id,
                        query=q.query,
                        returned_chunk_ids=[],
                        returned_doc_ids=[],
                        error=f"{type(e).__name__}: {e}",
                    )
                )
        return cls(dataset=dataset, rows=rows)


def _extract_hits(raw: Any) -> tuple[list[str], list[str], str | None]:
    """Best-effort extraction of (chunk_ids, doc_ids, answer_text) from
    whatever the caller's retrieve-function returned."""
    # Answer from opencraig.client
    if hasattr(raw, "citations_all"):
        chunks, docs = [], []
        for c in list(getattr(raw, "citations_all", []) or getattr(raw, "citations_used", [])):
            cid = getattr(c, "chunk_id", None) or (c.get("chunk_id") if isinstance(c, dict) else None)
            did = getattr(c, "doc_id", None) or (c.get("doc_id") if isinstance(c, dict) else None)
            if cid:
                chunks.append(cid)
            if did:
                docs.append(did)
        return chunks, docs, getattr(raw, "text", None)
    # dict-shaped /query response
    if isinstance(raw, dict):
        chunks, docs = [], []
        for c in raw.get("citations_all") or raw.get("citations_used") or []:
            if isinstance(c, dict):
                if c.get("chunk_id"):
                    chunks.append(c["chunk_id"])
                if c.get("doc_id"):
                    docs.append(c["doc_id"])
        return chunks, docs, raw.get("text")
    # list of ScoredChunk / dict with chunk_id
    if isinstance(raw, list):
        chunks, docs = [], []
        for c in raw:
            cid = getattr(c, "chunk_id", None) or (c.get("chunk_id") if isinstance(c, dict) else None)
            did = getattr(c, "doc_id", None) or (c.get("doc_id") if isinstance(c, dict) else None)
            if cid:
                chunks.append(cid)
            if did:
                docs.append(did)
        return chunks, docs, None
    return [], [], None
