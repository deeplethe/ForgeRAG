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

from .base import Entity, GraphStore, Relation
from .faiss_index import VectorIndex

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _match_any_prefix(paths: set[str], prefix: str) -> bool:
    """True if any member of *paths* is equal to ``prefix`` or sits under it.

    A prefix of ``/legal`` matches ``/legal`` itself, ``/legal/foo``
    and ``/legal/foo/bar``, but NOT ``/legal-extra`` — we only match
    on full segment boundaries.
    """
    if not paths:
        return False
    pfx = prefix.rstrip("/") or "/"
    sep = pfx + "/"
    return any(p == pfx or p.startswith(sep) for p in paths)


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
        "source_paths": sorted(e.source_paths),
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
        source_paths=set(d.get("source_paths", [])),
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
        "source_paths": sorted(r.source_paths),
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
        source_paths=set(d.get("source_paths", [])),
        description_embedding=d.get("description_embedding", []),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class NetworkXGraphStore(GraphStore):
    """In-memory graph backed by NetworkX with JSON persistence."""

    def __init__(self, path: str = "./storage/kg.json") -> None:
        self._path = Path(path)
        self._graph: nx.DiGraph = nx.DiGraph()
        self._lock = threading.RLock()
        # FAISS-backed vector indexes for semantic search
        self._relation_idx = VectorIndex()
        self._entity_idx = VectorIndex()
        # relation_id → Relation quick-lookup cache (avoids O(n) scan per search)
        self._rel_cache: dict[str, Relation] = {}
        # Batch-mode: when > 0, ``_save()`` becomes a no-op and the dirty
        # bit is set; the deferred dump is forced by ``end_batch``. Counter
        # rather than bool so concurrent batches (one per doc-in-flight)
        # nest correctly — only the outermost ``end_batch`` flushes.
        self._batch_depth = 0
        self._batch_dirty = False
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        """Load kg.json into the in-memory graph. Three-tier strategy
        ordered by speed:

        1. **orjson** (Rust JSON parser) — bytes-mode parse that skips
           the UTF-8-to-Python-str step. ~3× faster than ``json.load``
           with roughly half the peak memory. Loads our 1.95 GB sample
           in ~10–15 s. ``json.load`` MemoryError'd here.
        2. **ijson streaming** — one entity at a time, bounded memory.
           Order of minutes for the same 1.95 GB file but never OOMs.
           Reached only if orjson is missing or the orjson load itself
           ran out of memory.
        3. **json.load** — stdlib last resort for environments where
           neither orjson nor ijson is installed. Fine for tiny fixtures
           (<100 MB), unsafe for big files.

        Whichever path supplies them, we end up with two iterables of
        node / edge dicts; the graph build is shared.
        """
        if not self._path.exists():
            logger.info("No existing KG file at %s – starting fresh", self._path)
            return

        # Warn loud on multi-GB graphs so the operator knows why backend
        # startup is slow. orjson typically OOMs above ~1 GB on a 16 GB
        # machine; we then fall back to ijson streaming which is bounded
        # but slow (≈1 minute per GB on this profile). Anything over
        # ~500 MB is the cue to start thinking about migrating to Neo4j.
        size_bytes = self._path.stat().st_size
        if size_bytes > 500 * 1024 * 1024:
            logger.warning(
                "kg.json is %.2f GB — backend startup will block on the load "
                "(orjson likely OOMs, ijson streaming fallback ≈%.0fs). "
                "Consider switching graph.backend to neo4j for graphs this size.",
                size_bytes / 1e9,
                size_bytes / 1e9 * 60,  # rough: ~60s per GB on the dev profile
            )

        nodes_iter = edges_iter = None

        # Tier 1: orjson fast path
        try:
            import orjson

            with open(self._path, "rb") as fh:
                data = orjson.loads(fh.read())
            nodes_iter = iter(data.get("nodes", []))
            edges_iter = iter(data.get("edges", []))
            logger.debug("kg.json loaded via orjson")
        except ImportError:
            logger.debug("orjson unavailable, trying ijson")
        except Exception as e:
            # Intentional broad catch — orjson can raise JSONDecodeError
            # (its name for OOM) or any number of internal errors; we
            # always want to try the streaming fallback rather than
            # failing the whole graph load.
            logger.warning(
                "orjson load failed (%s: %s); falling back to ijson streaming",
                type(e).__name__,
                e,
            )

        # Tier 2: ijson streaming fallback
        if nodes_iter is None:
            try:
                import ijson

                # ijson needs the file open across the iteration; load
                # everything into Python lists up front so we can close
                # and the graph-build code below stays uniform. Memory
                # is bounded to one entity at a time during the read,
                # then ~all entities live in the list afterwards (same
                # peak as orjson would hold; the difference is only in
                # the parse-time spike).
                nodes_acc, edges_acc = [], []
                with open(self._path, "rb") as fh:
                    for nd in ijson.items(fh, "nodes.item"):
                        nodes_acc.append(nd)
                    fh.seek(0)
                    for ed in ijson.items(fh, "edges.item"):
                        edges_acc.append(ed)
                nodes_iter = iter(nodes_acc)
                edges_iter = iter(edges_acc)
                logger.debug("kg.json loaded via ijson streaming")
            except ImportError:
                logger.debug("ijson unavailable, trying stdlib json")

        # Tier 3: stdlib json.load (will MemoryError on big files)
        if nodes_iter is None:
            try:
                with open(self._path, encoding="utf-8") as fh:
                    data = json.load(fh)
                nodes_iter = iter(data.get("nodes", []))
                edges_iter = iter(data.get("edges", []))
                logger.debug("kg.json loaded via stdlib json.load")
            except Exception:
                logger.exception("All load strategies failed for %s", self._path)
                return

        # Common build path
        try:
            n_nodes = 0
            n_edges = 0
            for nd in nodes_iter:
                ent = _entity_from_dict(nd)
                self._graph.add_node(
                    ent.entity_id, entity=ent, _type_counts=Counter({ent.entity_type: 1})
                )
                n_nodes += 1
            for ed in edges_iter:
                rel = _relation_from_dict(ed)
                self._graph.add_edge(rel.source_entity, rel.target_entity, relation=rel)
                n_edges += 1

            # Silently discard any legacy "communities" block from older
            # kg.json files written when community detection was enabled.
            # Rebuild FAISS indexes from loaded data
            self._rebuild_relation_index()
            self._rebuild_entity_index()
            logger.info(
                "Loaded KG from %s: %d entities, %d relations",
                self._path,
                n_nodes,
                n_edges,
            )
        except Exception:
            logger.exception("Failed to build graph from %s", self._path)

    def _save(self) -> None:
        # In batch mode, defer the on-disk write until end_batch(). The
        # in-memory graph stays consistent for in-process readers; only
        # the JSON file lags. This eliminates the O(N) full-graph rewrite
        # that would otherwise fire on every upsert (thousands of rewrites
        # per ingested document, dominating wall-clock time at scale).
        with self._lock:
            if self._batch_depth > 0:
                self._batch_dirty = True
                return
            self._save_locked()

    def _save_locked(self) -> None:
        """Force-write the on-disk JSON. Caller must hold ``self._lock``.

        Streaming dump: write each entity / relation to disk individually
        rather than building a single multi-GB string in memory first.
        ``json.dump`` materialises the entire ``{"nodes":[...],"edges":[
        ...]}`` payload in a 2× scratch buffer before writing — same
        10× memory explosion that broke ``_load``. Writing one item at
        a time caps memory to a single entity's footprint.

        File layout: one entity / relation per line, NO inner indent
        (``separators=(',', ':')``). The line-per-item format keeps
        ``grep`` / ``head`` / line-based diffs usable without per-glyph
        whitespace bloat — typically saves 30–40 % vs the default
        ``json.dump`` spacing on a graph dominated by float embedding
        arrays. (Indented JSON adds ``,\\n      `` between every float;
        for a 1024-dim embedding that's ~6 KB of pure whitespace.)
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        compact = (",", ":")  # no spaces between dict entries / list items
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write('{"nodes":[')
            first = True
            for n in self._graph.nodes:
                ent = self._graph.nodes[n]["entity"]
                fh.write("\n" if first else ",\n")
                json.dump(_entity_to_dict(ent), fh, ensure_ascii=False, separators=compact)
                first = False
            fh.write('\n],"edges":[')
            first = True
            for u, v in self._graph.edges:
                rel = self._graph.edges[u, v]["relation"]
                fh.write("\n" if first else ",\n")
                json.dump(_relation_to_dict(rel), fh, ensure_ascii=False, separators=compact)
                first = False
            fh.write("\n]}\n")
        tmp.replace(self._path)

    def begin_batch(self) -> None:
        """Open a write batch — defers JSON dumps until ``end_batch``.
        Counter-based so nested batches collapse into a single outer flush.
        """
        with self._lock:
            self._batch_depth += 1

    def end_batch(self) -> None:
        """Close a write batch. Outermost end_batch forces the deferred
        JSON dump if any mutation happened during the batch."""
        with self._lock:
            if self._batch_depth == 0:
                return
            self._batch_depth -= 1
            if self._batch_depth == 0 and self._batch_dirty:
                self._batch_dirty = False
                self._save_locked()

    # -- index rebuilders ---------------------------------------------------

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

    def _rebuild_entity_index(self) -> None:
        """Rebuild the entity name FAISS index from scratch.

        Enables cross-lingual entity lookup: a Chinese query like
        "蜜蜂" can find an entity named "bee" as long as the embedder
        is multilingual. Only entities that have a populated
        ``name_embedding`` are indexed (set at ingest by the
        embedding pipeline when disambiguation is enabled).
        """
        self._entity_idx.clear()
        keys, vecs = [], []
        for nid in self._graph.nodes:
            ent: Entity = self._graph.nodes[nid]["entity"]
            if ent.name_embedding:
                keys.append(ent.entity_id)
                vecs.append(ent.name_embedding)
        if keys:
            self._entity_idx.add_batch(keys, vecs)

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
                existing.source_paths |= entity.source_paths
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
            # Keep the entity-name FAISS index in sync so query-time
            # embedding search can find this entity without a rebuild.
            final_ent: Entity = self._graph.nodes[eid]["entity"]
            if final_ent.name_embedding:
                self._entity_idx.add(eid, final_ent.name_embedding)
            self._save()

    def update_entity_description(self, entity_id: str, description: str) -> None:
        """Directly replace an entity's description and persist."""
        with self._lock:
            if entity_id in self._graph:
                self._graph.nodes[entity_id]["entity"].description = description
                self._save()

    def update_relation_description(
        self,
        relation_id: str,
        description: str,
        description_embedding: list[float] | None = None,
    ) -> None:
        """Replace a relation's description (+ optionally its embedding).

        Walks the multidigraph's edges to find the one keyed by
        ``relation_id`` — relations don't have an O(1) index in the
        networkx backend, but post-summary writes are rare so the
        scan is acceptable. Refreshes the FAISS embedding mirror so
        relation-semantic search stays consistent with the new text.
        """
        with self._lock:
            for _u, _v, data in self._graph.edges(data=True):
                rel: Relation | None = data.get("relation")
                if rel is None or rel.relation_id != relation_id:
                    continue
                rel.description = description
                if description_embedding is not None:
                    rel.description_embedding = description_embedding
                    # Refresh the FAISS mirror so retrieval sees the
                    # new vector. The mirror keys by relation_id;
                    # add() is idempotent / replaces in place.
                    if hasattr(self, "_relation_idx") and self._relation_idx is not None:
                        try:
                            self._relation_idx.add(relation_id, description_embedding)
                        except Exception:
                            logger.warning(
                                "relation embedding refresh failed for %s",
                                relation_id,
                            )
                self._save()
                return

    def upsert_relation(self, relation: Relation) -> None:
        with self._lock:
            src, tgt = relation.source_entity, relation.target_entity
            # Both endpoints must exist as proper entities. The
            # ingestion pipeline guarantees this by upserting all
            # entities BEFORE any relation, with ``_parse_response``
            # auto-promoting any relation-only endpoint into a
            # real-named stub before it ever reaches the graph.
            #
            # If we still find a missing endpoint here, the upstream
            # invariant has been violated — log and skip rather than
            # silently fabricating an Entity whose ``name`` is the
            # entity-id hash. (That fabrication was the source of the
            # ``bb1c30...`` hash-label nodes in the graph viewer.)
            missing = [eid for eid in (src, tgt) if eid not in self._graph]
            if missing:
                logger.warning(
                    "upsert_relation skipped: endpoints not in graph %s "
                    "(src=%s tgt=%s, doc_ids=%s). Upstream pipeline must "
                    "upsert entities before relations.",
                    missing,
                    src,
                    tgt,
                    sorted(relation.source_doc_ids),
                )
                return
            if self._graph.has_edge(src, tgt):
                existing: Relation = self._graph.edges[src, tgt]["relation"]
                if relation.description and relation.description not in existing.description:
                    existing.description = f"{existing.description}\n{relation.description}".strip()
                existing.weight += relation.weight
                existing.source_doc_ids |= relation.source_doc_ids
                existing.source_chunk_ids |= relation.source_chunk_ids
                existing.source_paths |= relation.source_paths
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
                            "source_doc_ids": sorted(rel.source_doc_ids),
                            "source_chunk_ids": sorted(rel.source_chunk_ids),
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
                            "source_doc_ids": sorted(rel.source_doc_ids),
                            "source_chunk_ids": sorted(rel.source_chunk_ids),
                        }
                    )

            return {"nodes": nodes, "edges": edges}

    # -- entity disambiguation ----------------------------------------------

    def get_all_entities(self) -> list[Entity]:
        with self._lock:
            return [self._graph.nodes[nid]["entity"] for nid in self._graph.nodes]

    def get_all_relations(self):
        """Walk the in-memory DiGraph once. Avoids the base-class
        fallback which builds a synthetic ``get_subgraph`` over every
        entity (O(N²) join cost on big graphs)."""
        with self._lock:
            return [self._graph.edges[u, v]["relation"] for u, v in self._graph.edges]

    # -- entity semantic search ---------------------------------------------

    def search_entities_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        path_prefix: str | None = None,
        path_prefixes_or: list[str] | None = None,
    ) -> list[tuple[Entity, float]]:
        """Cosine-similarity search over entity name embeddings.

        Powers cross-lingual entity lookup when the embedder is
        multilingual: a query vector derived from "蜜蜂" will land
        close to the stored "bee" name vector.

        With *path_prefix*, over-fetches 5× and post-filters on
        ``source_paths`` before trimming to *top_k*.
        """
        with self._lock:
            if not query_embedding:
                return []
            prefixes: list[str] = []
            if path_prefix:
                prefixes.append(path_prefix)
            if path_prefixes_or:
                prefixes.extend(path_prefixes_or)
            fetch_k = top_k * 5 if prefixes else top_k
            hits = self._entity_idx.search(query_embedding, fetch_k)
            results: list[tuple[Entity, float]] = []
            for eid, score in hits:
                if eid not in self._graph:
                    continue
                ent: Entity = self._graph.nodes[eid]["entity"]
                if prefixes and not any(_match_any_prefix(ent.source_paths, p) for p in prefixes):
                    continue
                results.append((ent, score))
                if len(results) >= top_k:
                    break
            return results

    # -- relation semantic search -------------------------------------------

    def search_relations_by_embedding(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        path_prefix: str | None = None,
        path_prefixes_or: list[str] | None = None,
    ) -> list[tuple[Relation, float]]:
        with self._lock:
            if not query_embedding:
                return []
            prefixes: list[str] = []
            if path_prefix:
                prefixes.append(path_prefix)
            if path_prefixes_or:
                prefixes.extend(path_prefixes_or)
            fetch_k = top_k * 5 if prefixes else top_k
            hits = self._relation_idx.search(query_embedding, fetch_k)
            results: list[tuple[Relation, float]] = []
            for rid, score in hits:
                rel = self._rel_cache.get(rid)
                if rel is None:
                    continue
                if prefixes and not any(_match_any_prefix(rel.source_paths, p) for p in prefixes):
                    continue
                results.append((rel, score))
                if len(results) >= top_k:
                    break
            return results

    # -- path-prefix rewrite (folder rename / move) -------------------------

    def update_paths(self, old_prefix: str, new_prefix: str) -> int:
        """Rewrite every ``source_paths`` entry that starts with *old_prefix*.

        Called by FolderService when <2000 chunks are affected; the
        nightly maintenance script uses the same hook for deferred ops.
        Returns the count of (entities + relations) touched.
        """
        if not old_prefix or old_prefix == new_prefix:
            return 0
        touched = 0
        with self._lock:
            for nid in self._graph.nodes:
                ent: Entity = self._graph.nodes[nid]["entity"]
                new_set = {
                    (new_prefix + p[len(old_prefix) :]) if p == old_prefix or p.startswith(old_prefix + "/") else p
                    for p in ent.source_paths
                }
                if new_set != ent.source_paths:
                    ent.source_paths = new_set
                    touched += 1
            for u, v in self._graph.edges:
                rel: Relation = self._graph.edges[u, v]["relation"]
                new_set = {
                    (new_prefix + p[len(old_prefix) :]) if p == old_prefix or p.startswith(old_prefix + "/") else p
                    for p in rel.source_paths
                }
                if new_set != rel.source_paths:
                    rel.source_paths = new_set
                    touched += 1
            if touched:
                self._save()
        return touched

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
                # Rebuild FAISS indexes to remove stale entries. The
                # entity index matters for cross-lingual search: without
                # this rebuild, search_entities_by_embedding could
                # return FAISS hits whose underlying entity no longer
                # exists in the graph (silently dropped by the caller,
                # but wastes top-k slots).
                self._rebuild_relation_index()
                self._rebuild_entity_index()
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
                self._rebuild_entity_index()
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
        }

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._save()
