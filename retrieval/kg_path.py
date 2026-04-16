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

All levels produce scored chunks that are fed into the 4-way RRF merge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.retrieval import KGPathConfig
    from embedder.base import Embedder
    from graph.base import GraphStore
    from persistence.store import Store

from .types import KGContext, ScoredChunk

log = logging.getLogger(__name__)


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

        # Step 2: Local retrieval -- entity neighborhood traversal
        local_chunks = self._local_retrieval(entity_names)

        # Step 3: Global retrieval -- keyword-based entity search
        global_chunks = self._global_retrieval(keywords or entity_names)

        # Step 4: Community retrieval -- semantic search over community summaries
        community_chunks = self._community_retrieval(query)

        # Step 5: Relation semantic search
        relation_chunks = self._relation_retrieval(query)

        # Step 6: Weighted merge
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
    ) -> dict[str, float]:
        """
        Local retrieval: find entities by name -> traverse neighbors
        -> collect chunk_ids with scores based on hop distance.

        Also collects entity descriptions and relation descriptions
        into ``self.kg_context`` for synthesized context injection.

        Optimized to minimize graph round-trips: collects all neighbor
        IDs first, then resolves names in a single batch query.
        """
        from graph.base import entity_id_from_name

        chunk_scores: dict[str, float] = {}
        _seen_entities: set[str] = set()
        _seen_relations: set[str] = set()
        # Entities resolved during traversal (avoids N+1 on name lookups)
        _entity_cache: dict[str, "Entity"] = {}

        # Phase 1: Resolve seed entities
        seed_entities = []
        for name in entity_names:
            eid = entity_id_from_name(name)
            entity = self.graph.get_entity(eid)
            if entity is None:
                candidates = self.graph.search_entities(name, top_k=3)
                if not candidates:
                    continue
                entity = candidates[0]
                eid = entity.entity_id
            _entity_cache[eid] = entity
            seed_entities.append(entity)

        # Phase 2: For each seed, get relations + neighbors (these are
        # already batch-friendly per entity in both Neo4j and NetworkX)
        all_neighbor_ids: set[str] = set()
        # Collect relations per seed and defer name resolution
        _deferred_relations: list[tuple] = []  # (rel, source_eid, target_eid)

        for entity in seed_entities:
            eid = entity.entity_id

            # Collect entity description for KG context
            if eid not in _seen_entities and entity.description:
                _seen_entities.add(eid)
                self.kg_context.entities.append(
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
                score = 0.8 * rel.weight
                for cid in rel.source_chunk_ids:
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), score)

                rid = rel.relation_id
                if rid not in _seen_relations and rel.description:
                    _seen_relations.add(rid)
                    _deferred_relations.append((rel, rel.source_entity, rel.target_entity))
                    all_neighbor_ids.add(rel.source_entity)
                    all_neighbor_ids.add(rel.target_entity)

            # Neighbor entities (hop 1+)
            neighbors = self.graph.get_neighbors(eid, max_hops=self.cfg.max_hops)
            neighbor_score = 1.0 / (1 + self.cfg.max_hops)
            for neighbor in neighbors:
                _entity_cache[neighbor.entity_id] = neighbor
                if neighbor.entity_id not in _seen_entities and neighbor.description:
                    _seen_entities.add(neighbor.entity_id)
                    self.kg_context.entities.append(
                        {
                            "name": neighbor.name,
                            "type": neighbor.entity_type,
                            "description": neighbor.description,
                            "_eid": neighbor.entity_id,
                        }
                    )
                for cid in neighbor.source_chunk_ids:
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), neighbor_score)

        # Phase 3: Batch-resolve entity names for deferred relations
        # Only fetch IDs we don't already have in cache
        missing_ids = [eid for eid in all_neighbor_ids if eid not in _entity_cache]
        if missing_ids and hasattr(self.graph, "get_entities_batch"):
            batch = self.graph.get_entities_batch(missing_ids)
            _entity_cache.update(batch)

        # Now resolve relation names from cache
        for rel, src_eid, tgt_eid in _deferred_relations:
            src_name = _entity_cache[src_eid].name if src_eid in _entity_cache else src_eid
            tgt_name = _entity_cache[tgt_eid].name if tgt_eid in _entity_cache else tgt_eid
            self.kg_context.relations.append(
                {
                    "source": src_name,
                    "target": tgt_name,
                    "keywords": rel.keywords,
                    "description": rel.description,
                    "_rid": rel.relation_id,
                }
            )

        return chunk_scores

    def _global_retrieval(
        self,
        keywords: list[str],
    ) -> dict[str, float]:
        """
        Global retrieval: search entities by keywords -> collect
        chunk_ids from matched entities and their relations.

        Also collects entity descriptions into ``self.kg_context``
        (deduped against those already captured by local retrieval).
        """
        chunk_scores: dict[str, float] = {}
        # Build sets of already-seen IDs from local retrieval for dedup
        _seen_eids = {e.get("_eid") for e in self.kg_context.entities if e.get("_eid")}
        _seen_rids = {r.get("_rid") for r in self.kg_context.relations if r.get("_rid")}

        for kw in keywords:
            entities = self.graph.search_entities(kw, top_k=5)
            for rank, entity in enumerate(entities):
                # Score by search rank
                score = 1.0 / (1.0 + rank)
                for cid in entity.source_chunk_ids:
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), score)

                # Collect entity description (dedup against local)
                if entity.entity_id not in _seen_eids and entity.description:
                    _seen_eids.add(entity.entity_id)
                    self.kg_context.entities.append(
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

                    # Collect relation description (dedup against local + earlier global)
                    if rel.relation_id not in _seen_rids and rel.description:
                        _seen_rids.add(rel.relation_id)
                        src_ent = self.graph.get_entity(rel.source_entity)
                        tgt_ent = self.graph.get_entity(rel.target_entity)
                        self.kg_context.relations.append(
                            {
                                "source": src_ent.name if src_ent else rel.source_entity,
                                "target": tgt_ent.name if tgt_ent else rel.target_entity,
                                "keywords": rel.keywords,
                                "description": rel.description,
                                "_rid": rel.relation_id,
                            }
                        )

        return chunk_scores

    def _community_retrieval(self, query: str) -> dict[str, float]:
        """
        Community retrieval: embed query -> cosine search over
        community summary embeddings -> collect member chunk_ids.

        Also collects community summaries into ``self.kg_context``
        for synthesized context injection.
        """
        cw = getattr(self.cfg, "community_weight", 0.0)
        if cw <= 0 or self.embedder is None:
            return {}

        try:
            query_vec = self.embedder.embed_texts([query])[0]
        except Exception:
            return {}

        top_k = getattr(self.cfg, "community_top_k", 5)
        matches = self.graph.search_communities(query_vec, top_k=top_k)
        if not matches:
            return {}

        chunk_scores: dict[str, float] = {}
        # Collect all member entity IDs across communities first
        all_member_ids: list[str] = []
        valid_communities = []
        for community, sim_score in matches:
            if sim_score < 0.3:
                continue
            if community.summary and community.summary != community.title:
                self.kg_context.community_summaries.append(
                    {"title": community.title, "summary": community.summary}
                )
            valid_communities.append((community, sim_score))
            all_member_ids.extend(community.entity_ids)

        # Batch-fetch all member entities in one query
        if hasattr(self.graph, "get_entities_batch"):
            entity_map = self.graph.get_entities_batch(list(set(all_member_ids)))
        else:
            entity_map = {}
            for eid in set(all_member_ids):
                e = self.graph.get_entity(eid)
                if e:
                    entity_map[eid] = e

        for community, sim_score in valid_communities:
            member_set = set(community.entity_ids)
            for eid in community.entity_ids:
                entity = entity_map.get(eid)
                if entity is None:
                    continue
                for cid in entity.source_chunk_ids:
                    chunk_scores[cid] = max(chunk_scores.get(cid, 0), sim_score)
                # Relations within the community
                for rel in self.graph.get_relations(eid):
                    if rel.target_entity in member_set or rel.source_entity in member_set:
                        for cid in rel.source_chunk_ids:
                            chunk_scores[cid] = max(chunk_scores.get(cid, 0), sim_score * 0.7)

        return chunk_scores

    def _relation_retrieval(self, query: str) -> dict[str, float]:
        """
        Relation semantic search: embed query -> cosine search over
        relation description embeddings -> collect chunk_ids.

        Also collects relation descriptions into ``self.kg_context``
        (deduped against those already captured by local retrieval).
        """
        rw = getattr(self.cfg, "relation_weight", 0.0)
        if rw <= 0 or self.embedder is None:
            return {}

        try:
            query_vec = self.embedder.embed_texts([query])[0]
        except Exception:
            return {}

        top_k = getattr(self.cfg, "relation_top_k", 10)
        matches = self.graph.search_relations_by_embedding(query_vec, top_k=top_k)
        if not matches:
            return {}

        # Build set of already-collected relation IDs (from local retrieval)
        _existing_rids = {r.get("_rid") for r in self.kg_context.relations if r.get("_rid")}

        chunk_scores: dict[str, float] = {}
        for rel, sim_score in matches:
            if sim_score < 0.3:
                continue
            for cid in rel.source_chunk_ids:
                chunk_scores[cid] = max(chunk_scores.get(cid, 0), sim_score)

            # Collect relation description (dedup by relation_id)
            if rel.relation_id not in _existing_rids and rel.description:
                src_ent = self.graph.get_entity(rel.source_entity)
                tgt_ent = self.graph.get_entity(rel.target_entity)
                self.kg_context.relations.append(
                    {
                        "source": src_ent.name if src_ent else rel.source_entity,
                        "target": tgt_ent.name if tgt_ent else rel.target_entity,
                        "keywords": rel.keywords,
                        "description": rel.description,
                        "_rid": rel.relation_id,
                    }
                )
                _existing_rids.add(rel.relation_id)

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
        """Verify chunk_ids exist in the relational store."""
        if not chunk_scores:
            return []

        verified = []
        # Batch check: get chunks by ID
        for cid, score in chunk_scores.items():
            chunk = self.rel.get_chunk(cid)
            if chunk is not None:
                verified.append(
                    ScoredChunk(
                        chunk_id=cid,
                        score=score,
                        source="kg",
                    )
                )

        return verified
