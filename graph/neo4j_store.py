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
        source_paths=set(rec.get("source_paths", [])),
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
        source_paths=set(rec.get("source_paths", [])),
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

        # Set once the first time we see an embedding and successfully
        # create the vector index. Prevents per-upsert overhead.
        self._vector_indexes_created: bool = False

        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Create constraints and indexes if they don't exist yet.

        Vector indexes require a known embedding dimension at CREATE
        time. We probe a sample entity for that. If the graph has no
        embedded entities yet (fresh install), index creation is
        deferred to the first ``upsert_entity`` with an embedding.
        """
        with self._driver.session(database=self._database) as s:
            s.run("CREATE CONSTRAINT kg_entity_id IF NOT EXISTS FOR (e:KGEntity) REQUIRE e.entity_id IS UNIQUE")
            # Best-effort full-text index for search_entities fallback
            try:
                s.run("CREATE FULLTEXT INDEX kg_entity_name IF NOT EXISTS FOR (e:KGEntity) ON EACH [e.name]")
            except Exception:
                logger.debug("Full-text index creation skipped (may already exist)")

        dim = self._probe_embedding_dimension()
        if dim:
            self._create_vector_indexes(dim)
        else:
            logger.info(
                "Neo4j vector indexes deferred — no embedded entities yet; "
                "will be created on first upsert_entity with an embedding"
            )

    def _probe_embedding_dimension(self) -> int | None:
        """Return the dimension of any existing ``name_embedding``, or None.

        Runs a single fast Cypher — just looks at one entity with an
        embedding to learn the dimension for CREATE VECTOR INDEX.
        """
        cypher = """
        MATCH (e:KGEntity)
        WHERE e.name_embedding IS NOT NULL AND size(e.name_embedding) > 0
        RETURN size(e.name_embedding) AS dim
        LIMIT 1
        """
        try:
            with self._driver.session(database=self._database) as s:
                rec = s.run(cypher).single()
                if rec:
                    return int(rec["dim"])
        except Exception as e:
            logger.debug("Embedding dimension probe failed: %s", e)
        return None

    def _create_vector_indexes(self, dim: int) -> None:
        """Create (or no-op) HNSW vector indexes on entity + relation embeddings.

        Requires Neo4j 5.11+ for the entity index and 5.15+ for the
        relationship index. Failures are logged and swallowed — the
        store still works, embedding search just returns [] via the
        Cypher fallback in the query methods.
        """
        entity_cypher = """
        CREATE VECTOR INDEX kg_entity_embedding IF NOT EXISTS
        FOR (e:KGEntity) ON e.name_embedding
        OPTIONS {
          indexConfig: {
            `vector.dimensions`: $dim,
            `vector.similarity_function`: 'cosine'
          }
        }
        """
        relation_cypher = """
        CREATE VECTOR INDEX kg_relation_embedding IF NOT EXISTS
        FOR ()-[r:RELATES_TO]-() ON r.description_embedding
        OPTIONS {
          indexConfig: {
            `vector.dimensions`: $dim,
            `vector.similarity_function`: 'cosine'
          }
        }
        """
        with self._driver.session(database=self._database) as s:
            try:
                s.run(entity_cypher, dim=dim).consume()
                logger.info("Neo4j vector index kg_entity_embedding ensured (dim=%d)", dim)
            except Exception as e:
                logger.warning(
                    "Failed to create entity vector index (Neo4j 5.11+ required): %s",
                    e,
                )
            try:
                s.run(relation_cypher, dim=dim).consume()
                logger.info("Neo4j vector index kg_relation_embedding ensured (dim=%d)", dim)
            except Exception as e:
                logger.warning(
                    "Failed to create relation vector index (Neo4j 5.15+ required): %s",
                    e,
                )
        self._vector_indexes_created = True

    def _ensure_vector_indexes_for_dim(self, dim: int) -> None:
        """Idempotent: create vector indexes the first time we see an embedding.

        Called from upsert_entity / upsert_relation so a fresh graph
        gets indexes as soon as the first embedded entity arrives.
        """
        if self._vector_indexes_created:
            return
        self._create_vector_indexes(dim)

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
            e.source_paths    = $source_paths,
            e._type_counts    = $entity_type + ':1',
            e.name_embedding  = $name_embedding
        ON MATCH SET
            e.description = CASE
                WHEN e.description CONTAINS $description THEN e.description
                ELSE e.description + '\n' + $description
            END,
            e.source_doc_ids  = apoc.coll.toSet(e.source_doc_ids + $source_doc_ids),
            e.source_chunk_ids = apoc.coll.toSet(e.source_chunk_ids + $source_chunk_ids),
            e.source_paths    = apoc.coll.toSet(coalesce(e.source_paths, []) + $source_paths),
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
            e.source_paths     = $source_paths,
            e.name_embedding   = $name_embedding
        ON MATCH SET
            e.description = CASE
                WHEN e.description CONTAINS $description THEN e.description
                ELSE e.description + '\n' + $description
            END,
            e.source_doc_ids   = e.source_doc_ids + [x IN $source_doc_ids WHERE NOT x IN e.source_doc_ids],
            e.source_chunk_ids = e.source_chunk_ids + [x IN $source_chunk_ids WHERE NOT x IN e.source_chunk_ids],
            e.source_paths     = coalesce(e.source_paths, []) + [x IN $source_paths WHERE NOT x IN coalesce(e.source_paths, [])],
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
            "source_paths": sorted(entity.source_paths),
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
        # First time we see an embedding on a fresh graph, stand up the
        # vector index now (rather than waiting for next server start).
        if entity.name_embedding and not self._vector_indexes_created:
            self._ensure_vector_indexes_for_dim(len(entity.name_embedding))

    def update_entity_description(self, entity_id: str, description: str) -> None:
        """Directly replace an entity's description (no append)."""
        cypher = """
        MATCH (e:KGEntity {entity_id: $entity_id})
        SET e.description = $description
        """
        with self._driver.session(database=self._database) as s:
            s.run(cypher, entity_id=entity_id, description=description).consume()

    def update_relation_description(
        self,
        relation_id: str,
        description: str,
        description_embedding: list[float] | None = None,
    ) -> None:
        """Replace a relation's description (+ optionally its embedding).

        Refreshes ``description_embedding`` in the same Cypher write
        when provided so the vector index sees the new vector
        atomically with the new text. The relation_id is unique by
        construction, so we match on the property regardless of
        endpoint identity.
        """
        if description_embedding is not None:
            cypher = """
            MATCH ()-[r:RELATES_TO {relation_id: $relation_id}]-()
            SET r.description = $description,
                r.description_embedding = $description_embedding
            """
            params: dict[str, Any] = {
                "relation_id": relation_id,
                "description": description,
                "description_embedding": description_embedding,
            }
        else:
            cypher = """
            MATCH ()-[r:RELATES_TO {relation_id: $relation_id}]-()
            SET r.description = $description
            """
            params = {"relation_id": relation_id, "description": description}
        with self._driver.session(database=self._database) as s:
            s.run(cypher, **params).consume()

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
            r.source_paths      = $source_paths,
            r.description_embedding = $description_embedding
        ON MATCH SET
            r.description = CASE
                WHEN r.description CONTAINS $description THEN r.description
                ELSE r.description + '\n' + $description
            END,
            r.weight            = r.weight + $weight,
            r.source_doc_ids    = r.source_doc_ids + [x IN $source_doc_ids WHERE NOT x IN r.source_doc_ids],
            r.source_chunk_ids  = r.source_chunk_ids + [x IN $source_chunk_ids WHERE NOT x IN r.source_chunk_ids],
            r.source_paths      = coalesce(r.source_paths, []) + [x IN $source_paths WHERE NOT x IN coalesce(r.source_paths, [])],
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
            "source_paths": sorted(relation.source_paths),
            "description_embedding": list(relation.description_embedding) if relation.description_embedding else None,
        }
        with self._driver.session(database=self._database) as s:
            s.run(cypher, **params).consume()
        if relation.description_embedding and not self._vector_indexes_created:
            self._ensure_vector_indexes_for_dim(len(relation.description_embedding))

    # -- lookups ------------------------------------------------------------

    def get_entity(self, entity_id: str) -> Entity | None:
        cypher = """
        MATCH (e:KGEntity {entity_id: $entity_id})
        RETURN e.entity_id AS entity_id, e.name AS name,
               e.entity_type AS entity_type, e.description AS description,
               e.source_doc_ids AS source_doc_ids,
               e.source_chunk_ids AS source_chunk_ids,
               e.source_paths AS source_paths
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
               e.source_chunk_ids AS source_chunk_ids,
               e.source_paths AS source_paths
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
               node.source_chunk_ids AS source_chunk_ids,
               node.source_paths AS source_paths
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
               neighbor.source_chunk_ids AS source_chunk_ids,
               neighbor.source_paths AS source_paths
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
               r.source_chunk_ids AS source_chunk_ids,
               r.source_paths AS source_paths
        """
        with self._driver.session(database=self._database) as s:
            result = s.run(cypher, entity_id=entity_id)
            return [_relation_from_record(dict(r)) for r in result]

    def search_entities_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        path_prefix: str | None = None,
        path_prefixes_or: list[str] | None = None,
    ) -> list[tuple[Entity, float]]:
        """Cosine search over entity name embeddings using Neo4j's
        native HNSW vector index (``kg_entity_embedding``).

        Cross-lingual by virtue of a multilingual embedder: a Chinese
        query vector lands near the English name vector it encodes.

        When *path_prefix* is passed the vector query over-fetches
        by 5× and applies a WHERE over ``node.source_paths`` so the
        filter happens inside one Cypher round-trip rather than via a
        client-side post-filter.

        Returns ``[]`` if the index doesn't exist (Neo4j < 5.11, or
        no embedded entities yet) or if the query dimension doesn't
        match — failures are logged but don't raise.
        """
        if not query_embedding:
            return []
        # Collapse path_prefix + path_prefixes_or into a single list
        # passed to Cypher as $prefixes — the query accepts any match,
        # which also cleanly handles the OR-fallback case (pending
        # rename not yet drained).
        prefixes: list[str] = []
        if path_prefix:
            prefixes.append(path_prefix.rstrip("/") or "/")
        if path_prefixes_or:
            prefixes.extend(p.rstrip("/") or "/" for p in path_prefixes_or)
        if prefixes:
            fetch_k = int(top_k) * 5
            cypher = """
            CALL db.index.vector.queryNodes('kg_entity_embedding', $k, $vec)
            YIELD node, score
            WITH node, score
            WHERE any(pfx IN $prefixes WHERE
                     any(p IN coalesce(node.source_paths, [])
                         WHERE p = pfx OR p STARTS WITH pfx + '/'))
            RETURN node.entity_id AS entity_id, node.name AS name,
                   node.entity_type AS entity_type, node.description AS description,
                   node.source_doc_ids AS source_doc_ids,
                   node.source_chunk_ids AS source_chunk_ids,
                   node.source_paths AS source_paths,
                   score
            ORDER BY score DESC
            LIMIT $final_k
            """
            params = {
                "k": fetch_k,
                "vec": list(query_embedding),
                "prefixes": prefixes,
                "final_k": int(top_k),
            }
        else:
            cypher = """
            CALL db.index.vector.queryNodes('kg_entity_embedding', $k, $vec)
            YIELD node, score
            RETURN node.entity_id AS entity_id, node.name AS name,
                   node.entity_type AS entity_type, node.description AS description,
                   node.source_doc_ids AS source_doc_ids,
                   node.source_chunk_ids AS source_chunk_ids,
                   node.source_paths AS source_paths,
                   score
            """
            params = {"k": int(top_k), "vec": list(query_embedding)}
        results: list[tuple[Entity, float]] = []
        try:
            with self._driver.session(database=self._database) as s:
                for rec in s.run(cypher, **params):
                    ent = _entity_from_record(dict(rec))
                    results.append((ent, float(rec["score"])))
        except Exception as e:
            logger.warning("Neo4j entity vector search failed: %s", e)
            return []
        return results

    def search_relations_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        path_prefix: str | None = None,
        path_prefixes_or: list[str] | None = None,
    ) -> list[tuple[Relation, float]]:
        """Cosine search over relation description embeddings using
        Neo4j's native relationship vector index (``kg_relation_embedding``).

        Requires Neo4j 5.15+ (relationship vector indexes landed in
        that release). Returns ``[]`` if unavailable.

        When *path_prefix* is given, over-fetches 5× and applies a
        server-side ``WHERE`` on ``relationship.source_paths``.
        """
        if not query_embedding:
            return []
        prefixes: list[str] = []
        if path_prefix:
            prefixes.append(path_prefix.rstrip("/") or "/")
        if path_prefixes_or:
            prefixes.extend(p.rstrip("/") or "/" for p in path_prefixes_or)
        if prefixes:
            fetch_k = int(top_k) * 5
            cypher = """
            CALL db.index.vector.queryRelationships('kg_relation_embedding', $k, $vec)
            YIELD relationship, score
            WITH relationship, score
            WHERE any(pfx IN $prefixes WHERE
                     any(p IN coalesce(relationship.source_paths, [])
                         WHERE p = pfx OR p STARTS WITH pfx + '/'))
            RETURN relationship.relation_id AS relation_id,
                   startNode(relationship).entity_id AS source_entity,
                   endNode(relationship).entity_id AS target_entity,
                   relationship.keywords AS keywords,
                   relationship.description AS description,
                   relationship.weight AS weight,
                   relationship.source_doc_ids AS source_doc_ids,
                   relationship.source_chunk_ids AS source_chunk_ids,
                   relationship.source_paths AS source_paths,
                   score
            ORDER BY score DESC
            LIMIT $final_k
            """
            params = {
                "k": fetch_k,
                "vec": list(query_embedding),
                "prefixes": prefixes,
                "final_k": int(top_k),
            }
        else:
            cypher = """
            CALL db.index.vector.queryRelationships('kg_relation_embedding', $k, $vec)
            YIELD relationship, score
            RETURN relationship.relation_id AS relation_id,
                   startNode(relationship).entity_id AS source_entity,
                   endNode(relationship).entity_id AS target_entity,
                   relationship.keywords AS keywords,
                   relationship.description AS description,
                   relationship.weight AS weight,
                   relationship.source_doc_ids AS source_doc_ids,
                   relationship.source_chunk_ids AS source_chunk_ids,
                   relationship.source_paths AS source_paths,
                   score
            """
            params = {"k": int(top_k), "vec": list(query_embedding)}
        results: list[tuple[Relation, float]] = []
        try:
            with self._driver.session(database=self._database) as s:
                for rec in s.run(cypher, **params):
                    rel = _relation_from_record(dict(rec))
                    results.append((rel, float(rec["score"])))
        except Exception as e:
            logger.warning("Neo4j relation vector search failed: %s", e)
            return []
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
               node.source_chunk_ids AS source_chunk_ids,
               node.source_paths AS source_paths
        ORDER BY score DESC
        LIMIT $limit
        """
        contains_cypher = """
        MATCH (e:KGEntity)
        WHERE toLower(e.name) CONTAINS toLower($term)
        RETURN e.entity_id AS entity_id, e.name AS name,
               e.entity_type AS entity_type, e.description AS description,
               e.source_doc_ids AS source_doc_ids,
               e.source_chunk_ids AS source_chunk_ids,
               e.source_paths AS source_paths
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

    def get_all_entities(self) -> list[Entity]:
        """Page over every ``KGEntity`` in the graph.

        Buffers in memory — fine for the 30k-entity scale we run at;
        for million-entity graphs callers should prefer streaming.
        """
        cypher = "MATCH (e:KGEntity) RETURN e"
        out: list[Entity] = []
        with self._driver.session(database=self._database) as s:
            for rec in s.run(cypher):
                out.append(_entity_from_record(dict(rec["e"])))
        return out

    def get_all_relations(self) -> list[Relation]:
        """Page over every ``RELATES_TO`` edge in the graph.

        ``source_entity`` / ``target_entity`` are projected from the
        endpoint nodes' ``entity_id``s rather than read off the
        relation properties — the relation itself doesn't store them
        directly (graph topology already encodes the wiring).
        """
        cypher = """
        MATCH (src:KGEntity)-[r:RELATES_TO]->(tgt:KGEntity)
        RETURN r {.*, source_entity: src.entity_id, target_entity: tgt.entity_id} AS r
        """
        out: list[Relation] = []
        with self._driver.session(database=self._database) as s:
            for rec in s.run(cypher):
                out.append(_relation_from_record(dict(rec["r"])))
        return out

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
                           weight: r.weight,
                           source_doc_ids: r.source_doc_ids,
                           source_chunk_ids: r.source_chunk_ids}] AS edges
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
                           weight: r.weight,
                           source_doc_ids: r.source_doc_ids,
                           source_chunk_ids: r.source_chunk_ids}] AS edges
        """
        with self._driver.session(database=self._database) as s:
            result = s.run(cypher, limit=limit)
            rec = result.single()
            if rec is None:
                return {"nodes": [], "edges": []}
            return {"nodes": list(rec["nodes"]), "edges": list(rec["edges"])}

    # -- path-prefix rewrite ------------------------------------------------

    def update_paths(self, old_prefix: str, new_prefix: str) -> int:
        """
        Rewrite ``source_paths`` on all entities and relations whose
        values start with ``old_prefix`` (exact or descendant) into
        ``new_prefix + tail``. Returns the number of touched items.

        Two Cypher calls (one for entities, one for relationships) run
        in the same session. Uses list comprehension with an inline
        replace because apoc might not be present on every deployment.
        """
        old_pfx = old_prefix.rstrip("/")
        new_pfx = new_prefix.rstrip("/")

        entity_cypher = """
        MATCH (e:KGEntity)
        WHERE any(p IN coalesce(e.source_paths, [])
                  WHERE p = $old OR p STARTS WITH $old + '/')
        SET e.source_paths = [
          p IN coalesce(e.source_paths, []) |
          CASE
            WHEN p = $old THEN $new
            WHEN p STARTS WITH $old + '/' THEN $new + substring(p, size($old))
            ELSE p
          END
        ]
        RETURN count(e) AS n
        """
        relation_cypher = """
        MATCH ()-[r:RELATES_TO]-()
        WHERE any(p IN coalesce(r.source_paths, [])
                  WHERE p = $old OR p STARTS WITH $old + '/')
        SET r.source_paths = [
          p IN coalesce(r.source_paths, []) |
          CASE
            WHEN p = $old THEN $new
            WHEN p STARTS WITH $old + '/' THEN $new + substring(p, size($old))
            ELSE p
          END
        ]
        RETURN count(r) AS n
        """
        params = {"old": old_pfx, "new": new_pfx}
        touched = 0
        with self._driver.session(database=self._database) as s:
            rec = s.run(entity_cypher, **params).single()
            if rec:
                touched += int(rec["n"] or 0)
            rec = s.run(relation_cypher, **params).single()
            if rec:
                touched += int(rec["n"] or 0)
        return touched

    # -- doc-scoped subgraph -----------------------------------------------

    def get_by_doc(self, doc_id: str) -> dict:
        """Cypher-indexed override of the base scan-and-filter default.

        Uses the same ``$doc_id IN e.source_doc_ids`` predicate that
        ``delete_by_doc`` uses, so behaviour is consistent across the
        two doc-scoped operations.
        """
        cypher = """
        MATCH (e:KGEntity)
        WHERE $doc_id IN e.source_doc_ids
        RETURN e.entity_id AS entity_id
        """
        with self._driver.session(database=self._database) as session:
            result = session.run(cypher, doc_id=doc_id)
            ids = [r["entity_id"] for r in result]
        if not ids:
            return {"nodes": [], "edges": []}
        return self.get_subgraph(ids)

    def explore(
        self,
        *,
        anchors: int = 200,
        halo_cap: int = 600,
        doc_id: str | None = None,
        entity_type: str | None = None,
    ) -> dict:
        """Neo4j override — single Cypher round-trip for the whole
        anchor + halo computation. The base implementation walks
        ``get_all_entities`` + ``get_all_relations`` in Python; that's
        O(N) memory and several Bolt round-trips per call. This pushes
        the same logic server-side so the wire payload is just the
        final node/edge set.

        Strategy: degree(e) via ``COUNT { (e)--() }`` (Neo4j 5+ replaced
        the legacy ``size((e)--())`` form for pattern expressions).
        ORDER BY DESC LIMIT N gives the anchors. A second MATCH expands
        one hop, ORDER BY halo degree, LIMIT halo_cap. The final
        projection inlines node + edge collection so the result is
        bounded — calling ``get_subgraph(ids)`` would re-expand one
        more hop and blow the cap (e.g. 30 ids → 4k nodes on a dense
        graph). We project just the nodes in the id list and the edges
        whose both endpoints are in the list.
        """
        # Filter clause for the anchor MATCH. Built up as a Cypher
        # snippet because parametric ``WHERE $foo IS NULL OR ...``
        # patterns force Neo4j into a full scan even with indexes —
        # cheaper to just emit only the predicates we need.
        clauses: list[str] = []
        if doc_id is not None:
            clauses.append("$doc_id IN e.source_doc_ids")
        if entity_type is not None:
            clauses.append("e.entity_type = $entity_type")
        where_anchor = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        cypher = f"""
        // 1. Pick top-`anchors` entities by degree (filtered).
        MATCH (e:KGEntity)
        {where_anchor}
        WITH e, COUNT {{ (e)--() }} AS deg
        ORDER BY deg DESC
        LIMIT $anchors
        WITH collect(e) AS anchors_list
        // 2. Halo: 1-hop neighbours of anchors, ranked by their own
        //    degree, capped to `halo_cap`. Doc filter respected;
        //    entity_type filter intentionally NOT applied to halo —
        //    if you ask for "all PERSONs" you still want to see the
        //    ORGANIZATION nodes they connect to.
        UNWIND anchors_list AS a
        OPTIONAL MATCH (a)--(n:KGEntity)
        WHERE NOT n IN anchors_list
          AND ($doc_id IS NULL OR $doc_id IN n.source_doc_ids)
        WITH anchors_list, n, COUNT {{ (n)--() }} AS n_deg
        ORDER BY n_deg DESC
        WITH anchors_list, collect(DISTINCT n)[..$halo_cap] AS halo
        // 3. Union into one bounded node list, then project nodes +
        //    only the edges whose endpoints are both in that list.
        WITH [x IN anchors_list + halo WHERE x IS NOT NULL] AS all_nodes
        UNWIND all_nodes AS n
        OPTIONAL MATCH (n)-[r:RELATES_TO]-(m:KGEntity)
        WHERE m IN all_nodes
        WITH all_nodes, collect(DISTINCT r) AS rels
        RETURN
            [n IN all_nodes | {{id: n.entity_id, name: n.name,
                                type: n.entity_type,
                                description: n.description,
                                degree: COUNT {{ (n)--() }},
                                source_doc_ids: n.source_doc_ids,
                                source_chunk_ids: n.source_chunk_ids}}] AS nodes,
            [r IN rels  | {{source: startNode(r).entity_id,
                            target: endNode(r).entity_id,
                            keywords: r.keywords,
                            description: r.description,
                            weight: r.weight,
                            source_doc_ids: r.source_doc_ids,
                            source_chunk_ids: r.source_chunk_ids}}] AS edges
        """
        with self._driver.session(database=self._database) as session:
            rec = session.run(
                cypher,
                anchors=anchors,
                halo_cap=halo_cap,
                doc_id=doc_id,
                entity_type=entity_type,
            ).single()
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
        # Neo4j's native vector index auto-maintains entries as nodes /
        # relationships are deleted — no client-side index bookkeeping
        # needed. We still collect the deleted ID lists for callers
        # that want to know how many were removed.
        deleted_rids: list[str] = []
        deleted_eids: list[str] = []
        with self._driver.session(database=self._database) as s:
            res = s.run(cypher_rels, **params).single()
            if res and res["deleted_rids"]:
                deleted_rids = list(res["deleted_rids"])
            res = s.run(cypher_nodes, **params).single()
            if res and res["deleted_eids"]:
                deleted_eids = list(res["deleted_eids"])
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
        # Neo4j native vector index auto-syncs on delete — no client work.
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
