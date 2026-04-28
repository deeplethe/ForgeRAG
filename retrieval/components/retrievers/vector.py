"""
VectorRetriever — dense embedding search over the vector store.

Takes a list of query variants, embeds them in one batch, searches the
vector store, dedups, and returns both the ScoredChunk list and the
raw VectorHit list (needed by the Tree path for doc-id routing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...telemetry import get_tracer
from ...types import ScoredChunk

_tracer = get_tracer()


@dataclass
class VectorResult:
    hits: list[ScoredChunk]
    raw_hits: list = field(default_factory=list)  # VectorHit[], keeps doc_id


class VectorRetriever:
    """
    Args:
        cfg: ``RetrievalVectorConfig`` (top_k / default_filter).
        embedder: object with ``embed_texts(list[str]) -> list[list[float]]``.
        vector_store: object with ``.search(vec, top_k, filter) -> list[VectorHit]``.
    """

    def __init__(self, cfg, *, embedder, vector_store):
        self.cfg = cfg
        self.embedder = embedder
        self.vector = vector_store

    def run(
        self,
        queries: list[str],
        *,
        top_k: int | None = None,
        filter: dict | None = None,
        path_prefix: str | None = None,
        or_fallback_prefixes: list[str] | None = None,
        allowed_doc_ids: set[str] | None = None,   # noqa: ARG002 — kept for API stability
    ) -> VectorResult:
        top_k = top_k if top_k is not None else self.cfg.top_k

        with _tracer.start_as_current_span("forgerag.vector_path") as span:
            span.set_attribute("forgerag.top_k", top_k)
            span.set_attribute("forgerag.queries_count", len(queries))

            # Single batched embedding call for all variants
            q_vecs = self.embedder.embed_texts(queries)

            # Compose backend filter. ``path_prefix`` flows through to the
            # vector store as a denormalised path scope; ``_build_chroma_where``
            # translates it into a Chroma 1.5-compatible
            # ``path_index $contains "|<prefix>|"`` query that matches at
            # path-component boundaries (so ``/leg`` won't false-match
            # ``/legal/...``). See ``persistence/vector/chroma.py``.
            vector_filter: dict[str, Any] = {}
            base = filter or self.cfg.default_filter
            if base:
                vector_filter = {k: v for k, v in base.items() if k != "_path_filter"}
            if path_prefix:
                vector_filter["path_prefix"] = path_prefix
                if or_fallback_prefixes:
                    vector_filter["path_prefix_or"] = list(or_fallback_prefixes)

            # Run per-variant, collect raw + scored
            raw_hits = []
            best: dict[str, ScoredChunk] = {}
            for q_vec in q_vecs:
                hits = self.vector.search(q_vec, top_k=top_k, filter=vector_filter or None)
                raw_hits.extend(hits)
                for h in hits:
                    ex = best.get(h.chunk_id)
                    if ex is None or h.score > ex.score:
                        best[h.chunk_id] = ScoredChunk(
                            chunk_id=h.chunk_id,
                            score=h.score,
                            source="vector",
                        )

            deduped_raw: dict[str, Any] = {}
            for h in raw_hits:
                if h.chunk_id not in deduped_raw:
                    deduped_raw[h.chunk_id] = h

            out_hits = sorted(best.values(), key=lambda s: -s.score)
            span.set_attribute("forgerag.hits", len(out_hits))
            return VectorResult(hits=out_hits, raw_hits=list(deduped_raw.values()))
