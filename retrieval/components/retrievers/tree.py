"""
TreeRetriever — LLM / heuristic tree navigation over document structure.

Wraps the existing ``TreePath`` class (which owns the actual
per-document BFS / LLM-navigator logic) as a composable component with
an OTel span and a clean single-method interface.
"""

from __future__ import annotations

from ...telemetry import get_tracer
from ...types import ScoredChunk

_tracer = get_tracer()


class TreeRetriever:
    """
    Args:
        cfg: ``RetrievalTreePathConfig`` (top_k, llm_nav_enabled, nav.*).
        bm25_cfg: ``RetrievalBM25Config`` — needed because TreePath uses
                  BM25 scoring internally on each candidate doc's tree.
        bm25_index: shared ``InMemoryBM25Index``.
        rel: relational store for chunk / tree metadata.
        navigator: optional ``LLMTreeNavigator``. Pass ``None`` to force
                   heuristic routing regardless of cfg.llm_nav_enabled.
    """

    def __init__(self, cfg, *, bm25_cfg, bm25_index, rel, navigator=None):
        self.cfg = cfg
        self.bm25_cfg = bm25_cfg
        self.bm25 = bm25_index
        self.rel = rel
        self.navigator = navigator

    def run(
        self,
        query: str,
        *,
        bm25_doc_ids: set[str],
        vector_doc_ids: set[str],
        prefilter_hits: list | None = None,
        allowed_doc_ids: set[str] | None = None,
        top_k: int | None = None,
        llm_nav_enabled: bool | None = None,
    ) -> list[ScoredChunk]:
        # Build a per-request cfg copy with any top_k / llm_nav override applied.
        tp_cfg = self.cfg
        if top_k is not None or llm_nav_enabled is not None:
            updates = {}
            if top_k is not None:
                updates["top_k"] = top_k
            if llm_nav_enabled is not None:
                updates["llm_nav_enabled"] = llm_nav_enabled
            tp_cfg = self.cfg.model_copy(update=updates)

        # Honour per-request llm_nav: if caller turned it off, don't use
        # the navigator even if cfg says otherwise.
        _nav = self.navigator if tp_cfg.llm_nav_enabled else None

        with _tracer.start_as_current_span("forgerag.tree_path") as span:
            span.set_attribute("forgerag.top_k", tp_cfg.top_k)
            span.set_attribute("forgerag.llm_nav_enabled", bool(_nav is not None))

            from ...tree_path import TreePath

            tp = TreePath(
                tp_cfg,
                self.bm25_cfg,
                self.bm25,
                self.rel,
                navigator=_nav,
            )
            combined_doc_ids = (bm25_doc_ids or set()) | (vector_doc_ids or set())
            result: list[ScoredChunk] = tp.search(
                query,
                vector_doc_ids=combined_doc_ids,
                prefilter_hits=prefilter_hits,
                allowed_doc_ids=allowed_doc_ids,
            )
            span.set_attribute("forgerag.hits", len(result))
            return result
