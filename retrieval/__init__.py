"""Retrieval primitives consumed by the agent layer.

Post-cutover this package houses the bare retrieval components
(BM25 index, vector store wrapper, reranker, web search, types)
that the agent's tool dispatch ``api/agent/dispatch.py`` reaches
into directly. The old fixed RetrievalPipeline + RRF merge +
KG/tree path code was removed when ``api/agent/loop.py`` proved
out as a superior orchestration layer (see
``benchmark_results/BENCH_REPORT.md``).
"""

from .bm25 import InMemoryBM25Index, build_bm25_index
from .rerank import PassthroughReranker, Reranker, make_reranker
from .types import MergedChunk, RetrievalResult, ScoredChunk

__all__ = [
    "InMemoryBM25Index",
    "MergedChunk",
    "PassthroughReranker",
    "Reranker",
    "RetrievalResult",
    "ScoredChunk",
    "build_bm25_index",
    "make_reranker",
]
