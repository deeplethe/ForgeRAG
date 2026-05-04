"""
KGRetriever — knowledge-graph multi-hop retrieval.

Wraps ``KGPath`` (entity extraction → graph traversal → chunk scoring).
The synthesized ``kg_context`` (selected entities + relations suitable
for injection into the answer prompt) is exposed as a second return
value for the answering pipeline to consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...telemetry import get_tracer
from ...types import ScoredChunk

_tracer = get_tracer()


@dataclass
class KGResult:
    hits: list[ScoredChunk]
    kg_context: Any | None = None  # KGContext — see retrieval.kg_path
    llm_calls: list[dict] = field(default_factory=list)


class KGRetriever:
    """
    Args:
        cfg: ``RetrievalKGPathConfig`` (top_k, max_hops, weights, model).
        graph_store: Neo4j / NetworkX graph store.
        rel: relational store for chunk rehydration.
        embedder: used for semantic entity matching inside the graph.
    """

    def __init__(self, cfg, *, graph_store, rel, embedder):
        self.cfg = cfg
        self.graph_store = graph_store
        self.rel = rel
        self.embedder = embedder

    def run(
        self,
        query: str,
        *,
        top_k: int | None = None,
        allowed_doc_ids: set[str] | None = None,
        path_prefixes: list[str] | None = None,
        or_fallback_prefixes: list[str] | None = None,
    ) -> KGResult:
        if self.graph_store is None:
            # Caller should not normally invoke us without a graph store,
            # but be defensive for SDK users composing unusual chains.
            return KGResult(hits=[], kg_context=None, llm_calls=[])

        kg_cfg = self.cfg if top_k is None else self.cfg.model_copy(update={"top_k": top_k})

        with _tracer.start_as_current_span("forgerag.kg_path") as span:
            span.set_attribute("forgerag.top_k", kg_cfg.top_k)
            span.set_attribute("forgerag.max_hops", kg_cfg.max_hops)

            from ...kg_path import KGPath

            kp = KGPath(kg_cfg, self.graph_store, self.rel, embedder=self.embedder)
            # Translate to KGPath's legacy signature (single primary
            # prefix + or-list). KGPath itself can stay single-prefix
            # for now — multi-prefix authz fans out at the top of the
            # resolver instead of inside the KG search loop, which keeps
            # the graph traversal contract unchanged.
            primary = (path_prefixes or [None])[0] if path_prefixes else None
            extra: list[str] = []
            if path_prefixes and len(path_prefixes) > 1:
                extra.extend(path_prefixes[1:])
            if or_fallback_prefixes:
                extra.extend(or_fallback_prefixes)
            hits: list[ScoredChunk] = kp.search(
                query,
                allowed_doc_ids=allowed_doc_ids,
                path_prefix=primary,
                path_prefixes_or=extra or None,
            )
            span.set_attribute("forgerag.hits", len(hits))
            return KGResult(
                hits=hits,
                kg_context=kp.kg_context,
                llm_calls=getattr(kp, "_llm_calls", []),
            )
