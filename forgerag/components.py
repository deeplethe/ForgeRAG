"""
Composable retrieval components — public SDK surface.

Every class here is a small, injectable unit with a single ``run()``
method. Each emits an OTel span, so the moment you wire them into a
custom chain you get waterfall tracing for free.

Typical usage:

    from forgerag.components import (
        PathScopeResolver, BM25Retriever, VectorRetriever,
        RRFFusion, Reranker,
    )

    bm25 = BM25Retriever(cfg.bm25, my_index)
    vec  = VectorRetriever(cfg.vector, embedder=my_emb, vector_store=my_vs)
    fused = RRFFusion(k=60).run(
        [
            bm25.run(["my query"], top_k=30).hits,
            vec.run(["my query"], top_k=30).hits,
        ]
    )

See each component's module docstring for the full contract.
"""

from retrieval.components import (
    BM25Retriever,
    ContextExpander,
    KGRetriever,
    PathScopeResolver,
    RRFFusion,
    TreeRetriever,
    VectorRetriever,
)
from retrieval.components import (
    RerankComponent as Reranker,
)

__all__ = [
    "BM25Retriever",
    "ContextExpander",
    "KGRetriever",
    "PathScopeResolver",
    "RRFFusion",
    "Reranker",
    "TreeRetriever",
    "VectorRetriever",
]
