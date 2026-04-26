"""
BM25Retriever — lexical chunk retrieval + document-level prefilter.

Output pair:
    hits        — list[ScoredChunk] of best chunks
    doc_ids     — set[str] of expanded doc_ids (for the Tree path's
                  document-level routing gate, which combines BM25+vector
                  hits into a single candidate doc set)

Supports multi-query input (query understanding produces expansion
variants); internally dedups chunks by best score across queries.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...telemetry import get_tracer
from ...types import ScoredChunk

_tracer = get_tracer()


@dataclass
class BM25Result:
    hits: list[ScoredChunk]
    expanded_doc_ids: set[str]


class BM25Retriever:
    """
    Args:
        cfg: ``RetrievalBM25Config`` (top_k / doc_prefilter_top_k / k1 / b).
        index: an ``InMemoryBM25Index`` — shared, lazy-built by AppState.
    """

    def __init__(self, cfg, index):
        self.cfg = cfg
        self.index = index

    def run(
        self,
        queries: list[str],
        *,
        top_k: int | None = None,
        doc_prefilter_top_k: int | None = None,
        allowed_doc_ids: set[str] | None = None,
    ) -> BM25Result:
        top_k = top_k if top_k is not None else self.cfg.top_k
        doc_k = doc_prefilter_top_k if doc_prefilter_top_k is not None else self.cfg.doc_prefilter_top_k

        with _tracer.start_as_current_span("forgerag.bm25_path") as span:
            span.set_attribute("forgerag.top_k", top_k)
            span.set_attribute("forgerag.queries_count", len(queries))

            # Chunk-level
            best: dict[str, ScoredChunk] = {}
            for q in queries:
                for cid, score in self.index.search_chunks(q, top_k, allowed_doc_ids=allowed_doc_ids):
                    ex = best.get(cid)
                    if ex is None or score > ex.score:
                        best[cid] = ScoredChunk(chunk_id=cid, score=score, source="bm25")
            hits = sorted(best.values(), key=lambda s: -s.score)

            # Doc-level prefilter (for Tree path)
            doc_ids: set[str] = set()
            for q in queries:
                for doc_id, _ in self.index.search_docs(q, doc_k, allowed_doc_ids=allowed_doc_ids):
                    doc_ids.add(doc_id)

            span.set_attribute("forgerag.hits", len(hits))
            span.set_attribute("forgerag.expanded_docs", len(doc_ids))
            return BM25Result(hits=hits, expanded_doc_ids=doc_ids)
