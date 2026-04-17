"""
NetworkX-backed knowledge graph store.

Suitable for development, testing, and small-to-medium knowledge graphs.
The graph is persisted to a single JSON file and loaded into memory on
initialisation.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import networkx as nx

from .base import Community, Entity, GraphStore, Relation
from .faiss_index import VectorIndex

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _entity_to_dict(e: Entity) -> dict[str, Any]:
    d: dict[str, Any] = {
        "entity_id": e.entity_id,
        "name": e.name,
        "entity_type": e.entity_type,
        "description": e.description,
        "source_doc_ids": sorted(e.source_doc_ids),
        "source_chunk_ids": sorted(e.source_chunk_ids),
    }
    if e.name_embedding:
        d["name_embedding"] = e.name_embedding
    return d


def _entity_from_dict(d: dict[str, Any]) -> Entity:
    return Entity(
        entity_id=d["entity_id"],
        name=d["name"],
        entity_type=d.get("entity_type", "unknown"),
        description=d.get("description", ""),
        source_doc_ids=set(d.get("source_doc_ids", [])),
        source_chunk_ids=set(d.get("source_chunk_ids", [])),
        name_embedding=d.get("name_embedding", []),
    )


def _relation_to_dict(r: Relation) -> dict[str, Any]:
    d: dict[str, Any] = {
        "relation_id": r.relation_id,
        "source_entity": r.source_entity,
        "target_entity": r.target_entity,
        "keywords": r.keywords,
        "description": r.description,
        "weight": r.weight,
        "source_doc_ids": sorted(r.source_doc_ids),
        "source_chunk_ids": sorted(r.source_chunk_ids),
    }
    if r.description_embedding:
        d["description_embedding"] = r.description_embedding
    return d


def _relation_from_dict(d: dict[str, Any]) -> Relation:
    return Relation(
        relation_id=d["relation_id"],
        source_entity=d["source_entity"],
        target_entity=d["target_entity"],
        keywords=d.get("keywords", ""),
        description=d.get("description", ""),
        weight=d.get("weight", 1.0),
        source_doc_ids=set(d.get("source_doc_ids", [])),
        source_chunk_ids=set(d.get("source_chunk_ids", [])),
        description_embedding=d.get("description_embedding", []),
    )


def _community_to_dict(c: Community) -> dict[str, Any]:
    d: dict[str, Any] = {
        "community_id": c.community_id,
        "level": c.level,
        "entity_ids": c.entity_ids,
        "title": c.title,
        "summary": c.summary,
    }
    if c.summary_embedding:
        d["summary_embedding"] = c.summary_embedding
    return d


def _community_from_dict(d: dict[str, Any]) -> Community:
    return Community(
        community_id=d.get("community_id", ""),
        level=d.get("level", 0),
        entity_ids=d.get("entity_ids", []),
        title=d.get("title", ""),
        summary=d.get("summary", ""),
        summary_embedding=d.get("summary_embedding", []),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class NetworkXGraphStore(GraphStore):
    """In-memory graph backed by NetworkX with JSON persistence."""

    def __init__(self, path: str = "./storage/kg.json") -> None:
        self._path = Path(path)
        self._graph: nx.DiGraph = nx.DiGraph()
        self._communities: dict[str, Community] = {}
        self._lock = threading.RLock()
        # FAISS-backed vector indexes for semantic search
        self._community_idx = VectorIndex()
        self._relation_idx = VectorIndex()
        # relation_id → Relation quick-lookup cache (avoids O(n) scan per search)
        self._rel_cache: dict[str, Relation] = {}
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            logger.info("No existing KG file at %s – starting fresh", self._path)
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            for nd in data.get("nodes", []):
                ent = _entity_from_dict(nd)
                self._graph.add_node(ent.entity_id, entity=ent, _type_counts=Counter({ent.entity_type: 1}))
            for ed in data.get("edges", []):
                rel = _relation_from_dict(ed)
                self._graph.add_edge(rel.source_entity, rel.target_entity, relation=rel)
            for cd in data.get("communities", []):
                comm = _community_from_dict(cd)
                self._communities[comm.community_id] = comm
            # Rebuild FAISS indexes from loaded data
            self._rebuild_community_index()
            self._rebuild_relation_index()
            logger.info(
                "Loaded KG from %s: %d entities, %d relations, %d communities",
                self._path,
                self._graph.number_of_nodes(),
                self._graph.number_of_edges(),
                len(self._communities),
            )
        except Exception:
            logger.exception("Failed to load KG from %s", self._path)

    def _save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            nodes = [_entity_to_dict(self._graph.nodes[n]["entity"]) for n in self._graph.nodes]
            edges = [_relation_to_dict(self._graph.edges[u, v]["relation"]) for u, v in self._graph.edges]
            communities = [_community_to_dict(c) for c in self._communities.values()]
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"nodes": nodes, "edges": edges, "communities": communities},
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
            tmp.replace(self._path)

    # -- index rebuilders ---------------------------------------------------

    def _rebuild_community_index(self) -> None:
        """Rebuild the community summary FAISS index from scratch."""
        self._community_idx.clear()
        keys, vecs = [], []
        for cid, comm in self._communities.items():
            if comm.summary_embedding:
                keys.append(cid)
                vecs.append(comm.summary_embedding)
        if keys:
            self._community_idx.add_batch(keys, vecs)

    def _rebuild_relation_index(self) -> None:
        """Rebuild the relation description FAISS index and cache from scratch."""
        self._relation_idx.clear()
        self._rel_cache.clear()
        keys, vecs = [], []
        for u, v in self._graph.edges:
            rel: Relation = self._graph.edges[u, v]["relation"]
            self._rel_cache[rel.relation_id] = rel
            if rel.description_embedding:
                keys.append(rel.relation_id)
                vecs.append(rel.description_embedding)
        if keys:
            self._relation_idx.add_batch(keys, vecs)

    # -- mutations ----------------------------------------------------------

    def upsert_entity(self, entity: Entity) -> None:
        with self._lock:
            eid = entity.entity_id
            if eid in self._graph:
                existing: Entity = self._graph.nodes[eid]["entity"]
                tc: Counter = self._graph.nodes[eid]["_type_counts"]
                # Append description
                if entity.description and entity.description not in existing.description:
                    existing.description = f"{existing.description}\n{entity.description}".strip()
                # Union sources
                existing.source_doc_ids |= entity.source_doc_ids
                existing.source_chunk_ids |= entity.source_chunk_ids
                # Track type counts, keep most common
                tc[entity.entity_type] += 1
                existing.entity_type = tc.most_common(1)[0][0]
                # Update embedding if provided and not already set
                if entity.name_embedding and not existing.name_embedding:
                    existing.name_embedding = entity.name_embedding
            else:
                self._graph.add_node(
                    eid,
                    entity=entity,
                    _type_counts=Counter({entity.entity_type: 1}),
                )
            self._save()

    def update_entity_description(self, entity_id: str, description: str) -> None:
        """Directly replace an entity's description and persist."""
        with self._lock:
            if entity_id in self._graph:
                self._graph.nodes[entity_id]["entity"].description = description
                self._save()

    def upsert_relation(self, relation: Relation) -> None:
        with self._lock:
            src, tgt = relation.source_entity, relation.target_entity
            # Ensure both endpoint nodes exist (add_edge auto-creates bare
            # nodes without the "entity" attr, which breaks _save).
            for eid in (src, tgt):
                if eid not in self._graph:
                    placeholder = Entity(
                        entity_id=eid,
                        name=eid,
                        entity_type="UNKNOWN",
                        source_doc_ids=relation.source_doc_ids.copy(),
                        source_chunk_ids=relation.source_chunk_ids.copy(),
                    )
                    self._graph.add_node(
                        eid,
                        entity=placeholder,
                        _type_counts=Counter({"UNKNOWN": 1}),
                    )
            if self._graph.has_edge(src, tgt):
                existing: Relation = self._graph.edges[src, tgt]["relation"]
                if relation.description and relation.description not in existing.description:
                    existing.description = f"{existing.description}\n{relation.description}".strip()
                existing.weight += relation.weight
                existing.source_doc_ids |= relation.source_doc_ids
                existing.source_chunk_ids |= relation.source_chunk_ids
                if relation.keywords and relation.keywords not in existing.keywords:
                    existing.keywords = f"{existing.keywords}, {relation.keywords}".strip(", ")
                # Update embedding if provided and not already set
                if relation.description_embedding and not existing.description_embedding:
                    existing.description_embedding = relation.description_embedding
            else:
                self._graph.add_edge(src, tgt, relation=relation)
            # Update relation cache + FAISS index
            final_rel: Relation = self._graph.edges[src, tgt]["relation"]
            self._rel_cache[final_rel.relation_id] = final_rel
            if final_rel.description_embedding:
                self._relation_idx.add(final_rel.relation_id, final_rel.description_embedding)
            self._save()

    # -- lookups ------------------------------------------------------------

    def get_entity(self, entity_id: str) -> Entity | None:
        if entity_id in self._graph:
            return self._graph.nodes[entity_id]["entity"]
        return None

    def get_entities_by_ids(self, entity_ids: list[str]) -> dict[str, Entity]:
        if not entity_ids:
            return {}
        with self._lock:
            return {eid: self._graph.nodes[eid]["entity"] for eid in entity_ids if eid in self._graph}

    def get_neighbors(self, entity_id: str, max_hops: int = 2) -> list[Entity]:
        with self._lock:
            if entity_id not in self._graph:
                return []
            # BFS on the undirected view so both in- and out-neighbours are found
            undirected = self._graph.to_undirected(as_view=True)
            reachable = nx.single_source_shortest_path_length(undirected, entity_id, cutoff=max_hops)
            return [
                self._graph.nodes[nid]["entity"] for nid in reachable if nid != entity_id and nid in self._graph.nodes
            ]

    def get_relations(self, entity_id: str) -> list[Relation]:
        with self._lock:
            if entity_id not in self._graph:
                return []
            rels: list[Relation] = []
            for u, v in self._graph.out_edges(entity_id):
                rels.append(self._graph.edges[u, v]["relation"])
            for u, v in self._graph.in_edges(entity_id):
                rels.append(self._graph.edges[u, v]["relation"])
            return rels

    def search_entities(self, query: str, top_k: int = 10) -> list[Entity]:
        with self._lock:
            q = query.strip().lower()
            if not q:
                return []
            results: list[Entity] = []
            for nid in self._graph.nodes:
                ent: Entity = self._graph.nodes[nid]["entity"]
                if q in ent.name.lower():
                    results.append(ent)
                    if len(results) >= top_k:
                        break
            return results

    def get_subgraph(self, entity_ids: list[str]) -> dict:
        with self._lock:
            # Collect requested entities + direct neighbours
            node_ids: set[str] = set()
            for eid in entity_ids:
                if eid in self._graph:
                    node_ids.add(eid)
                    node_ids.update(self._graph.successors(eid))
                    node_ids.update(self._graph.predecessors(eid))

            nodes = []
            for nid in node_ids:
                ent: Entity = self._graph.nodes[nid]["entity"]
                nodes.append(
                    {
                        "id": ent.entity_id,
                        "name": ent.name,
                        "type": ent.entity_type,
                        "description": ent.description,
                        "source_doc_ids": sorted(ent.source_doc_ids),
                        "source_chunk_ids": sorted(ent.source_chunk_ids),
                    }
                )

            edges = []
            for u, v in self._graph.edges:
                if u in node_ids and v in node_ids:
                    rel: Relation = self._graph.edges[u, v]["relation"]
                    edges.append(
                        {
                            "source": rel.source_entity,
                            "target": rel.target_entity,
                            "keywords": rel.keywords,
                            "weight": rel.weight,
                        }
                    )

            return {"nodes": nodes, "edges": edges}

    def get_full(self, limit: int = 500) -> dict:
        """Return the full graph up to *limit* nodes, sorted by degree (most connected first)."""
        with self._lock:
            if self._graph.number_of_nodes() == 0:
                return {"nodes": [], "edges": []}

            # Pick top-N nodes by total degree so the visualisation shows the
            # most connected / interesting part of the graph.
            undirected = self._graph.to_undirected(as_view=True)
            ranked = sorted(undirected.degree, key=lambda x: x[1], reverse=True)
            node_ids = {nid for nid, _ in ranked[:limit]}

            nodes = []
            for nid in node_ids:
                ent: Entity = self._graph.nodes[nid]["entity"]
                deg = undirected.degree(nid)
                nodes.append(
                    {
                        "id": ent.entity_id,
                        "name": ent.name,
                        "type": ent.entity_type,
                        "description": ent.description,
                        "degree": deg,
                        "source_doc_ids": sorted(ent.source_doc_ids),
                        "source_chunk_ids": sorted(ent.source_chunk_ids),
                    }
                )

            edges = []
            for u, v in self._graph.edges:
                if u in node_ids and v in node_ids:
                    rel: Relation = self._graph.edges[u, v]["relation"]
                    edges.append(
                        {
                            "source": rel.source_entity,
                            "target": rel.target_entity,
                            "keywords": rel.keywords,
                            "description": rel.description,
                            "weight": rel.weight,
                        }
                    )

            return {"nodes": nodes, "edges": edges}

    # -- entity disambiguation ----------------------------------------------

    def get_all_entities(self) -> list[Entity]:
        with self._lock:
            return [self._graph.nodes[nid]["entity"] for nid in self._graph.nodes]

    # -- community detection ------------------------------------------------

    def detect_communities(self, resolution: float = 1.0) -> list[Community]:
        """Run Leiden clustering. Requires python-igraph + leidenalg."""
        with self._lock:
            if self._graph.number_of_nodes() < 2:
                return []
            try:
                import igraph as ig
                import leidenalg
            except ImportError:
                logger.warning(
                    "Community detection requires python-igraph and leidenalg: pip install python-igraph leidenalg"
                )
                return []

            # Convert NetworkX → igraph (undirected for community detection)
            undirected = self._graph.to_undirected()
            node_list = list(undirected.nodes)
            node_index = {n: i for i, n in enumerate(node_list)}

            ig_graph = ig.Graph(n=len(node_list), directed=False)
            ig_graph.vs["name"] = node_list
            ig_edges = []
            ig_weights = []
            seen_edges: set[tuple[str, str]] = set()
            for u, v in undirected.edges:
                pair = (min(u, v), max(u, v))
                if pair in seen_edges:
                    continue
                seen_edges.add(pair)
                ig_edges.append((node_index[u], node_index[v]))
                # Use relation weight if available
                w = 1.0
                if self._graph.has_edge(u, v):
                    w = self._graph.edges[u, v]["relation"].weight
                elif self._graph.has_edge(v, u):
                    w = self._graph.edges[v, u]["relation"].weight
                ig_weights.append(max(w, 0.1))
            ig_graph.add_edges(ig_edges)

            # Run Leiden
            partition = leidenalg.find_partition(
                ig_graph,
                leidenalg.ModularityVertexPartition,
                weights=ig_weights if ig_weights else None,
                resolution_parameter=resolution,
            )

            communities: list[Community] = []
            for idx, members in enumerate(partition):
                entity_ids = [node_list[m] for m in members]
                # Build a short title from top entities
                names = []
                for eid in entity_ids[:5]:
                    ent = self._graph.nodes[eid]["entity"]
                    names.append(ent.name)
                title = ", ".join(names)
                if len(entity_ids) > 5:
                    title += f" (+{len(entity_ids) - 5})"

                communities.append(
                    Community(
                        community_id=f"c_{idx}",
                        level=0,
                        entity_ids=entity_ids,
                        title=title,
                    )
                )

            logger.info(
                "Leiden detected %d communities (resolution=%.2f)",
                len(communities),
                resolution,
            )
            return communities

    def get_communities(self) -> list[Community]:
        with self._lock:
            return list(self._communities.values())

    def upsert_community(self, community: Community) -> None:
        with self._lock:
            self._communities[community.community_id] = community
            if community.summary_embedding:
                self._community_idx.add(community.community_id, community.summary_embedding)
            self._save()

    def search_communities(
        self,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[tuple[Community, float]]:
        with self._lock:
            if not query_embedding:
                return []
            hits = self._community_idx.search(query_embedding, top_k)
            results: list[tuple[Community, float]] = []
            for cid, score in hits:
                comm = self._communities.get(cid)
                if comm is not None:
                    results.append((comm, score))
            return results

    # -- relation semantic search -------------------------------------------

    def search_relations_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
    ) -> list[tuple[Relation, float]]:
        with self._lock:
            if not query_embedding:
                return []
            hits = self._relation_idx.search(query_embedding, top_k)
            results: list[tuple[Relation, float]] = []
            for rid, score in hits:
                rel = self._rel_cache.get(rid)
                if rel is not None:
                    results.append((rel, score))
            return results

    # -- deletion -----------------------------------------------------------

    def delete_by_doc(self, doc_id: str) -> int:
        with self._lock:
            deleted = 0

            # Edges first
            edges_to_remove = []
            for u, v in list(self._graph.edges):
                rel: Relation = self._graph.edges[u, v]["relation"]
                if doc_id in rel.source_doc_ids:
                    rel.source_doc_ids.discard(doc_id)
                    # Remove chunk_ids that belong to this doc (prefix match)
                    doc_prefix = doc_id + ":"
                    rel.source_chunk_ids = {cid for cid in rel.source_chunk_ids if not cid.startswith(doc_prefix)}
                    if not rel.source_doc_ids:
                        edges_to_remove.append((u, v))
                        deleted += 1
            for u, v in edges_to_remove:
                self._graph.remove_edge(u, v)

            # Nodes
            nodes_to_remove = []
            for nid in list(self._graph.nodes):
                ent: Entity = self._graph.nodes[nid]["entity"]
                if doc_id in ent.source_doc_ids:
                    ent.source_doc_ids.discard(doc_id)
                    doc_prefix = doc_id + ":"
                    ent.source_chunk_ids = {cid for cid in ent.source_chunk_ids if not cid.startswith(doc_prefix)}
                    if not ent.source_doc_ids:
                        nodes_to_remove.append(nid)
                        deleted += 1
            for nid in nodes_to_remove:
                self._graph.remove_node(nid)

            if deleted:
                # Rebuild FAISS indexes to remove stale entries
                self._rebuild_relation_index()
                self._save()
            return deleted

    def cleanup_orphans(self, valid_doc_ids: set[str]) -> dict:
        """Remove entities and relations whose source_doc_ids are all gone.

        *valid_doc_ids* is the set of document IDs that still exist in the
        relational store.  Any entity/relation referencing only docs NOT in
        this set is deleted.

        Returns ``{"removed_entities": N, "removed_relations": N}``.
        """
        with self._lock:
            removed_edges = 0
            removed_nodes = 0

            # Edges first
            edges_to_remove = []
            for u, v in list(self._graph.edges):
                rel: Relation = self._graph.edges[u, v]["relation"]
                alive = rel.source_doc_ids & valid_doc_ids
                if not alive:
                    edges_to_remove.append((u, v))
                elif alive != rel.source_doc_ids:
                    # Partial: remove dead doc refs
                    dead = rel.source_doc_ids - alive
                    rel.source_doc_ids = alive
                    for d in dead:
                        pfx = d + ":"
                        rel.source_chunk_ids = {c for c in rel.source_chunk_ids if not c.startswith(pfx)}
            for u, v in edges_to_remove:
                self._graph.remove_edge(u, v)
                removed_edges += 1

            # Nodes
            nodes_to_remove = []
            for nid in list(self._graph.nodes):
                ent: Entity = self._graph.nodes[nid]["entity"]
                alive = ent.source_doc_ids & valid_doc_ids
                if not alive:
                    nodes_to_remove.append(nid)
                elif alive != ent.source_doc_ids:
                    dead = ent.source_doc_ids - alive
                    ent.source_doc_ids = alive
                    for d in dead:
                        pfx = d + ":"
                        ent.source_chunk_ids = {c for c in ent.source_chunk_ids if not c.startswith(pfx)}
            for nid in nodes_to_remove:
                self._graph.remove_node(nid)
                removed_nodes += 1

            if removed_edges or removed_nodes:
                self._rebuild_relation_index()
                self._save()

            return {
                "removed_entities": removed_nodes,
                "removed_relations": removed_edges,
            }

    # -- introspection ------------------------------------------------------

    def stats(self) -> dict:
        return {
            "entities": self._graph.number_of_nodes(),
            "relations": self._graph.number_of_edges(),
            "communities": len(self._communities),
        }

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._save()
