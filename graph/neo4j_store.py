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

    def _ensure_indexes(self) -> None:
        """Create constraints and indexes if they don't exist yet."""
        with self._driver.session(database=self._database) as s:
            s.run("CREATE CONSTRAINT kg_entity_id IF NOT EXISTS FOR (e:KGEntity) REQUIRE e.entity_id IS UNIQUE")
            # Best-effort full-text index for search_entities
            try:
                s.run("CREATE FULLTEXT INDEX kg_entity_name IF NOT EXISTS FOR (e:KGEntity) ON EACH [e.name]")
            except Exception:
                logger.debug("Full-text index creation skipped (may already exist)")

    # -- mutations ----------------------------------------------------------

    def upsert_entity(self, entity: Entity) -> None:
        cypher = """
        MERGE (e:KGEntity {entity_id: $entity_id})
        ON CREATE SET
            e.name            = $name,
            e.entity_type     = $entity_type,
            e.description     = $description,
            e.source_doc_ids  = $source_doc_ids,
            e.source_chunk_ids = $source_chunk_ids,
            e._type_counts    = $entity_type + ':1'
        ON MATCH SET
            e.description = CASE
                WHEN e.description CONTAINS $description THEN e.description
                ELSE e.description + '\n' + $description
            END,
            e.source_doc_ids  = apoc.coll.toSet(e.source_doc_ids + $source_doc_ids),
            e.source_chunk_ids = apoc.coll.toSet(e.source_chunk_ids + $source_chunk_ids),
            e._type_counts    = e._type_counts + ',' + $entity_type + ':1'
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
            e.source_chunk_ids = $source_chunk_ids
        ON MATCH SET
            e.description = CASE
                WHEN e.description CONTAINS $description THEN e.description
                ELSE e.description + '\n' + $description
            END,
            e.source_doc_ids   = e.source_doc_ids + [x IN $source_doc_ids WHERE NOT x IN e.source_doc_ids],
            e.source_chunk_ids = e.source_chunk_ids + [x IN $source_chunk_ids WHERE NOT x IN e.source_chunk_ids],
            e.entity_type      = $entity_type
        """
        params = {
            "entity_id": entity.entity_id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "description": entity.description,
            "source_doc_ids": sorted(entity.source_doc_ids),
            "source_chunk_ids": sorted(entity.source_chunk_ids),
        }
        with self._driver.session(database=self._database) as s:
            try:
                s.run(cypher, **params).consume()
                return
            except Exception:
                pass
        # Fallback in a fresh session
        with self._driver.session(database=self._database) as s:
            s.run(cypher_no_apoc, **params).consume()

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
            r.source_chunk_ids  = $source_chunk_ids
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
            END
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
        }
        with self._driver.session(database=self._database) as s:
            s.run(cypher, **params).consume()

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
        cypher_rels = """
        MATCH ()-[r:RELATES_TO]-()
        WHERE $doc_id IN r.source_doc_ids
        SET r.source_doc_ids = [x IN r.source_doc_ids WHERE x <> $doc_id],
            r.source_chunk_ids = [x IN r.source_chunk_ids WHERE NOT x STARTS WITH $doc_prefix]
        WITH r
        WHERE size(r.source_doc_ids) = 0
        DELETE r
        RETURN count(r) AS deleted_rels
        """
        cypher_nodes = """
        MATCH (e:KGEntity)
        WHERE $doc_id IN e.source_doc_ids
        SET e.source_doc_ids = [x IN e.source_doc_ids WHERE x <> $doc_id],
            e.source_chunk_ids = [x IN e.source_chunk_ids WHERE NOT x STARTS WITH $doc_prefix]
        WITH e
        WHERE size(e.source_doc_ids) = 0
        DETACH DELETE e
        RETURN count(e) AS deleted_nodes
        """
        params = {"doc_id": doc_id, "doc_prefix": doc_id + ":"}
        total = 0
        with self._driver.session(database=self._database) as s:
            res = s.run(cypher_rels, **params).single()
            total += res["deleted_rels"] if res else 0
            res = s.run(cypher_nodes, **params).single()
            total += res["deleted_nodes"] if res else 0
        return total

    def cleanup_orphans(self, valid_doc_ids: set[str]) -> dict:
        """Remove entities/relations whose source docs no longer exist."""
        valid = sorted(valid_doc_ids)

        # Clean relations: remove dead doc refs + chunk refs, delete if empty
        cypher_rels = """
        MATCH ()-[r:RELATES_TO]-()
        WHERE any(d IN r.source_doc_ids WHERE NOT d IN $valid)
        SET r.source_doc_ids = [x IN r.source_doc_ids WHERE x IN $valid],
            r.source_chunk_ids = [c IN r.source_chunk_ids
                WHERE any(v IN $valid WHERE c STARTS WITH v + ':')]
        WITH r
        WHERE size(r.source_doc_ids) = 0
        DELETE r
        RETURN count(r) AS removed
        """
        # Clean entities
        cypher_nodes = """
        MATCH (e:KGEntity)
        WHERE any(d IN e.source_doc_ids WHERE NOT d IN $valid)
        SET e.source_doc_ids = [x IN e.source_doc_ids WHERE x IN $valid],
            e.source_chunk_ids = [c IN e.source_chunk_ids
                WHERE any(v IN $valid WHERE c STARTS WITH v + ':')]
        WITH e
        WHERE size(e.source_doc_ids) = 0
        DETACH DELETE e
        RETURN count(e) AS removed
        """
        removed_rels = 0
        removed_nodes = 0
        with self._driver.session(database=self._database) as s:
            res = s.run(cypher_rels, valid=valid).single()
            removed_rels = res["removed"] if res else 0
            res = s.run(cypher_nodes, valid=valid).single()
            removed_nodes = res["removed"] if res else 0
        return {
            "removed_entities": removed_nodes,
            "removed_relations": removed_rels,
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
