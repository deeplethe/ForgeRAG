"""
Abstract base class and data models for the knowledge graph store.

Every backend (NetworkX, Neo4j, ...) must implement :class:`GraphStore`.
Entities and relations are plain dataclasses so they can be serialised
cheaply and passed across layers without coupling to any driver.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def entity_id_from_name(name: str) -> str:
    """Derive a deterministic entity ID from a human-readable name.

    Lowercase + strip + SHA-256 truncated to 16 hex chars.
    """
    normalised = name.strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    """A single knowledge-graph node (concept, person, place, ...)."""

    name: str
    entity_type: str = "unknown"
    description: str = ""
    source_doc_ids: set[str] = field(default_factory=set)
    source_chunk_ids: set[str] = field(default_factory=set)
    entity_id: str = ""
    name_embedding: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.entity_id:
            self.entity_id = entity_id_from_name(self.name)


@dataclass
class Relation:
    """A directed edge between two entities."""

    source_entity: str  # entity_id
    target_entity: str  # entity_id
    keywords: str = ""
    description: str = ""
    weight: float = 1.0
    source_doc_ids: set[str] = field(default_factory=set)
    source_chunk_ids: set[str] = field(default_factory=set)
    relation_id: str = ""
    description_embedding: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.relation_id:
            self.relation_id = f"{self.source_entity}->{self.target_entity}"


@dataclass
class Community:
    """A cluster of related entities discovered via community detection."""

    community_id: str = ""
    level: int = 0
    entity_ids: list[str] = field(default_factory=list)
    title: str = ""
    summary: str = ""
    summary_embedding: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Abstract store
# ---------------------------------------------------------------------------


class GraphStore(ABC):
    """Backend-agnostic interface for knowledge-graph persistence."""

    # -- mutations ----------------------------------------------------------

    @abstractmethod
    def upsert_entity(self, entity: Entity) -> None:
        """Insert or merge an entity.

        Merge semantics when the same ``entity_id`` already exists:
        * descriptions are appended (newline-separated),
        * source sets are unioned,
        * entity_type keeps the most-common value.
        """

    @abstractmethod
    def upsert_relation(self, relation: Relation) -> None:
        """Insert or merge a relation.

        Merge semantics when the same source+target pair exists:
        * descriptions are appended,
        * weights are summed,
        * source sets are unioned.
        """

    # -- lookups ------------------------------------------------------------

    @abstractmethod
    def get_entity(self, entity_id: str) -> Entity | None:
        """Return a single entity by ID, or ``None``."""

    def get_entities_by_ids(self, entity_ids: list[str]) -> dict[str, Entity]:
        """Batch lookup. Returns ``{entity_id: Entity}`` — missing IDs are omitted.

        Default implementation loops ``get_entity`` per ID. Backends with
        network round-trips (Neo4j) MUST override with a single query.
        """
        out: dict[str, Entity] = {}
        for eid in entity_ids:
            ent = self.get_entity(eid)
            if ent is not None:
                out[eid] = ent
        return out

    @abstractmethod
    def get_neighbors(self, entity_id: str, max_hops: int = 2) -> list[Entity]:
        """BFS traversal: return all entities reachable within *max_hops*."""

    @abstractmethod
    def get_relations(self, entity_id: str) -> list[Relation]:
        """All relations where *entity_id* is source **or** target."""

    @abstractmethod
    def search_entities(self, query: str, top_k: int = 10) -> list[Entity]:
        """Fuzzy / substring name search."""

    @abstractmethod
    def get_subgraph(self, entity_ids: list[str]) -> dict:
        """Return ``{"nodes": [...], "edges": [...]}`` for visualisation.

        Includes the listed entities **plus** their direct neighbours.
        """

    def get_full(self, limit: int = 500) -> dict:
        """Return the entire graph (up to *limit* nodes) for overview visualization.

        Default implementation: gather all entity IDs and delegate to get_subgraph.
        Backends may override for efficiency.
        """
        all_entities = self.search_entities("", top_k=limit)
        if not all_entities:
            return {"nodes": [], "edges": []}
        return self.get_subgraph([e.entity_id for e in all_entities])

    # -- description update ---------------------------------------------------

    def update_entity_description(self, entity_id: str, description: str) -> None:
        """Replace an entity's description (e.g. after LLM consolidation).

        Default: get + set + upsert.  Backends may override for atomicity.
        """
        entity = self.get_entity(entity_id)
        if entity is not None:
            entity.description = description
            self.upsert_entity(entity)

    def update_relation_description(self, relation_id: str, description: str) -> None:
        """Replace a relation's description. Default: no-op (override in backends)."""

    # -- entity disambiguation ----------------------------------------------

    def get_all_entities(self) -> list[Entity]:
        """Return every entity in the store. Override for efficiency."""
        return []

    # -- community detection ------------------------------------------------

    def detect_communities(self, resolution: float = 1.0) -> list[Community]:
        """Run Leiden clustering on the graph. Override in backends."""
        return []

    def get_communities(self) -> list[Community]:
        """Return all stored communities."""
        return []

    def upsert_community(self, community: Community) -> None:
        """Store or update a community."""

    def search_communities(
        self,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[tuple[Community, float]]:
        """Cosine-similarity search over community summary embeddings.

        Returns list of (community, score) tuples sorted by score desc.
        """
        return []

    # -- relation semantic search -------------------------------------------

    def search_relations_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[tuple[Relation, float]]:
        """Cosine-similarity search over relation description embeddings.

        Returns list of (relation, score) tuples sorted by score desc.
        """
        return []

    # -- deletion -----------------------------------------------------------

    @abstractmethod
    def delete_by_doc(self, doc_id: str) -> int:
        """Remove entities/relations sourced **only** from *doc_id*.

        For items that reference other docs as well, just remove *doc_id*
        from their source sets.  Returns the number of items deleted.
        """

    # -- introspection ------------------------------------------------------

    @abstractmethod
    def stats(self) -> dict:
        """Return ``{"entities": N, "relations": N}``."""

    # -- lifecycle ----------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        """Release resources (file handles, driver connections, ...)."""
