"""
Neo4j-backed knowledge graph store.

Suitable for production deployments where the graph may grow to
millions of entities/relations and requires concurrent access.

Requires the ``neo4j`` Python driver (``pip install neo4j``).
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Entity, GraphStore, Relation
from .faiss_index import VectorIndex

logger = logging.getLogger(__name__)

try:
    from neo4j import Driver, GraphDatabase  # type: ignore[import-untyped]

    _HAS_NEO4J = True
except ImportError:
    _HAS_NEO4J = False
    GraphDatabase = None  # type: ignore[assignment,misc]
    Driver = None  # type: ignore[assignment,misc]


def _require_neo4j() -> None:
    if not _HAS_NEO4J:
        raise ImportError("The neo4j Python driver is required for Neo4jGraphStore. Install it with: pip install neo4j")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity_from_record(rec: dict[str, Any]) -> Entity:
    return Entity(
        entity_id=rec["entity_id"],
        name=rec["name"],
        entity_type=rec.get("entity_type", "unknown"),
        description=rec.get("description", ""),
        source_doc_ids=set(rec.get("source_doc_ids", [])),
        source_chunk_ids=set(rec.get("source_chunk_ids", [])),
        name_embedding=list(rec.get("name_embedding") or []),
    )


def _relation_from_record(rec: dict[str, Any]) -> Relation:
    return Relation(
        relation_id=rec.get("relation_id", ""),
        source_entity=rec["source_entity"],
        target_entity=rec["target_entity"],
        keywords=rec.get("keywords", ""),
        description=rec.get("description", ""),
        weight=rec.get("weight", 1.0),
        source_doc_ids=set(rec.get("source_doc_ids", [])),
        source_chunk_ids=set(rec.get("source_chunk_ids", [])),
        description_embedding=list(rec.get("description_embedding") or []),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Neo4jGraphStore(GraphStore):
    """Knowledge graph backed by Neo4j."""

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "neo4j",
        database: str = "neo4j",
    ) -> None:
        _require_neo4j()
        self._uri = uri
        self._database = database
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))
        # Suppress noisy Neo4j GqlStatusObject warnings (e.g. "property does not exist")
        import warnings

        warnings.filterwarnings("ignore", category=DeprecationWarning, module="neo4j")
        logging.getLogger("neo4j").setLevel(logging.ERROR)
        self._ensure_indexes()

        # Client-side FAISS mirrors of the embedding properties stored
        # on Neo4j nodes/relations. Populated at startup and kept in
        # sync by upsert_entity/upsert_relation. Query-time embedding
        # search runs locally against these — no Neo4j roundtrip.
        #
        # Trade-off: doubles memory use (embeddings held both in Neo4j
        # and here) and each Neo4j instance maintains its own copy, so
        # multiple workers each pay the startup load cost. Fine up to
        # ~100k entities. Beyond that, switch to Neo4j's native vector
        # index (5.11+) — signature of these methods won't change.
        self._entity_idx = VectorIndex()
        self._relation_idx = VectorIndex()
        self._rel_cache: dict[str, Relation] = {}
        self._load_vector_indexes()

    def _ensure_indexes(self) -> None:
        """Create constraints and indexes if they don't exist yet."""
        with self._driver.session(database=self._database) as s:
            s.run("CREATE CONSTRAINT kg_entity_id IF NOT EXISTS FOR (e:KGEntity) REQUIRE e.entity_id IS UNIQUE")
            # Best-effort full-text index for search_entities
            try:
                s.run("CREATE FULLTEXT INDEX kg_entity_name IF NOT EXISTS FOR (e:KGEntity) ON EACH [e.name]")
            except Exception:
                logger.debug("Full-text index creation skipped (may already exist)")

    def _load_vector_indexes(self) -> None:
        """Pull all persisted entity/relation embeddings into local VectorIndex.

        Runs once at startup. Existing graphs that were ingested before
        this column existed will return empty embeddings — those entities
        remain unsearchable by embedding until the next re-ingest or
        until a backfill script populates ``name_embedding``.
        """
        cypher_ents = """
        MATCH (e:KGEntity)
        WHERE e.name_embedding IS NOT NULL AND size(e.name_embedding) > 0
        RETURN e.entity_id AS eid, e.name_embedding AS vec
        """
        cypher_rels = """
        MATCH ()-[r:RELATES_TO]-()
        WHERE r.description_embedding IS NOT NULL AND size(r.description_embedding) > 0
        RETURN r.relation_id AS rid,
               startNode(r).entity_id AS source_entity,
               endNode(r).entity_id AS target_entity,
               r.keywords AS keywords, r.description AS description,
               r.weight AS weight,
               r.source_doc_ids AS source_doc_ids,
               r.source_chunk_ids AS source_chunk_ids,
               r.description_embedding AS vec
        """
        ent_keys: list[str] = []
        ent_vecs: list[list[float]] = []
        rel_keys: list[str] = []
        rel_vecs: list[list[float]] = []
        try:
            with self._driver.session(database=self._database) as s:
                for rec in s.run(cypher_ents):
                    ent_keys.append(rec["eid"])
                    ent_vecs.append(list(rec["vec"]))
                for rec in s.run(cypher_rels):
                    d = dict(rec)
                    rel = _relation_from_record(d)
                    rel.description_embedding = list(d["vec"])
                    self._rel_cache[rel.relation_id] = rel
                    rel_keys.append(rel.relation_id)
                    rel_vecs.append(list(d["vec"]))
        except Exception as e:
            logger.warning("Neo4j vector index preload failed: %s", e)
            return
        if ent_keys:
            self._entity_idx.add_batch(ent_keys, ent_vecs)
        if rel_keys:
            self._relation_idx.add_batch(rel_keys, rel_vecs)
        logger.info(
            "Neo4j vector indexes loaded: %d entities, %d relations",
            len(ent_keys),
            len(rel_keys),
        )

    # -- mutations ----------------------------------------------------------

    def upsert_entity(self, entity: Entity) -> None:
        # Only write name_embedding if the entity actually has one.
        # Using COALESCE keeps existing values when an upsert that
        # lacks the embedding comes in (e.g. a metadata-only update).
        cypher = """
        MERGE (e:KGEntity {entity_id: $entity_id})
        ON CREATE SET
            e.name            = $name,
            e.entity_type     = $entity_type,
            e.description     = $description,
            e.source_doc_ids  = $source_doc_ids,
            e.source_chunk_ids = $source_chunk_ids,
            e._type_counts    = $entity_type + ':1',
            e.name_embedding  = $name_embedding
        ON MATCH SET
            e.description = CASE
                WHEN e.description CONTAINS $description THEN e.description
                ELSE e.description + '\n' + $description
            END,
            e.source_doc_ids  = apoc.coll.toSet(e.source_doc_ids + $source_doc_ids),
            e.source_chunk_ids = apoc.coll.toSet(e.source_chunk_ids + $source_chunk_ids),
            e._type_counts    = e._type_counts + ',' + $entity_type + ':1',
            e.name_embedding  = coalesce($name_embedding, e.name_embedding)
        """
        # Fallback without APOC: use plain list concatenation and dedupe in
        # application code on read.
        cypher_no_apoc = """
        MERGE (e:KGEntity {entity_id: $entity_id})
        ON CREATE SET
            e.name             = $name,
            e.entity_type      = $entity_type,
            e.description      = $description,
            e.source_doc_ids   = $source_doc_ids,
            e.source_chunk_ids = $source_chunk_ids,
            e.name_embedding   = $name_embedding
        ON MATCH SET
            e.description = CASE
                WHEN e.description CONTAINS $description THEN e.description
                ELSE e.description + '\n' + $description
            END,
            e.source_doc_ids   = e.source_doc_ids + [x IN $source_doc_ids WHERE NOT x IN e.source_doc_ids],
            e.source_chunk_ids = e.source_chunk_ids + [x IN $source_chunk_ids WHERE NOT x IN e.source_chunk_ids],
            e.entity_type      = $entity_type,
            e.name_embedding   = coalesce($name_embedding, e.name_embedding)
        """
        params = {
            "entity_id": entity.entity_id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "description": entity.description,
            "source_doc_ids": sorted(entity.source_doc_ids),
            "source_chunk_ids": sorted(entity.source_chunk_ids),
            # None tells Neo4j to leave the existing value alone (via coalesce).
            "name_embedding": list(entity.name_embedding) if entity.name_embedding else None,
        }
        with self._driver.session(database=self._database) as s:
            try:
                s.run(cypher, **params).consume()
            except Exception:
                # Fallback in a fresh session if APOC isn't available.
                with self._driver.session(database=self._database) as s2:
                    s2.run(cypher_no_apoc, **params).consume()
        # Keep the local FAISS mirror in sync so query-time search
        # sees new entities without a full reload.
        if entity.name_embedding:
            self._entity_idx.add(entity.entity_id, entity.name_embedding)

    def update_entity_description(self, entity_id: str, description: str) -> None:
        """Directly replace an entity's description (no append)."""
        cypher = """
        MATCH (e:KGEntity {entity_id: $entity_id})
        SET e.description = $description
        """
        with self._driver.session(database=self._database) as s:
            s.run(cypher, entity_id=entity_id, description=description).consume()

    def upsert_relation(self, relation: Relation) -> None:
        cypher = """
        MATCH (src:KGEntity {entity_id: $source_entity})
        MATCH (tgt:KGEntity {entity_id: $target_entity})
        MERGE (src)-[r:RELATES_TO]->(tgt)
        ON CREATE SET
            r.relation_id      = $relation_id,
            r.keywords          = $keywords,
            r.description       = $description,
            r.weight            = $weight,
            r.source_doc_ids    = $source_doc_ids,
            r.source_chunk_ids  = $source_chunk_ids,
            r.description_embedding = $description_embedding
        ON MATCH SET
            r.description = CASE
                WHEN r.description CONTAINS $description THEN r.description
                ELSE r.description + '\n' + $description
            END,
            r.weight            = r.weight + $weight,
            r.source_doc_ids    = r.source_doc_ids + [x IN $source_doc_ids WHERE NOT x IN r.source_doc_ids],
            r.source_chunk_ids  = r.source_chunk_ids + [x IN $source_chunk_ids WHERE NOT x IN r.source_chunk_ids],
            r.keywords          = CASE
                WHEN r.keywords CONTAINS $keywords THEN r.keywords
                ELSE r.keywords + ', ' + $keywords
            END,
            r.description_embedding = coalesce($description_embedding, r.description_embedding)
        """
        params = {
            "source_entity": relation.source_entity,
            "target_entity": relation.target_entity,
            "relation_id": relation.relation_id,
            "keywords": relation.keywords,
            "description": relation.description,
            "weight": relation.weight,
            "source_doc_ids": sorted(relation.source_doc_ids),
            "source_chunk_ids": sorted(relation.source_chunk_ids),
            "description_embedding": list(relation.description_embedding) if relation.description_embedding else None,
        }
        with self._driver.session(database=self._database) as s:
            s.run(cypher, **params).consume()
        # Keep local mirrors in sync.
        self._rel_cache[relation.relation_id] = relation
        if relation.description_embedding:
            self._relation_idx.add(relation.relation_id, relation.description_embedding)

    # -- lookups ------------------------------------------------------------

    def get_entity(self, entity_id: str) -> Entity | None:
        cypher = """
        MATCH (e:KGEntity {entity_id: $entity_id})
        RETURN e.entity_id AS entity_id, e.name AS name,
               e.entity_type AS entity_type, e.description AS description,
               e.source_doc_ids AS source_doc_ids,
               e.source_chunk_ids AS source_chunk_ids
        """
        with self._driver.session(database=self._database) as s:
            result = s.run(cypher, entity_id=entity_id)
            rec = result.single()
            if rec is None:
                return None
            return _entity_from_record(dict(rec))

    def get_entities_by_ids(self, entity_ids: list[str]) -> dict[str, Entity]:
        if not entity_ids:
            return {}
        cypher = """
        MATCH (e:KGEntity) WHERE e.entity_id IN $entity_ids
        RETURN e.entity_id AS entity_id, e.name AS name,
               e.entity_type AS entity_type, e.description AS description,
               e.source_doc_ids AS source_doc_ids,
               e.source_chunk_ids AS source_chunk_ids
        """
        out: dict[str, Entity] = {}
        with self._driver.session(database=self._database) as s:
            result = s.run(cypher, entity_ids=list(entity_ids))
            for rec in result:
                ent = _entity_from_record(dict(rec))
                out[ent.entity_id] = ent
        return out

    def get_neighbors(self, entity_id: str, max_hops: int = 2) -> list[Entity]:
        cypher = """
        MATCH (start:KGEntity {entity_id: $entity_id})
        CALL apoc.path.subgraphNodes(start, {
            maxLevel: $max_hops,
            relationshipFilter: 'RELATES_TO'
        }) YIELD node
        WHERE node.entity_id <> $entity_id
        RETURN node.entity_id AS entity_id, node.name AS name,
               node.entity_type AS entity_type,
               node.description AS description,
               node.source_doc_ids AS source_doc_ids,
               node.source_chunk_ids AS source_chunk_ids
        """
        cypher_no_apoc = f"""
        MATCH (start:KGEntity {{entity_id: $entity_id}})
        MATCH path = (start)-[:RELATES_TO*1..{max_hops}]-(neighbor:KGEntity)
        WHERE neighbor.entity_id <> $entity_id
        WITH DISTINCT neighbor
        RETURN neighbor.entity_id AS entity_id, neighbor.name AS name,
               neighbor.entity_type AS entity_type,
               neighbor.description AS description,
               neighbor.source_doc_ids AS source_doc_ids,
               neighbor.source_chunk_ids AS source_chunk_ids
        """
        with self._driver.session(database=self._database) as s:
            try:
                result = s.run(cypher, entity_id=entity_id, max_hops=max_hops)
                return [_entity_from_record(dict(r)) for r in result]
            except Exception:
                pass
        # Fallback in a fresh session
        with self._driver.session(database=self._database) as s:
            result = s.run(cypher_no_apoc, entity_id=entity_id)
            return [_entity_from_record(dict(r)) for r in result]

    def get_relations(self, entity_id: str) -> list[Relation]:
        cypher = """
        MATCH (e:KGEntity {entity_id: $entity_id})-[r:RELATES_TO]-(other:KGEntity)
        RETURN r.relation_id AS relation_id,
               startNode(r).entity_id AS source_entity,
               endNode(r).entity_id AS target_entity,
               r.keywords AS keywords, r.description AS description,
               r.weight AS weight,
               r.source_doc_ids AS source_doc_ids,
               r.source_chunk_ids AS source_chunk_ids
        """
        with self._driver.session(database=self._database) as s:
            result = s.run(cypher, entity_id=entity_id)
            return [_relation_from_record(dict(r)) for r in result]

    def search_entities_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[tuple[Entity, float]]:
        """Cosine search over entity name embeddings (cross-lingual aware).

        Runs against the local FAISS mirror, then materialises matched
        entities via a single ``get_entities_by_ids`` batch call. Neo4j
        is hit once for the read, not once per hit.
        """
        if not query_embedding:
            return []
        hits = self._entity_idx.search(query_embedding, top_k)
        if not hits:
            return []
        eids = [eid for eid, _ in hits]
        ent_map = self.get_entities_by_ids(eids)
        results: list[tuple[Entity, float]] = []
        for eid, score in hits:
            ent = ent_map.get(eid)
            if ent is not None:
                results.append((ent, score))
        return results

    def search_relations_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[tuple[Relation, float]]:
        if not query_embedding:
            return []
        hits = self._relation_idx.search(query_embedding, top_k)
        results: list[tuple[Relation, float]] = []
        for rid, score in hits:
            rel = self._rel_cache.get(rid)
            if rel is not None:
                results.append((rel, score))
        return results

    def search_entities(self, query: str, top_k: int = 10) -> list[Entity]:
        q = query.strip()
        if not q:
            return []
        # Try full-text index first
        # NOTE: Cypher params use $term/$limit (not $query) to avoid
        # clashing with neo4j Session.run()'s own `query` parameter.
        ft_cypher = """
        CALL db.index.fulltext.queryNodes('kg_entity_name', $term)
        YIELD node, score
        RETURN node.entity_id AS entity_id, node.name AS name,
               node.entity_type AS entity_type,
               node.description AS description,
               node.source_doc_ids AS source_doc_ids,
               node.source_chunk_ids AS source_chunk_ids
        ORDER BY score DESC
        LIMIT $limit
        """
        contains_cypher = """
        MATCH (e:KGEntity)
        WHERE toLower(e.name) CONTAINS toLower($term)
        RETURN e.entity_id AS entity_id, e.name AS name,
               e.entity_type AS entity_type, e.description AS description,
               e.source_doc_ids AS source_doc_ids,
               e.source_chunk_ids AS source_chunk_ids
        LIMIT $limit
        """
        with self._driver.session(database=self._database) as s:
            try:
                result = s.run(ft_cypher, term=q, limit=top_k)
                entities = [_entity_from_record(dict(r)) for r in result]
                if entities:
                    return entities
            except Exception:
                pass
            # Fallback: CONTAINS
            result = s.run(contains_cypher, term=q, limit=top_k)
            return [_entity_from_record(dict(r)) for r in result]

    def get_subgraph(self, entity_ids: list[str]) -> dict:
        cypher = """
        MATCH (e:KGEntity) WHERE e.entity_id IN $entity_ids
        OPTIONAL MATCH (e)-[r:RELATES_TO]-(neighbor:KGEntity)
        WITH collect(DISTINCT e) + collect(DISTINCT neighbor) AS all_nodes,
             collect(DISTINCT r) AS all_rels
        UNWIND all_nodes AS n
        WITH collect(DISTINCT n) AS nodes, all_rels
        UNWIND all_rels AS r
        WITH nodes, collect(DISTINCT r) AS rels
        RETURN
            [n IN nodes | {id: n.entity_id, name: n.name,
                           type: n.entity_type,
                           description: n.description,
                           source_doc_ids: n.source_doc_ids,
                           source_chunk_ids: n.source_chunk_ids}] AS nodes,
            [r IN rels  | {source: startNode(r).entity_id,
                           target: endNode(r).entity_id,
                           keywords: r.keywords,
                           weight: r.weight}] AS edges
        """
        with self._driver.session(database=self._database) as s:
            result = s.run(cypher, entity_ids=entity_ids)
            rec = result.single()
            if rec is None:
                return {"nodes": [], "edges": []}
            return {"nodes": list(rec["nodes"]), "edges": list(rec["edges"])}

    def get_full(self, limit: int = 500) -> dict:
        """Return the full graph (up to *limit* nodes by degree)."""
        cypher = """
        MATCH (e:KGEntity)
        OPTIONAL MATCH (e)-[r:RELATES_TO]-()
        WITH e, count(r) AS deg
        ORDER BY deg DESC
        LIMIT $limit
        WITH collect(e) AS top_nodes
        UNWIND top_nodes AS n
        OPTIONAL MATCH (n)-[r:RELATES_TO]-(m:KGEntity)
        WHERE m IN top_nodes
        WITH collect(DISTINCT n) AS nodes, collect(DISTINCT r) AS rels
        RETURN
            [n IN nodes | {id: n.entity_id, name: n.name,
                           type: n.entity_type,
                           description: n.description,
                           degree: size([(n)-[:RELATES_TO]-() | 1]),
                           source_doc_ids: n.source_doc_ids,
                           source_chunk_ids: n.source_chunk_ids}] AS nodes,
            [r IN rels  | {source: startNode(r).entity_id,
                           target: endNode(r).entity_id,
                           keywords: r.keywords,
                           description: r.description,
                           weight: r.weight}] AS edges
        """
        with self._driver.session(database=self._database) as s:
            result = s.run(cypher, limit=limit)
            rec = result.single()
            if rec is None:
                return {"nodes": [], "edges": []}
            return {"nodes": list(rec["nodes"]), "edges": list(rec["edges"])}

    # -- deletion -----------------------------------------------------------

    def delete_by_doc(self, doc_id: str) -> int:
        # Remove doc_id from source lists and clean chunk_ids by prefix;
        # delete entity/relation if source_doc_ids becomes empty.
        #
        # Cypher captures the relation_id / entity_id as a scalar BEFORE
        # the DELETE so we can RETURN the list of truly-deleted keys —
        # then we punch them out of the local FAISS mirrors without
        # rescanning all of Neo4j.
        cypher_rels = """
        MATCH ()-[r:RELATES_TO]-()
        WHERE $doc_id IN r.source_doc_ids
        SET r.source_doc_ids = [x IN r.source_doc_ids WHERE x <> $doc_id],
            r.source_chunk_ids = [x IN r.source_chunk_ids WHERE NOT x STARTS WITH $doc_prefix]
        WITH r, r.relation_id AS rid
        WHERE size(r.source_doc_ids) = 0
        DELETE r
        RETURN collect(rid) AS deleted_rids
        """
        cypher_nodes = """
        MATCH (e:KGEntity)
        WHERE $doc_id IN e.source_doc_ids
        SET e.source_doc_ids = [x IN e.source_doc_ids WHERE x <> $doc_id],
            e.source_chunk_ids = [x IN e.source_chunk_ids WHERE NOT x STARTS WITH $doc_prefix]
        WITH e, e.entity_id AS eid
        WHERE size(e.source_doc_ids) = 0
        DETACH DELETE e
        RETURN collect(eid) AS deleted_eids
        """
        params = {"doc_id": doc_id, "doc_prefix": doc_id + ":"}
        deleted_rids: list[str] = []
        deleted_eids: list[str] = []
        with self._driver.session(database=self._database) as s:
            res = s.run(cypher_rels, **params).single()
            if res and res["deleted_rids"]:
                deleted_rids = list(res["deleted_rids"])
            res = s.run(cypher_nodes, **params).single()
            if res and res["deleted_eids"]:
                deleted_eids = list(res["deleted_eids"])
        # Sync local mirrors — idempotent if a key never had an embedding.
        for rid in deleted_rids:
            self._relation_idx.remove(rid)
            self._rel_cache.pop(rid, None)
        for eid in deleted_eids:
            self._entity_idx.remove(eid)
        return len(deleted_rids) + len(deleted_eids)

    def cleanup_orphans(self, valid_doc_ids: set[str]) -> dict:
        """Remove entities/relations whose source docs no longer exist."""
        valid = sorted(valid_doc_ids)

        # Clean relations: remove dead doc refs + chunk refs, delete if empty.
        # Capture deleted relation_ids so we can punch them out of the
        # local FAISS mirror precisely.
        cypher_rels = """
        MATCH ()-[r:RELATES_TO]-()
        WHERE any(d IN r.source_doc_ids WHERE NOT d IN $valid)
        SET r.source_doc_ids = [x IN r.source_doc_ids WHERE x IN $valid],
            r.source_chunk_ids = [c IN r.source_chunk_ids
                WHERE any(v IN $valid WHERE c STARTS WITH v + ':')]
        WITH r, r.relation_id AS rid
        WHERE size(r.source_doc_ids) = 0
        DELETE r
        RETURN collect(rid) AS deleted_rids
        """
        cypher_nodes = """
        MATCH (e:KGEntity)
        WHERE any(d IN e.source_doc_ids WHERE NOT d IN $valid)
        SET e.source_doc_ids = [x IN e.source_doc_ids WHERE x IN $valid],
            e.source_chunk_ids = [c IN e.source_chunk_ids
                WHERE any(v IN $valid WHERE c STARTS WITH v + ':')]
        WITH e, e.entity_id AS eid
        WHERE size(e.source_doc_ids) = 0
        DETACH DELETE e
        RETURN collect(eid) AS deleted_eids
        """
        deleted_rids: list[str] = []
        deleted_eids: list[str] = []
        with self._driver.session(database=self._database) as s:
            res = s.run(cypher_rels, valid=valid).single()
            if res and res["deleted_rids"]:
                deleted_rids = list(res["deleted_rids"])
            res = s.run(cypher_nodes, valid=valid).single()
            if res and res["deleted_eids"]:
                deleted_eids = list(res["deleted_eids"])
        for rid in deleted_rids:
            self._relation_idx.remove(rid)
            self._rel_cache.pop(rid, None)
        for eid in deleted_eids:
            self._entity_idx.remove(eid)
        return {
            "removed_entities": len(deleted_eids),
            "removed_relations": len(deleted_rids),
        }

    # -- introspection ------------------------------------------------------

    def stats(self) -> dict:
        cypher = """
        OPTIONAL MATCH (e:KGEntity)
        WITH count(e) AS entities
        OPTIONAL MATCH ()-[r:RELATES_TO]->()
        RETURN entities, count(r) AS relations
        """
        with self._driver.session(database=self._database) as s:
            rec = s.run(cypher).single()
            if rec is None:
                return {"entities": 0, "relations": 0}
            return {"entities": rec["entities"], "relations": rec["relations"]}

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._driver.close()
