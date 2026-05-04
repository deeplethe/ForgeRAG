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

Path filtering (Chroma 1.x specific encoding)
---------------------------------------------
Chroma 1.5 dropped ``$startswith`` and its ``$contains`` operator on
metadata is *array-membership*, not string-substring (see
``chromadb/api/types.py:1235``: "checks if array field contains
value"). To get correct subtree-prefix filtering we store ``path`` as
a *list* of every ancestor + the self path:

    /agriculture/00_b_eekeeping.md
        →  ["/agriculture", "/agriculture/00_b_eekeeping.md"]

Then a folder scope query becomes ``{"path": {"$contains": "/agriculture"}}``
which list-membership-checks against each chunk's ancestor list. A
single-file scope is the same shape — just the leaf path. Either case
is one clause; no ``$or`` and no ``is_file`` plumbing needed.

This encoding is **internal to ChromaStore**. Callers still pass
``path_prefix: str`` filters and read ``meta["path"]`` as a string —
the upsert encodes to list, the search restores the leaf back to a
string before returning hits, so downstream code is unaware.

Other vector backends (pgvector, qdrant, …) implement their own path
filtering using whatever native operators they support; this list
encoding is **not** propagated outside this file.
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
        # One-shot path-to-list migration. Idempotent — chunks already
        # in list form are skipped, so re-runs are essentially free.
        # Without this, legacy chunks (string ``path``) would be invisible
        # to scoped queries since ``$contains`` does array-membership and
        # a string field can't satisfy that.
        try:
            n = self._backfill_path_to_list()
            if n > 0:
                log.info("Chroma: migrated %d chunks to list-encoded path metadata", n)
        except Exception as e:
            log.warning(
                "Chroma: path-to-list backfill failed; scoped queries may miss legacy chunks: %s",
                e,
            )

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
            # Encode path as the ancestor list (see module docstring).
            # FolderService rename keeps this in sync (small ops
            # synchronously via update_paths, large ops via the nightly
            # maintenance queue + OR-fallback at query time).
            raw_path = m.get("path") if isinstance(m.get("path"), str) else it.metadata.get("path", "/")
            m["path"] = _path_ancestors(raw_path)
            metadatas.append(m)
        col.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    def update_paths(self, chunk_id_to_path: dict[str, str]) -> None:
        """Bulk-update path metadata for a batch of existing chunk_ids.
        Used by the nightly maintenance script after a deferred folder
        rename, and by FolderService for small synchronous renames.

        Caller passes ``{chunk_id: new_path_string}``; we encode each new
        path to its ancestor list (Chroma's list-typed metadata schema)
        before writing.
        """
        if not chunk_id_to_path:
            return
        col = self._ensure_collection()
        ids = list(chunk_id_to_path.keys())
        # Chroma's update() patches metadata in-place for these IDs.
        col.update(
            ids=ids,
            metadatas=[{"path": _path_ancestors(chunk_id_to_path[cid])} for cid in ids],
        )

    # -------------------------------------------------------------------
    def _backfill_path_to_list(self) -> int:
        """One-shot migration: any chunk whose ``path`` metadata is still
        a string (legacy schema, pre-list-encoding) gets re-encoded to
        the ancestor list. Idempotent — chunks already in list form are
        skipped.

        Chunks with an empty ancestor list (legacy ``path = "/"`` from
        the old default fallback) are *also* skipped — Chroma rejects
        empty-list metadata, and an empty list would never satisfy any
        folder-scope query anyway, so leaving them as legacy strings
        produces correct behaviour (string ``path`` field can't satisfy
        Chroma's array-membership ``$contains``, so they're invisible
        to scoped queries — which matches the "root-level chunk, never
        in any folder scope" semantics).

        Pages through the collection in batches so large collections
        don't blow memory. Returns the number of chunks updated.
        """
        col = self._collection
        if col is None:
            return 0
        BATCH = 1000
        offset = 0
        total = 0
        while True:
            res = col.get(limit=BATCH, offset=offset, include=["metadatas"])
            ids = res.get("ids") or []
            if not ids:
                break
            metas = res.get("metadatas") or []
            up_ids: list[str] = []
            up_metas: list[dict] = []
            for cid, meta in zip(ids, metas, strict=False):
                if not isinstance(meta, dict):
                    continue
                p = meta.get("path")
                if not isinstance(p, str):
                    continue
                ancestors = _path_ancestors(p)
                if not ancestors:
                    # Root / empty path — see method docstring.
                    continue
                up_ids.append(cid)
                up_metas.append({"path": ancestors})
            if up_ids:
                col.update(ids=up_ids, metadatas=up_metas)
                total += len(up_ids)
            if len(ids) < BATCH:
                break
            offset += BATCH
        return total

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
            # Restore ``path`` to its single-string form (the leaf of the
            # ancestor list) so downstream consumers see the same shape
            # as before the list encoding.
            meta = _restore_path_string(meta)
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


def _path_ancestors(path: str | None) -> list[str]:
    """Encode a virtual path into the ancestor list used by Chroma's
    list-typed ``path`` metadata field.

        /agriculture/00.md  →  ["/agriculture", "/agriculture/00.md"]
        /x.md               →  ["/x.md"]
        /                   →  []        (root scope = no filter)
        ""  / None          →  []

    The leaf (longest element) is the canonical "self" path; the rest
    are its ancestors. This shape is what makes ``$contains`` correctly
    answer "is scope an ancestor-or-self of this chunk's path?".
    """
    stripped = (path or "").strip("/")
    if not stripped:
        return []
    out: list[str] = []
    cur = ""
    for seg in stripped.split("/"):
        cur += "/" + seg
        out.append(cur)
    return out


def _restore_path_string(meta: dict[str, Any]) -> dict[str, Any]:
    """Replace ``path: list[str]`` with ``path: str`` (= the leaf, i.e.
    the longest element since ancestors are strict prefixes of self).

    Used in ``search()`` so callers see the same metadata shape as
    before the list encoding. Tolerant of legacy chunks that still have
    a string ``path`` (returns them unchanged).
    """
    p = meta.get("path")
    if isinstance(p, list) and p:
        out = dict(meta)
        out["path"] = max(p, key=len)
        return out
    return meta


def _build_chroma_where(filter: dict[str, Any] | None) -> dict | None:
    """
    Translate our generic filter dict to Chroma's JSON where-syntax.

    Supported keys:
      - doc_id, parse_version, node_id, content_type, path : exact match
      - path_prefixes : list of folder prefixes combined with $or. The
        primary multi-folder scope key; carries both the user's resolved
        accessible folders AND any deferred-rename OR-fallback paths.
      - path_prefix : legacy single-prefix alias. Treated as
        ``path_prefixes=[path_prefix]``. Kept so existing callers still
        compile during the multi-user transition; new code should pass
        ``path_prefixes`` directly.
      - path_prefix_or : legacy fallback-list alias. Merged into
        ``path_prefixes`` semantics.

    Unknown keys are silently dropped to stay forward-compatible with
    the generic pipeline filter shape.
    """
    if not filter:
        return None

    # Coalesce the three possible path keys into one list. Stable order
    # and dedup keep the resulting Chroma where-clause deterministic
    # (helpful for telemetry and snapshot-style tests).
    merged_prefixes: list[str] = []
    for k in ("path_prefixes", "path_prefix", "path_prefix_or"):
        v = filter.get(k)
        if v is None:
            continue
        if isinstance(v, str):
            v = [v]
        for p in v:
            if not isinstance(p, str):
                continue
            if p in ("", "/"):
                # Root prefix collapses scope to "no filter".
                merged_prefixes = []
                break
            p = p.rstrip("/")
            if p and p not in merged_prefixes:
                merged_prefixes.append(p)
        else:
            continue
        break  # outer loop terminator when "/" hit

    clauses: list[dict] = []
    if merged_prefixes:
        sub = [{"path": {"$contains": p}} for p in merged_prefixes]
        clauses.append({"$or": sub} if len(sub) > 1 else sub[0])

    for k, v in filter.items():
        if k in ("path_prefix", "path_prefix_or", "path_prefixes"):
            continue  # already handled above
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
