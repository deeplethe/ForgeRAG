"""
Retrieval orchestrator + error type — public SDK surface.

``RetrievalPipeline`` is the default end-to-end assembly of components
(query-understanding → parallel BM25/Vector/KG → Tree → RRF → expand →
rerank → citations). Most SDK users won't construct it directly — they'll
either use ``forgerag.client.Client`` (remote) or compose components
themselves via ``forgerag.components``.

``RetrievalError`` is raised when an infrastructure dependency (LLM,
embedder, vector store, KG store, reranker) fails. Set
``QueryOverrides.allow_partial_failure = True`` to degrade gracefully.
"""

from retrieval.pipeline import RetrievalError, RetrievalPipeline, build_bm25_index
from retrieval.types import RetrievalResult, ScoredChunk

__all__ = [
    "RetrievalError",
    "RetrievalPipeline",
    "RetrievalResult",
    "ScoredChunk",
    "build_bm25_index",
]
