"""
Composable retrieval components. Each component is a small class with a
``run()`` method that does one thing — BM25 lookup, vector search, tree
navigation, RRF fusion, context expansion, rerank, etc. — and emits an
OTel span for observability.

ForgeRAG's own ``RetrievalPipeline`` is built by composing these; SDK
users can instantiate subsets, wire custom collaborators (their own
embedder, store, LLM), and assemble their own chains.

Public exports (stable surface):

    PathScopeResolver          — resolve path_filter → (prefix, allowed_ids)
    BM25Retriever              — lexical search + doc-id prefilter
    VectorRetriever            — dense embedding search
    TreeRetriever              — LLM / heuristic tree navigation
    KGRetriever                — knowledge-graph multi-hop traversal
    RRFFusion                  — Reciprocal Rank Fusion of N ranked lists
    ContextExpander            — descendant / sibling / crossref expansion
    RerankComponent            — LLM / API reranker stage

Inputs / outputs use ``ScoredChunk`` from ``retrieval.types`` and simple
dataclasses where helpful. Every component accepts cfg as the first ctor
arg and takes collaborators as keyword args, so dependency injection is
uniform across the library.
"""

from __future__ import annotations

from .expand import ContextExpander
from .fusion import RRFFusion
from .path_scope import PathScopeResolver
from .rerank import RerankComponent
from .retrievers.bm25 import BM25Retriever
from .retrievers.kg import KGRetriever
from .retrievers.tree import TreeRetriever
from .retrievers.vector import VectorRetriever

__all__ = [
    "BM25Retriever",
    "ContextExpander",
    "KGRetriever",
    "PathScopeResolver",
    "RRFFusion",
    "RerankComponent",
    "TreeRetriever",
    "VectorRetriever",
]
