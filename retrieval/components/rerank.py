"""
RerankComponent — LLM / API reranker stage.

Thin wrapper around the existing ``Reranker`` protocol (llm-as-judge,
rerank-api backend, passthrough). When disabled or when the candidate
set is empty, just head-slices the input list.
"""

from __future__ import annotations

from ..telemetry import get_tracer
from ..types import ScoredChunk

_tracer = get_tracer()


class RerankComponent:
    """
    Args:
        cfg: ``RetrievalRerankConfig`` (enabled / top_k / backend / model).
        reranker: the inner ``Reranker`` implementation (see
                  ``retrieval.rerank.make_reranker``).

    The component always honours the cfg's ``enabled`` flag; to force on
    or off per request pass ``enabled=...`` to ``run()``.
    """

    def __init__(self, cfg, *, reranker):
        self.cfg = cfg
        self.reranker = reranker

    def run(
        self,
        query: str,
        candidates: list[ScoredChunk],
        *,
        top_k: int | None = None,
        enabled: bool | None = None,
    ) -> tuple[list[ScoredChunk], str | None]:
        top_k = top_k if top_k is not None else self.cfg.top_k
        # No cfg-level toggle anymore; the orchestrator passes
        # ``enabled=False`` when QueryOverrides.rerank=False or when
        # cfg.backend == "passthrough".
        if enabled is None:
            enabled = self.cfg.backend != "passthrough"

        with _tracer.start_as_current_span("forgerag.rerank") as span:
            span.set_attribute("forgerag.enabled", enabled)
            span.set_attribute("forgerag.top_k", top_k)
            span.set_attribute("forgerag.candidates", len(candidates))

            if not enabled or not candidates:
                return candidates[:top_k], None

            try:
                picked = self.reranker.rerank(query, candidates, top_k=top_k)
                span.set_attribute("forgerag.picked", len(picked))
                return picked, None
            except Exception as e:
                # The pipeline orchestrator decides whether to re-raise
                # as RetrievalError (strict) or fall back — the component
                # simply reports the error + returns RRF-order slice.
                err = f"{type(e).__name__}: {e}"
                span.set_attribute("forgerag.error", err)
                return candidates[:top_k], err
