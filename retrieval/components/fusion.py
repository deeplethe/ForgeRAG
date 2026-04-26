"""
RRFFusion — Reciprocal Rank Fusion of N ranked chunk lists.

Keeps the existing ``retrieval.merge.rrf_merge`` implementation as the
one source of truth for the score formula; this component is a thin
OTel-wrapped interface so it plugs into composable chains.
"""

from __future__ import annotations

from ..telemetry import get_tracer
from ..types import ScoredChunk

_tracer = get_tracer()


class RRFFusion:
    """
    Args:
        k: RRF constant (default 60). Smaller values let top-ranked items
           dominate more; larger values smooth across positions.
    """

    def __init__(self, k: int = 60):
        self.k = k

    def run(
        self,
        ranked_lists: list[list[ScoredChunk]],
        *,
        labels: list[str] | None = None,
    ) -> list[ScoredChunk]:
        from ..merge import rrf_merge

        labels = labels or [f"path_{i}" for i in range(len(ranked_lists))]
        with _tracer.start_as_current_span("forgerag.rrf_merge") as span:
            span.set_attribute("forgerag.rrf_k", self.k)
            span.set_attribute("forgerag.active_paths", labels)
            span.set_attribute("forgerag.input_counts", [len(xs) for xs in ranked_lists])
            out = rrf_merge(ranked_lists, k=self.k)
            span.set_attribute("forgerag.merged_count", len(out))
            return out
