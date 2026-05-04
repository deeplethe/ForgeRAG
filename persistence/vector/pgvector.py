"""
pgvector VectorStore.

Shares the Store's SQLAlchemy engine so we don't open a second
connection pool. Assumes the caller has ensured the pgvector
extension is loaded and `chunks.embedding` column exists
(see pgvector_ensure_column below -- called from ensure_schema).

Distance operators (pgvector):
    <->   L2
    <=>   cosine distance (1 - cosine_sim)
    <#>   negative inner product

Higher-is-better scores are produced by post-processing.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from config import PgvectorConfig

from .base import VectorHit, VectorItem

log = logging.getLogger(__name__)


_DISTANCE_OPS = {
    "cosine": "<=>",
    "l2": "<->",
    "ip": "<#>",
}

_OP_CLASS = {
    "cosine": "vector_cosine_ops",
    "l2": "vector_l2_ops",
    "ip": "vector_ip_ops",
}


class PgvectorStore:
    backend = "pgvector"

    def __init__(self, cfg: PgvectorConfig, relational_store):
        self.cfg = cfg
        self.dimension = cfg.dimension
        self._rel = relational_store

    def _engine(self):
        eng = getattr(self._rel, "_engine", None)
        if eng is None:
            raise RuntimeError("pgvector: underlying Store is not connected")
        return eng

    # -------------------------------------------------------------------
    def connect(self) -> None:
        self._engine()  # existence check

    def close(self) -> None:
        pass

    # -------------------------------------------------------------------
    def ensure_schema(self) -> None:
        """
        Enable pgvector, make sure chunks.embedding exists with the
        configured dimension, and create the ANN index if requested.
        Safe to call repeatedly.
        """
        with self._engine().begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text(f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector({self.dimension})"))
            if self.cfg.index_type == "none":
                return
            op_class = _OP_CLASS[self.cfg.distance]
            if self.cfg.index_type == "hnsw":
                sql = (
                    f"CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw "
                    f"ON chunks USING hnsw (embedding {op_class}) "
                    f"WITH (m = {self.cfg.hnsw_m}, "
                    f"ef_construction = {self.cfg.hnsw_ef_construction})"
                )
            else:  # ivfflat
                sql = (
                    f"CREATE INDEX IF NOT EXISTS idx_chunks_embedding_ivfflat "
                    f"ON chunks USING ivfflat (embedding {op_class}) "
                    f"WITH (lists = 100)"
                )
            try:
                conn.execute(text(sql))
            except Exception as e:
                log.warning("pgvector index creation failed: %s", e)

    # -------------------------------------------------------------------
    def upsert(self, items: list[VectorItem]) -> None:
        if not items:
            return
        sql = text("UPDATE chunks SET embedding = CAST(:vec AS vector) WHERE chunk_id = :cid")
        with self._engine().begin() as conn:
            for it in items:
                conn.execute(
                    sql,
                    {"vec": _format_vector(it.embedding), "cid": it.chunk_id},
                )

    # -------------------------------------------------------------------
    def delete_chunks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        sql = text("UPDATE chunks SET embedding = NULL WHERE chunk_id = ANY(:ids)")
        with self._engine().begin() as conn:
            conn.execute(sql, {"ids": chunk_ids})

    def delete_parse_version(self, doc_id: str, parse_version: int) -> None:
        # Relational delete_parse_version removes the row entirely
        # so embeddings disappear with it; nothing extra to do.
        return

    # -------------------------------------------------------------------
    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        op = _DISTANCE_OPS[self.cfg.distance]
        where_parts = ["embedding IS NOT NULL"]
        params: dict[str, Any] = {
            "qvec": _format_vector(query_vector),
            "top_k": top_k,
        }
        if filter:
            # Coalesce path keys (path_prefixes / path_prefix /
            # path_prefix_or) into one OR'd prefix list. Multi-prefix
            # scope comes from the multi-user authz layer; the legacy
            # single-prefix and rename-fallback keys are merged for
            # back-compat during the transition.
            merged_prefixes: list[str] = []
            collapsed = False
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
                        merged_prefixes = []
                        collapsed = True
                        break
                    p = p.rstrip("/")
                    if p and p not in merged_prefixes:
                        merged_prefixes.append(p)
                if collapsed:
                    break
            if merged_prefixes:
                # Native B-tree prefix scans on chunks.path
                # (ix_chunks_path_prefix). Multi-prefix queries OR
                # together; size in practice is dominated by the user's
                # accessible folder set.
                or_chunks: list[str] = []
                for i, pfx in enumerate(merged_prefixes):
                    pname = f"pp{i}"
                    or_chunks.append(
                        f"(chunks.path = :{pname}_eq OR chunks.path LIKE :{pname}_lk)"
                    )
                    params[f"{pname}_eq"] = pfx
                    params[f"{pname}_lk"] = pfx + "/%"
                where_parts.append("(" + " OR ".join(or_chunks) + ")")

            for i, (k, v) in enumerate(filter.items()):
                if k in ("path_prefix", "path_prefix_or", "path_prefixes"):
                    continue  # already handled above
                # Exact-match filters on low-cardinality columns
                if k in ("doc_id", "parse_version", "node_id", "content_type"):
                    pname = f"f{i}"
                    where_parts.append(f"{k} = :{pname}")
                    params[pname] = v
                    continue
        where = " AND ".join(where_parts)

        sql = text(
            f"SELECT chunk_id, doc_id, parse_version, node_id, "
            f"content_type, page_start, page_end, "
            f"embedding {op} CAST(:qvec AS vector) AS distance "
            f"FROM chunks WHERE {where} "
            f"ORDER BY embedding {op} CAST(:qvec AS vector) "
            f"LIMIT :top_k"
        )

        hits: list[VectorHit] = []
        with self._engine().begin() as conn:
            rows = conn.execute(sql, params).all()
        for r in rows:
            chunk_id, doc_id, pv, node_id, ctype, ps, pe, distance = r
            hits.append(
                VectorHit(
                    chunk_id=chunk_id,
                    score=_distance_to_score(distance, self.cfg.distance),
                    doc_id=doc_id,
                    parse_version=pv,
                    metadata={
                        "node_id": node_id,
                        "content_type": ctype,
                        "page_start": ps,
                        "page_end": pe,
                    },
                )
            )
        return hits


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_vector(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _distance_to_score(distance: float, metric: str) -> float:
    if metric == "cosine":
        return 1.0 - float(distance)
    if metric == "ip":
        return -float(distance)
    return -float(distance)  # l2
