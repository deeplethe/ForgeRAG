"""
ForgeRAG — the SDK-facing namespace.

This package is a **thin re-export facade** over the internal modules
(``retrieval``, ``api``, ``answering``, ``persistence`` etc.). SDK users
import from here:

    from forgerag.components import BM25Retriever, RRFFusion, Reranker
    from forgerag.client import Client
    from forgerag.eval import Dataset, LLMJudge
    from forgerag.server import create_app   # mount into your own ASGI host

Internal application code keeps importing the underlying modules directly
(``from retrieval.pipeline import RetrievalPipeline``) to avoid circular
refactors. The two namespaces coexist; ``forgerag.*`` is the stable
public surface, ``retrieval.* / api.*`` are the implementation.

Why the facade instead of a big rename?
    * Zero-churn for existing code and its imports.
    * Users get a clean package name from day one.
    * If we ever do the full rename, callers don't notice — they
      already import from ``forgerag.*``.
"""

__all__ = [
    "__version__",
    "answering",
    "client",
    "components",
    "config",
    "eval",
    "retrieval",
    "server",
]

__version__ = "0.2.0"
