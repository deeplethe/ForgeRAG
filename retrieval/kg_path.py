"""
Knowledge Graph retrieval path.

Multi-level retrieval inspired by LightRAG:

  1. **Local**: Extract entities from query -> graph.get_neighbors()
     -> collect source chunk_ids from related entities/relations
  2. **Global**: Extract keywords from query -> graph.search_entities()
     -> high-level relationship traversal -> chunk_ids
  3. **Community**: Embed query -> cosine search over community summaries
     -> collect chunk_ids from matched community members
  4. **Relation semantic**: Embed query -> cosine search over relation
     description embeddings -> collect chunk_ids

The four levels run **in parallel** via a ThreadPoolExecutor. Each worker
writes to its own private ``KGContext`` accumulator (no shared state
during retrieval); contexts are deduplicated and merged into
``self.kg_context`` once all workers finish. All levels produce scored
chunks that are fed into the 4-way RRF merge.
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

    def search(self, query: str) -> list[ScoredChunk]:
        """
        Multi-level KG retrieval.

        Returns a ranked list of ScoredChunks sourced from the
        knowledge graph, ready for RRF merge with other paths.

        Also populates ``self.kg_context`` with entity descriptions,
        relation descriptions, and community summaries — synthesized
        knowledge that the answering layer injects into the LLM prompt
        alongside raw text chunks (inspired by LightRAG).
        """
        self.kg_context = KGContext()

        # Step 1: Extract entities and keywords from query
        entity_names, keywords = self._extract_query_entities(query)
        if not entity_names and not keywords:
            log.debug("KG path: no entities/keywords extracted from query")
            return []

        # Compute query embedding once; community & relation retrieval reuse it.
        query_vec: list[float] | None = None
        if self.embedder is not None:
            try:
                query_vec = self.embedder.embed_texts([query])[0]
            except Exception as e:
                log.warning("KG path: query embedding failed: %s", e)

        # Steps 2–5: run the four retrieval levels in parallel. Each worker
        # writes to its own KGContext — no shared mutable state during the
        # retrieval phase — and we merge-dedup at the end.
        local_ctx = KGContext()
        global_ctx = KGContext()
        community_ctx = KGContext()
        relation_ctx = KGContext()

        # Explicit pool + finally so a stuck worker cannot block search() on
        # the executor's shutdown (the `with` context manager's __exit__ does
        # shutdown(wait=True), which would defeat the collective timeout).
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kg_path")
        try:
            f_local = pool.submit(self._local_retrieval, entity_names, local_ctx)
            f_global = pool.submit(self._global_retrieval, keywords or entity_names, global_ctx)
            f_community = pool.submit(self._community_retrieval, query_vec, community_ctx)
            f_relation = pool.submit(self._relation_retrieval, query_vec, relation_ctx)

            # Collective timeout: wait once for all four futures, with ONE
            # shared budget. Previously each _result_or_empty had its own
            # 30s, so the worst case was 4×30=120s — pathological for a
            # "parallel" path.
            wait(
                [f_local, f_global, f_community, f_relation],
                timeout=_WORKER_TIMEOUT_S,
                return_when=ALL_COMPLETED,
            )
            # Snapshot each worker's scores AND its ctx together. If a
            # worker is still running, its ctx is being mutated concurrently
            # — we drop it to avoid iterating a list that's being appended
            # to from another thread.
            local_chunks, local_ctx = _collect(f_local, local_ctx, "local")
            global_chunks, global_ctx = _collect(f_global, global_ctx, "global")
            community_chunks, community_ctx = _collect(f_community, community_ctx, "community")
            relation_chunks, relation_ctx = _collect(f_relation, relation_ctx, "relation")
        finally:
            # wait=False + cancel_futures so we don't block on stragglers.
            # Still-running threads are orphaned; Neo4j's driver has its own
            # socket timeout, so they eventually die and the pool's threads
            # return naturally.
            pool.shutdown(wait=False, cancel_futures=True)

        # Merge the four (possibly-emptied) private contexts into
        # self.kg_context, deduped by _eid / _rid. Local wins on ties
        # because it's merged first.
        self.kg_context = _merge_contexts([local_ctx, global_ctx, community_ctx, relation_ctx])

        # Step 6: Weighted merge of chunk scores
        merged = self._merge_scores(
            local_chunks,
            global_chunks,
            community_chunks,
            relation_chunks,
        )

        # Step 7: Verify chunks exist and return top-k
        verified = self._verify_chunks(merged)
        top_k = sorted(verified, key=lambda s: -s.score)[: self.cfg.top_k]

        log.info(
            "KG path: entities=%d keywords=%d local=%d global=%d "
            "community=%d relation=%d merged=%d top_k=%d "
            "kg_ctx(ent=%d rel=%d comm=%d)",
            len(entity_names),
            len(keywords),
            len(local_chunks),
            len(global_chunks),
            len(community_chunks),
            len(relation_chunks),
            len(merged),
            len(top_k),
            len(self.kg_context.entities),
            len(self.kg_context.relations),
            len(self.kg_context.community_summaries),
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

        Writes entity/relation descriptions into the caller-provided
        ``ctx`` (private to this worker). Name resolution for relation
        endpoints is batched in a single ``get_entities_by_ids`` call.
        """
        from graph.base import entity_id_from_name

        chunk_scores: dict[str, float] = {}
        _seen_entities: set[str] = set()
        _seen_relations: set[str] = set()
        # entity_id → name, populated from entities we already fetched.
        _name_cache: dict[str, str] = {}
        # Relations whose endpoint names we still need to resolve in batch.
        _pending_rels: list[tuple[object, str]] = []  # (rel, rid)

        for name in entity_names:
            eid = entity_id_from_name(name)
            entity = self.graph.get_entity(eid)
            if entity is None:
                # Try fuzzy search
                candidates = self.graph.search_entities(name, top_k=3)
                if not candidates:
                    continue
                entity = candidates[0]
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

        Writes entity/relation descriptions into the caller-provided
        ``ctx``. Cross-function deduplication (against local's output)
        happens later in the merge step, not here.
        """
        chunk_scores: dict[str, float] = {}
        _seen_eids: set[str] = set()
        _seen_rids: set[str] = set()
        _name_cache: dict[str, str] = {}
        _pending_rels: list[tuple[object, str]] = []

        for kw in keywords:
            entities = self.graph.search_entities(kw, top_k=5)
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

    def _community_retrieval(
        self,
        query_vec: list[float] | None,
        ctx: KGContext,
    ) -> dict[str, float]:
        """
        Community retrieval: cosine search over community summary embeddings
        using the pre-computed query vector.

        Writes matched community summaries into the caller-provided ``ctx``.
        """
        cw = getattr(self.cfg, "community_weight", 0.0)
        if cw <= 0 or query_vec is None:
            return {}

        top_k = getattr(self.cfg, "community_top_k", 5)
        matches = self.graph.search_communities(query_vec, top_k=top_k)
        if not matches:
            return {}

        # First pass: filter matches, emit summaries, collect all member IDs.
        kept: list[tuple[object, float]] = []  # (community, sim_score)
        all_member_ids: set[str] = set()
        for community, sim_score in matches:
            if sim_score < 0.3:  # skip very low similarity
                continue

            # Collect community summary for KG context
            if community.summary and community.summary != community.title:
                ctx.community_summaries.append(
                    {
                        "title": community.title,
                        "summary": community.summary,
                    }
                )

            kept.append((community, sim_score))
            all_member_ids.update(community.entity_ids)

        # Batch-fetch every member entity across all kept communities in ONE
        # graph call, avoiding per-member round-trips on dense communities.
        entity_map = self.graph.get_entities_by_ids(list(all_member_ids)) if all_member_ids else {}

        chunk_scores: dict[str, float] = {}
        for community, sim_score in kept:
            member_set = set(community.entity_ids)
            for eid in community.entity_ids:
                entity = entity_map.get(eid)
                if entity is None:
                    continue
                for cid in entity.source_chunk_ids:
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), sim_score)
                # Relation chunks within the community. get_relations is still
                # per-entity; batching it would require a new graph method.
                for rel in self.graph.get_relations(eid):
                    if rel.target_entity in member_set or rel.source_entity in member_set:
                        for cid in rel.source_chunk_ids:
                            chunk_scores[cid] = max(chunk_scores.get(cid, 0), sim_score * 0.7)

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

    def _merge_scores(
        self,
        local: dict[str, float],
        global_: dict[str, float],
        community: dict[str, float],
        relation: dict[str, float],
    ) -> dict[str, float]:
        """Weighted merge of all retrieval levels."""
        merged: dict[str, float] = {}
        lw = self.cfg.local_weight
        gw = self.cfg.global_weight
        cw = getattr(self.cfg, "community_weight", 0.0)
        rw = getattr(self.cfg, "relation_weight", 0.0)

        all_ids = set(local) | set(global_) | set(community) | set(relation)
        for cid in all_ids:
            score = (
                lw * local.get(cid, 0)
                + gw * global_.get(cid, 0)
                + cw * community.get(cid, 0)
                + rw * relation.get(cid, 0)
            )
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

    Entities are deduped by ``_eid``, relations by ``_rid``. Community
    summaries are simply concatenated (not deduped) to preserve the
    original pre-parallelization behavior; only the community worker
    writes them, so cross-worker dedup wouldn't do anything anyway. The
    order of ``parts`` determines precedence on ties — earlier contexts
    win.
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
        out.community_summaries.extend(part.community_summaries)

    return out
