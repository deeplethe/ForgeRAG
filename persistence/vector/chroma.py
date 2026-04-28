"""
ChromaDB VectorStore.

Two deployment modes controlled by ChromaConfig.mode:
    - "persistent": local directory, single process (dev/simple prod)
    - "http":       remote Chroma server

Chroma handles its own persistence, indexing and filtering, so this
adapter is just a thin mapper from VectorItem/VectorHit to the
Chroma client API.

Chunk metadata mirrored into Chroma:
    doc_id, parse_version, node_id, content_type, page_start, page_end
Plus any keys the caller passed in item.metadata.
"""

from __future__ import annotations

import logging
from typing import Any

from config import ChromaConfig

from .base import VectorHit, VectorItem

log = logging.getLogger(__name__)


_DISTANCE_MAP = {
    "cosine": "cosine",
    "l2": "l2",
    "ip": "ip",
}


class ChromaStore:
    backend = "chromadb"

    def __init__(self, cfg: ChromaConfig):
        self.cfg = cfg
        self.dimension = cfg.dimension
        self._client = None
        self._collection = None

    # -------------------------------------------------------------------
    def connect(self) -> None:
        try:
            import chromadb
        except ImportError as e:
            raise RuntimeError("ChromaStore requires chromadb: pip install chromadb") from e
        import chromadb

        if self.cfg.mode == "persistent":
            self._client = chromadb.PersistentClient(path=self.cfg.persist_directory)
        else:
            self._client = chromadb.HttpClient(host=self.cfg.http_host, port=self.cfg.http_port)
        log.info("ChromaStore connected mode=%s", self.cfg.mode)

    def close(self) -> None:
        self._collection = None
        self._client = None

    # -------------------------------------------------------------------
    def ensure_schema(self) -> None:
        if self._client is None:
            raise RuntimeError("ChromaStore not connected")
        metadata = {"hnsw:space": _DISTANCE_MAP[self.cfg.distance]}
        self._collection = self._client.get_or_create_collection(
            name=self.cfg.collection_name,
            metadata=metadata,
        )
        log.info("Chroma collection ready: %s", self.cfg.collection_name)

    def _ensure_collection(self):
        if self._collection is None:
            self.ensure_schema()
        return self._collection

    # -------------------------------------------------------------------
    def upsert(self, items: list[VectorItem]) -> None:
        if not items:
            return
        col = self._ensure_collection()
        ids = [it.chunk_id for it in items]
        embeddings = [it.embedding for it in items]
        metadatas = []
        for it in items:
            m = dict(it.metadata)
            m.setdefault("doc_id", it.doc_id)
            m.setdefault("parse_version", it.parse_version)
            # Denormalize path into metadata so Chroma can filter on it
            # natively. Subtree match uses ``$or`` of ``{$eq scope}`` +
            # ``{$contains "<scope>/"}`` (see ``_build_chroma_where``);
            # FolderService rename keeps this in sync (small ops sync,
            # large ops via nightly maintenance + OR-fallback at query
            # time).
            if "path" not in m:
                m["path"] = it.metadata.get("path", "/")
            metadatas.append(m)
        col.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    def update_paths(self, chunk_id_to_path: dict[str, str]) -> None:
        """Bulk-update path metadata for a batch of existing chunk_ids.
        Used by the nightly maintenance script after a deferred folder
        rename, and by FolderService for small synchronous renames.
        """
        if not chunk_id_to_path:
            return
        col = self._ensure_collection()
        ids = list(chunk_id_to_path.keys())
        # Chroma's update() patches metadata in-place for these IDs.
        col.update(
            ids=ids,
            metadatas=[{"path": chunk_id_to_path[cid]} for cid in ids],
        )

    # -------------------------------------------------------------------
    def delete_chunks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        col = self._ensure_collection()
        col.delete(ids=chunk_ids)

    def delete_parse_version(self, doc_id: str, parse_version: int) -> None:
        col = self._ensure_collection()
        col.delete(
            where={
                "$and": [
                    {"doc_id": doc_id},
                    {"parse_version": parse_version},
                ]
            }
        )

    # -------------------------------------------------------------------
    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        col = self._ensure_collection()
        where = _build_chroma_where(filter)
        res = col.query(
            query_embeddings=[query_vector],
            n_results=top_k,
            where=where,
        )
        hits: list[VectorHit] = []
        ids = res.get("ids", [[]])[0]
        dists = res.get("distances", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        for i, cid in enumerate(ids):
            dist = float(dists[i]) if i < len(dists) else 0.0
            meta = metas[i] if i < len(metas) else {}
            hits.append(
                VectorHit(
                    chunk_id=cid,
                    score=_distance_to_score(dist, self.cfg.distance),
                    doc_id=meta.get("doc_id"),
                    parse_version=meta.get("parse_version"),
                    metadata=meta,
                )
            )
        return hits


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_chroma_where(filter: dict[str, Any] | None) -> dict | None:
    """
    Translate our generic filter dict to Chroma's JSON where-syntax.

    Supported keys:
      - doc_id, parse_version, node_id, content_type, path : exact match
      - path_prefix : matches a folder subtree OR an exact-path file
      - path_prefix_or : list of prefixes, OR-combined (deferred-rename
        fallback: match new_path OR old_path so queries don't lose hits
        while Chroma lags behind Postgres on a folder rename)

    Path-prefix encoding (Chroma 1.5+ removed ``$startswith``, leaving
    only ``$contains``/``$not_contains`` plus eq / in / etc.):

        ``$or`` of two clauses against the existing ``path`` metadata —
            { "$eq":       "/legal" }              # single-doc scope
            { "$contains": "/legal/" }             # subtree scope

        Trailing slash anchors the substring at a path-component
        boundary, so ``/leg`` doesn't false-match ``/legal/x.md`` and
        ``/legal/x`` doesn't false-match ``/legal/x_other``.

    Unknown keys are silently dropped to stay forward-compatible with
    the generic pipeline filter shape.
    """
    if not filter:
        return None

    def _path_subtree(scope: str) -> dict:
        """``$or`` clause matching either the exact file at ``scope`` or
        any descendant under ``<scope>/``."""
        s = scope.rstrip("/")
        return {"$or": [
            {"path": {"$eq": s}},
            {"path": {"$contains": s + "/"}},
        ]}

    clauses: list[dict] = []
    for k, v in filter.items():
        if k == "path_prefix":
            if isinstance(v, str) and v not in ("", "/"):
                clauses.append(_path_subtree(v))
            # Root / empty prefix: no constraint (match everything).
            continue
        if k == "path_prefix_or":
            sub = []
            for pfx in v or []:
                if isinstance(pfx, str) and pfx not in ("", "/"):
                    sub.append(_path_subtree(pfx))
            if sub:
                clauses.append({"$or": sub} if len(sub) > 1 else sub[0])
            continue
        if isinstance(v, (list, set, tuple)):
            clauses.append({k: {"$in": list(v)}})
        else:
            clauses.append({k: v})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _distance_to_score(distance: float, metric: str) -> float:
    if metric == "cosine":
        return 1.0 - distance
    if metric == "ip":
        return -distance
    return -distance  # l2
