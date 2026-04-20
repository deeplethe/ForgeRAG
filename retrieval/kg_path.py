"""
Knowledge Graph retrieval path.

Multi-level retrieval inspired by LightRAG (dual-level variant):

  1. **Local**: Extract entities from query -> graph.get_neighbors()
     -> collect source chunk_ids from related entities/relations
  2. **Global**: Extract keywords from query -> graph.search_entities()
     -> high-level relationship traversal -> chunk_ids
  3. **Relation semantic**: Embed query -> cosine search over relation
     description embeddings -> collect chunk_ids

The three levels run **in parallel** via a ThreadPoolExecutor. Each
worker writes to its own private ``KGContext`` accumulator (no shared
state during retrieval); contexts are deduplicated and merged into
``self.kg_context`` once all workers finish. All levels produce scored
chunks that are fed into the 3-way RRF merge.
"""

from __future__ import annotations

import logging
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.retrieval import KGPathConfig
    from embedder.base import Embedder
    from graph.base import GraphStore
    from persistence.store import Store

from .types import KGContext, ScoredChunk

log = logging.getLogger(__name__)

# Hard cap per worker so a stuck graph call can't hang the request forever.
_WORKER_TIMEOUT_S = 30


class KGPath:
    """Knowledge graph retrieval path."""

    def __init__(
        self,
        cfg: KGPathConfig,
        graph: GraphStore,
        relational: Store,
        *,
        extractor=None,  # KGExtractor instance for query entity extraction
        embedder: Embedder | None = None,
    ):
        self.cfg = cfg
        self.graph = graph
        self.rel = relational
        self.extractor = extractor
        self.embedder = embedder
        self._llm_calls: list[dict] = []  # collect LLM call info for trace
        self.kg_context: KGContext = KGContext()  # synthesized context for prompt injection

    def search(
        self,
        query: str,
        *,
        allowed_doc_ids: set[str] | None = None,
    ) -> list[ScoredChunk]:
        """
        Multi-level KG retrieval.

        Returns a ranked list of ScoredChunks sourced from the
        knowledge graph, ready for RRF merge with other paths.

        Also populates ``self.kg_context`` with entity and relation
        descriptions — synthesized knowledge that the answering layer
        injects into the LLM prompt alongside raw text chunks
        (inspired by LightRAG's dual-level context assembly).

        ``allowed_doc_ids`` — when set, entities and chunks whose
        source documents aren't in this whitelist are dropped *before*
        the top-k truncation, and the synthesized KGContext entities/
        relations are likewise scoped. This is a pre-filter at the
        results-aggregation stage — graph traversal itself still scans
        the full KG, but the final output is scope-clean.
        """
        self.kg_context = KGContext()

        # Step 1: Extract entities and keywords from query
        entity_names, keywords = self._extract_query_entities(query)
        if not entity_names and not keywords:
            log.debug("KG path: no entities/keywords extracted from query")
            return []

        # Compute query embedding once; relation retrieval reuses it.
        query_vec: list[float] | None = None
        if self.embedder is not None:
            try:
                query_vec = self.embedder.embed_texts([query])[0]
            except Exception as e:
                log.warning("KG path: query embedding failed: %s", e)

        # Steps 2–4: run the three retrieval levels in parallel. Each
        # worker writes to its own KGContext — no shared mutable state
        # during the retrieval phase — and we merge-dedup at the end.
        local_ctx = KGContext()
        global_ctx = KGContext()
        relation_ctx = KGContext()

        # Explicit pool + finally so a stuck worker cannot block search() on
        # the executor's shutdown (the `with` context manager's __exit__ does
        # shutdown(wait=True), which would defeat the collective timeout).
        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="kg_path")
        try:
            f_local = pool.submit(self._local_retrieval, entity_names, local_ctx)
            f_global = pool.submit(self._global_retrieval, keywords or entity_names, global_ctx)
            f_relation = pool.submit(self._relation_retrieval, query_vec, relation_ctx)

            # Collective timeout: wait once for all futures with ONE
            # shared budget. Previously each _result_or_empty had its
            # own 30s, so the worst case was N×30s — pathological for
            # a "parallel" path.
            wait(
                [f_local, f_global, f_relation],
                timeout=_WORKER_TIMEOUT_S,
                return_when=ALL_COMPLETED,
            )
            # Snapshot each worker's scores AND its ctx together. If a
            # worker is still running, its ctx is being mutated concurrently
            # — we drop it to avoid iterating a list that's being appended
            # to from another thread.
            local_chunks, local_ctx = _collect(f_local, local_ctx, "local")
            global_chunks, global_ctx = _collect(f_global, global_ctx, "global")
            relation_chunks, relation_ctx = _collect(f_relation, relation_ctx, "relation")
        finally:
            # wait=False + cancel_futures so we don't block on stragglers.
            # Still-running threads are orphaned; Neo4j's driver has its own
            # socket timeout, so they eventually die and the pool's threads
            # return naturally.
            pool.shutdown(wait=False, cancel_futures=True)

        # Merge the private contexts into self.kg_context, deduped by
        # _eid / _rid. Local wins on ties because it's merged first.
        self.kg_context = _merge_contexts([local_ctx, global_ctx, relation_ctx])

        # Cap the synthesized context to prevent hub-entity 2-hop
        # explosion (seen on legal corpora: a "Company" query entity
        # can pull thousands of neighbor entities + relations, none
        # actually relevant). Caps chosen to match roughly what the
        # generator prompt can fit inside its 40% kg-budget (~50 lines
        # each at ~60 chars/line). Later entries are dropped — local
        # retrieval is merged first, so the kept entries are the most
        # directly relevant to the query's extracted entities.
        _MAX_KG_ENTITIES = 50
        _MAX_KG_RELATIONS = 30
        if len(self.kg_context.entities) > _MAX_KG_ENTITIES:
            log.debug(
                "KG context: truncating entities %d -> %d",
                len(self.kg_context.entities),
                _MAX_KG_ENTITIES,
            )
            self.kg_context.entities = self.kg_context.entities[:_MAX_KG_ENTITIES]
        if len(self.kg_context.relations) > _MAX_KG_RELATIONS:
            log.debug(
                "KG context: truncating relations %d -> %d",
                len(self.kg_context.relations),
                _MAX_KG_RELATIONS,
            )
            self.kg_context.relations = self.kg_context.relations[:_MAX_KG_RELATIONS]

        # Step 5: Weighted merge of chunk scores
        merged = self._merge_scores(
            local_chunks,
            global_chunks,
            relation_chunks,
        )

        # Step 5.5: Path scoping — drop chunks whose doc isn't in the
        # caller's scope BEFORE verification / truncation, so top_k
        # reflects post-scope ranking instead of wasting slots on
        # chunks we'd otherwise discard.
        if allowed_doc_ids is not None and merged:
            merged = self._scope_chunks(merged, allowed_doc_ids)
            # Also scope the synthesized KG context (entities / relations)
            # so the answer-generator prompt doesn't leak descriptions
            # from outside the user's scope.
            self._scope_kg_context(allowed_doc_ids)

        # Step 6: Verify chunks exist and return top-k
        verified = self._verify_chunks(merged)
        top_k = sorted(verified, key=lambda s: -s.score)[: self.cfg.top_k]

        log.info(
            "KG path: entities=%d keywords=%d local=%d global=%d relation=%d merged=%d top_k=%d kg_ctx(ent=%d rel=%d)",
            len(entity_names),
            len(keywords),
            len(local_chunks),
            len(global_chunks),
            len(relation_chunks),
            len(merged),
            len(top_k),
            len(self.kg_context.entities),
            len(self.kg_context.relations),
        )
        return top_k

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _extract_query_entities(self, query: str) -> tuple[list[str], list[str]]:
        """Extract entity names and keywords from the query using LLM."""
        import time as _time

        t0 = _time.time()
        try:
            if self.extractor is not None:
                result = self.extractor.extract_query_entities(query)
            else:
                # Use kg_path config; if api_key/api_base are empty, log and skip
                if not self.cfg.api_key and not self.cfg.api_base:
                    log.warning(
                        "KG path: no api_key or api_base configured "
                        "(provider_id=%s) — skipping query entity extraction",
                        getattr(self.cfg, "provider_id", None),
                    )
                    return [], []
                from ingestion.kg_extractor import KGExtractor

                ext = KGExtractor(
                    model=self.cfg.model,
                    api_key=self.cfg.api_key,
                    api_key_env=self.cfg.api_key_env,
                    api_base=self.cfg.api_base,
                )
                result = ext.extract_query_entities(query)
            ms = int((_time.time() - t0) * 1000)
            self._llm_calls.append(
                dict(
                    model=getattr(self.cfg, "model", "unknown"),
                    purpose="kg_entity_extraction",
                    latency_ms=ms,
                    output_preview=str(result[0][:5]) if result[0] else "[]",
                )
            )
            return result
        except Exception as e:
            log.warning("KG query entity extraction failed: %s", e)
            return [], []

    def _local_retrieval(
        self,
        entity_names: list[str],
        ctx: KGContext,
    ) -> dict[str, float]:
        """
        Local retrieval: find entities by name -> traverse neighbors
        -> collect chunk_ids with scores based on hop distance.

        Entity resolution order (first hit wins):
          1. Exact name match via ``entity_id_from_name`` (SHA256 hash).
             Fastest path for same-language, identical-spelling hits.
          2. Embedding cosine search via ``search_entities_by_embedding``.
             Bridges cross-lingual queries (e.g. "蜜蜂" → "bee") as long
             as the embedder is multilingual.
          3. Fuzzy name search via ``search_entities`` (substring /
             fulltext). Last-resort back-compat path.

        Writes entity/relation descriptions into the caller-provided
        ``ctx`` (private to this worker). Name resolution for relation
        endpoints is batched in a single ``get_entities_by_ids`` call.
        """

        chunk_scores: dict[str, float] = {}
        _seen_entities: set[str] = set()
        _seen_relations: set[str] = set()
        # entity_id → name, populated from entities we already fetched.
        _name_cache: dict[str, str] = {}
        # Relations whose endpoint names we still need to resolve in batch.
        _pending_rels: list[tuple[object, str]] = []  # (rel, rid)

        resolved_entities = self._resolve_entity_names(entity_names)

        for entity in resolved_entities:
            eid = entity.entity_id

            _name_cache[eid] = entity.name

            # Collect entity description for KG context
            if eid not in _seen_entities and entity.description:
                _seen_entities.add(eid)
                ctx.entities.append(
                    {
                        "name": entity.name,
                        "type": entity.entity_type,
                        "description": entity.description,
                        "_eid": eid,
                    }
                )

            # Direct entity chunks (hop 0) -- highest score
            for cid in entity.source_chunk_ids:
                chunk_scores[cid] = max(chunk_scores.get(cid, 0), 1.0)

            # Relations of this entity
            relations = self.graph.get_relations(eid)
            for rel in relations:
                score = 0.8 * rel.weight  # relation weight matters
                for cid in rel.source_chunk_ids:
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), score)

                # Defer relation-description collection: we need source/target
                # names, and resolving them one-by-one costs a round-trip each.
                rid = rel.relation_id
                if rid not in _seen_relations and rel.description:
                    _seen_relations.add(rid)
                    _pending_rels.append((rel, rid))

            # Neighbor entities (hop 1+)
            # get_neighbors returns a flat list without per-item hop info,
            # so we use a uniform decay based on the configured max_hops.
            neighbors = self.graph.get_neighbors(eid, max_hops=self.cfg.max_hops)
            neighbor_score = 1.0 / (1 + self.cfg.max_hops)  # average decay
            for neighbor in neighbors:
                _name_cache[neighbor.entity_id] = neighbor.name
                # Collect neighbor entity descriptions too
                if neighbor.entity_id not in _seen_entities and neighbor.description:
                    _seen_entities.add(neighbor.entity_id)
                    ctx.entities.append(
                        {
                            "name": neighbor.name,
                            "type": neighbor.entity_type,
                            "description": neighbor.description,
                            "_eid": neighbor.entity_id,
                        }
                    )
                for cid in neighbor.source_chunk_ids:
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), neighbor_score)

        _resolve_and_emit_relations(self.graph, _pending_rels, _name_cache, ctx)
        return chunk_scores

    def _global_retrieval(
        self,
        keywords: list[str],
        ctx: KGContext,
    ) -> dict[str, float]:
        """
        Global retrieval: search entities by keywords -> collect
        chunk_ids from matched entities and their relations.

        Keyword→entity resolution tries embedding cosine first
        (cross-lingual) then falls back to fuzzy name search.

        Writes entity/relation descriptions into the caller-provided
        ``ctx``. Cross-function deduplication (against local's output)
        happens later in the merge step, not here.
        """
        chunk_scores: dict[str, float] = {}
        _seen_eids: set[str] = set()
        _seen_rids: set[str] = set()
        _name_cache: dict[str, str] = {}
        _pending_rels: list[tuple[object, str]] = []

        # Batch-embed all keywords once (one API call instead of N).
        kw_vecs = self._batch_embed(keywords)

        for kw, kw_vec in zip(keywords, kw_vecs, strict=True):
            entities = self._search_entities_hybrid(kw, kw_vec, top_k=5)
            for rank, entity in enumerate(entities):
                # Score by search rank
                score = 1.0 / (1.0 + rank)
                for cid in entity.source_chunk_ids:
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), score)

                _name_cache[entity.entity_id] = entity.name
                if entity.entity_id not in _seen_eids and entity.description:
                    _seen_eids.add(entity.entity_id)
                    ctx.entities.append(
                        {
                            "name": entity.name,
                            "type": entity.entity_type,
                            "description": entity.description,
                            "_eid": entity.entity_id,
                        }
                    )

                # Also collect relation chunks and descriptions
                relations = self.graph.get_relations(entity.entity_id)
                for rel in relations:
                    rel_score = score * 0.6 * rel.weight
                    for cid in rel.source_chunk_ids:
                        chunk_scores[cid] = max(chunk_scores.get(cid, 0), rel_score)

                    if rel.relation_id not in _seen_rids and rel.description:
                        _seen_rids.add(rel.relation_id)
                        _pending_rels.append((rel, rel.relation_id))

        _resolve_and_emit_relations(self.graph, _pending_rels, _name_cache, ctx)
        return chunk_scores

    def _relation_retrieval(
        self,
        query_vec: list[float] | None,
        ctx: KGContext,
    ) -> dict[str, float]:
        """
        Relation semantic search: cosine search over relation description
        embeddings using the pre-computed query vector.

        Writes relation descriptions into the caller-provided ``ctx``.
        """
        rw = getattr(self.cfg, "relation_weight", 0.0)
        if rw <= 0 or query_vec is None:
            return {}

        top_k = getattr(self.cfg, "relation_top_k", 10)
        matches = self.graph.search_relations_by_embedding(query_vec, top_k=top_k)
        if not matches:
            return {}

        _seen_rids: set[str] = set()
        _name_cache: dict[str, str] = {}
        _pending_rels: list[tuple[object, str]] = []

        chunk_scores: dict[str, float] = {}
        for rel, sim_score in matches:
            if sim_score < 0.3:
                continue
            for cid in rel.source_chunk_ids:
                chunk_scores[cid] = max(chunk_scores.get(cid, 0), sim_score)

            if rel.relation_id not in _seen_rids and rel.description:
                _seen_rids.add(rel.relation_id)
                _pending_rels.append((rel, rel.relation_id))

        _resolve_and_emit_relations(self.graph, _pending_rels, _name_cache, ctx)
        return chunk_scores

    # ------------------------------------------------------------------
    # Entity-name resolution helpers (cross-lingual aware)
    # ------------------------------------------------------------------

    # Minimum cosine similarity for an embedding-search hit to count.
    # 0.5 is conservative; lower if real-world recall suffers.
    _EMBED_SEARCH_THRESHOLD = 0.5

    def _resolve_entity_names(self, entity_names: list[str]) -> list:
        """Resolve LLM-extracted entity names to Entity objects.

        Strategy (first hit wins per name):
          1. Exact: ``entity_id_from_name`` + ``get_entity`` — fastest,
             same-language identical-spelling path.
          2. Embedding cosine: ``search_entities_by_embedding`` —
             cross-lingual bridge (e.g. "蜜蜂" → "bee" if the embedder
             is multilingual and the graph has ``name_embedding``).
          3. Fuzzy substring: ``search_entities`` — last-resort
             back-compat path, kept so monolingual / embedding-less
             backends still work.
        """
        from graph.base import entity_id_from_name

        if not entity_names:
            return []

        resolved: list = []
        unresolved_idx: list[int] = []
        unresolved_names: list[str] = []

        for i, name in enumerate(entity_names):
            entity = self.graph.get_entity(entity_id_from_name(name))
            if entity is not None:
                resolved.append(entity)
            else:
                resolved.append(None)
                unresolved_idx.append(i)
                unresolved_names.append(name)

        if not unresolved_names:
            return [e for e in resolved if e is not None]

        # One batched embedding call for all unresolved names.
        name_vecs = self._batch_embed(unresolved_names)
        for local_i, (name, vec) in enumerate(zip(unresolved_names, name_vecs, strict=True)):
            entity = self._search_entities_hybrid(name, vec, top_k=1)
            if entity:
                resolved[unresolved_idx[local_i]] = entity[0]

        return [e for e in resolved if e is not None]

    def _search_entities_hybrid(
        self,
        name: str,
        vec: list[float] | None,
        *,
        top_k: int,
    ) -> list:
        """Embedding-first entity search with name-fuzzy fallback.

        Returns up to ``top_k`` Entity objects, ordered by relevance.
        """
        if vec:
            hits = self.graph.search_entities_by_embedding(vec, top_k=top_k)
            # Filter below threshold: prevents a cold graph from returning
            # random near-orthogonal nearest neighbors.
            good = [e for e, score in hits if score >= self._EMBED_SEARCH_THRESHOLD]
            if good:
                return good
        # Fallback: fuzzy name (substring / fulltext).
        return self.graph.search_entities(name, top_k=top_k)

    def _batch_embed(self, texts: list[str]) -> list[list[float] | None]:
        """Embed a list of short texts in one API call.

        Returns a list of same length as ``texts``. Any entry is
        ``None`` when the embedder is unavailable or the call fails —
        callers should treat that as "no embedding; use name fallback".
        """
        if not texts:
            return []
        if self.embedder is None:
            return [None] * len(texts)
        try:
            return list(self.embedder.embed_texts(texts))
        except Exception as e:
            log.warning("KG path: name embedding failed: %s", e)
            return [None] * len(texts)

    def _merge_scores(
        self,
        local: dict[str, float],
        global_: dict[str, float],
        relation: dict[str, float],
    ) -> dict[str, float]:
        """Weighted merge of all retrieval levels."""
        merged: dict[str, float] = {}
        lw = self.cfg.local_weight
        gw = self.cfg.global_weight
        rw = getattr(self.cfg, "relation_weight", 0.0)

        all_ids = set(local) | set(global_) | set(relation)
        for cid in all_ids:
            score = lw * local.get(cid, 0) + gw * global_.get(cid, 0) + rw * relation.get(cid, 0)
            merged[cid] = score

        return merged

    def _verify_chunks(
        self,
        chunk_scores: dict[str, float],
    ) -> list[ScoredChunk]:
        """Verify chunk_ids exist in the relational store.

        Uses a single ``get_chunks_by_ids`` call (batched) instead of N
        per-chunk ``get_chunk`` round-trips.
        """
        if not chunk_scores:
            return []

        rows = self.rel.get_chunks_by_ids(list(chunk_scores.keys()))
        return [
            ScoredChunk(chunk_id=r["chunk_id"], score=chunk_scores[r["chunk_id"]], source="kg")
            for r in rows
            if r.get("chunk_id") in chunk_scores
        ]

    def _scope_chunks(
        self,
        chunk_scores: dict[str, float],
        allowed_doc_ids: set[str],
    ) -> dict[str, float]:
        """Drop chunk_ids whose doc is outside the allowed set."""
        if not chunk_scores:
            return chunk_scores
        rows = self.rel.get_chunks_by_ids(list(chunk_scores.keys()))
        kept: dict[str, float] = {}
        for r in rows:
            cid = r.get("chunk_id")
            did = r.get("doc_id")
            if cid and did in allowed_doc_ids:
                kept[cid] = chunk_scores[cid]
        return kept

    def _scope_kg_context(self, allowed_doc_ids: set[str]) -> None:
        """Drop entities / relations whose source_doc_ids don't overlap
        the allowed set. Uses the synthesized KGContext populated during
        retrieval (it already carries source_doc_ids on each entry)."""
        def _keep(e: dict) -> bool:
            srcs = e.get("source_doc_ids")
            if not srcs:
                # Unknown provenance — err on the side of hiding
                return False
            if isinstance(srcs, (list, set, tuple)):
                return any(s in allowed_doc_ids for s in srcs)
            return srcs in allowed_doc_ids
        self.kg_context.entities = [e for e in self.kg_context.entities if _keep(e)]
        self.kg_context.relations = [r for r in self.kg_context.relations if _keep(r)]


# ---------------------------------------------------------------------------
# Module-level helpers (stateless — safe to call from worker threads).
# ---------------------------------------------------------------------------


def _collect(future, ctx: KGContext, label: str) -> tuple[dict[str, float], KGContext]:
    """Atomically harvest a worker's chunk scores and its KGContext.

    Expects ``future`` to already be done (the caller uses
    :func:`concurrent.futures.wait` with a collective timeout). If it's
    still running, the worker thread is still appending to ``ctx`` — we
    must NOT return it, or the main thread would race the worker during
    the merge. Returns an empty ctx in that case (and on exception).
    """
    if not future.done():
        log.warning("KG path: %s retrieval timed out — skipping", label)
        return {}, KGContext()
    try:
        return future.result(), ctx
    except Exception as e:
        log.warning("KG path: %s retrieval failed: %s", label, e)
        return {}, KGContext()


def _resolve_and_emit_relations(
    graph: GraphStore,
    pending: list[tuple[object, str]],
    name_cache: dict[str, str],
    ctx: KGContext,
) -> None:
    """Batch-resolve relation endpoint names and emit them into ``ctx``.

    Collects all unresolved endpoint entity IDs across ``pending``, issues
    a single ``get_entities_by_ids`` call, then appends the relation
    descriptions into ``ctx.relations``.
    """
    if not pending:
        return

    missing_ids: set[str] = set()
    for rel, _ in pending:
        if rel.source_entity not in name_cache:
            missing_ids.add(rel.source_entity)
        if rel.target_entity not in name_cache:
            missing_ids.add(rel.target_entity)
    if missing_ids:
        fetched = graph.get_entities_by_ids(list(missing_ids))
        for eid_, ent_ in fetched.items():
            name_cache[eid_] = ent_.name

    for rel, rid in pending:
        ctx.relations.append(
            {
                "source": name_cache.get(rel.source_entity, rel.source_entity),
                "target": name_cache.get(rel.target_entity, rel.target_entity),
                "keywords": rel.keywords,
                "description": rel.description,
                "_rid": rid,
            }
        )


def _merge_contexts(parts: list[KGContext]) -> KGContext:
    """Deduplicate-merge a list of KGContexts into one.

    Entities are deduped by ``_eid``, relations by ``_rid``. The
    order of ``parts`` determines precedence on ties — earlier
    contexts win.
    """
    out = KGContext()
    seen_eids: set[str] = set()
    seen_rids: set[str] = set()

    for part in parts:
        for e in part.entities:
            eid = e.get("_eid")
            if eid and eid in seen_eids:
                continue
            if eid:
                seen_eids.add(eid)
            out.entities.append(e)
        for r in part.relations:
            rid = r.get("_rid")
            if rid and rid in seen_rids:
                continue
            if rid:
                seen_rids.add(rid)
            out.relations.append(r)

    return out
