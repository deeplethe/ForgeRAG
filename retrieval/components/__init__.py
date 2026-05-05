"""
Reusable retrieval components.

Post-cutover only ``PathScopeResolver`` survives — it's used by
``api.agent.dispatch.build_tool_context`` to convert
``path_filters`` into the dual representation
(``path_prefixes`` for path-aware backends + ``allowed_doc_ids``
for path-unaware ones) that every agent tool inherits.

The other components — BM25Retriever, VectorRetriever, TreeRetriever,
KGRetriever, RRFFusion, ContextExpander, RerankComponent — were
specific to the deleted RetrievalPipeline. The agent reaches into
the underlying primitives (``state._bm25``, ``state.vector``,
``state.graph_store``, ``state.reranker``) directly via its tool
handlers in ``api/agent/tools.py``.
"""

from __future__ import annotations

from .path_scope import PathScopeResolver

__all__ = ["PathScopeResolver"]
