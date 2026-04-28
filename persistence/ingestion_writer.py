"""
High-level writer that persists one document's ingestion output
atomically across the relational store and (optionally) the vector
store.

Usage:

    rel = make_relational_store(cfg.persistence.relational)
    rel.connect()
    rel.ensure_schema(
        with_vector=cfg.persistence.vector.backend == "pgvector",
        embedding_dim=_dim(cfg),
    )
    vec = make_vector_store(cfg.persistence.vector, relational_store=rel)
    vec.connect()
    vec.ensure_schema()

    writer = IngestionWriter(rel, vec)
    writer.write(doc, tree, chunks, embeddings=embs)  # embeddings optional

Atomicity:
    The relational store's transaction() wraps the entire "delete
    old version -> insert documents/pages/blocks/tree/chunks" block.
    The vector store is updated AFTER the relational transaction
    commits. If the vector write fails, we log and raise; the
    relational side is already consistent (embeddings will simply be
    missing until the next ingestion). This two-phase-ish approach
    is a deliberate simplification: we prefer "metadata is always
    right, vectors may lag" over "everything rolled back together"
    because Chroma has no 2PC with external systems.

Hard overwrite:
    On re-ingestion, the writer calls delete_parse_version for both
    the NEW parse_version (defensive) and any OLDER parse_versions
    of the same doc_id that were previously active.
"""

from __future__ import annotations

import logging
from typing import Any

from parser.schema import Chunk, DocTree, ParsedDocument

from .serde import (
    block_to_row,
    chunk_to_row,
    profile_to_dict,
    trace_to_dict,
    tree_to_dict,
)
from .store import Store as RelationalStore
from .vector.base import VectorItem, VectorStore

log = logging.getLogger(__name__)


class IngestionWriter:
    def __init__(
        self,
        relational: RelationalStore,
        vector: VectorStore | None = None,
        *,
        embedder: Any | None = None,
    ):
        """
        embedder: optional Embedder instance. When present and
            write() is called without precomputed embeddings, the
            writer will invoke embedder.embed_chunks() after the
            relational transaction commits and before the vector
            store upsert. This is the "inline hook" path; for the
            "independent stage" path, leave embedder=None and pass
            precomputed `embeddings` to write().
        """
        self.rel = relational
        self.vec = vector
        self.embedder = embedder

    # ------------------------------------------------------------------
    def write(
        self,
        doc: ParsedDocument,
        tree: DocTree,
        chunks: list[Chunk],
        *,
        embeddings: dict[str, list[float]] | None = None,
        file_id: str | None = None,
    ) -> None:
        """
        Persist a complete ingestion result.

        embeddings:  optional {chunk_id: vector}. If None and a
                     default embedder was given at construction,
                     embeddings are computed inline after the
                     relational commit. Missing entries are simply
                     skipped in the vector upsert and can be backfilled
                     later via embedder.backfill_embeddings().
        """
        prev_version = self._lookup_active_version(doc.doc_id)

        with self.rel.transaction():
            # Hard overwrite: remove both the new version (defensive)
            # and any previously-active version.
            self.rel.delete_parse_version(doc.doc_id, doc.parse_version)
            if prev_version is not None and prev_version != doc.parse_version:
                self.rel.delete_parse_version(doc.doc_id, prev_version)

            # Pages as compact JSON on the document row
            pages_json = [{"page_no": p.page_no, "width": p.width, "height": p.height} for p in doc.pages]
            self.rel.upsert_document(
                doc_id=doc.doc_id,
                filename=doc.filename,
                format=doc.format.value,
                active_parse_version=doc.parse_version,
                profile_json=profile_to_dict(doc.profile),
                trace_json=trace_to_dict(doc.parse_trace),
                file_id=file_id,
                pages_json=pages_json,
            )
            self.rel.insert_blocks([block_to_row(b) for b in doc.blocks])
            self.rel.save_tree(
                doc_id=tree.doc_id,
                parse_version=tree.parse_version,
                root_id=tree.root_id,
                quality_score=tree.quality_score,
                generation_method=tree.generation_method,
                tree_json=tree_to_dict(tree),
            )
            self.rel.insert_chunks([chunk_to_row(c) for c in chunks])

        log.info(
            "ingestion_writer committed doc=%s version=%d chunks=%d",
            doc.doc_id,
            doc.parse_version,
            len(chunks),
        )

        # ----- vector side (after relational commit) -----
        if embeddings is None and self.embedder is not None and self.vec is not None:
            embeddings = self.embedder.embed_chunks(chunks)
        if self.vec is not None and embeddings:
            self._write_embeddings(doc, chunks, embeddings, prev_version)

    # ------------------------------------------------------------------
    def _write_embeddings(
        self,
        doc: ParsedDocument,
        chunks: list[Chunk],
        embeddings: dict[str, list[float]],
        prev_version: int | None,
    ) -> None:
        assert self.vec is not None

        # Purge old vectors for this doc first.
        # pgvector: rows already deleted by relational delete_parse_version.
        # External stores (chroma/qdrant/milvus/weaviate): delete old
        # version vectors, then upsert new ones.
        if self.vec.backend != "pgvector":
            if prev_version is not None and prev_version != doc.parse_version:
                self.vec.delete_parse_version(doc.doc_id, prev_version)

        # Denormalize the doc's current path into chunk metadata so the
        # vector store can do path-scoped filtering without joining
        # against SQL on every query. FolderService rename keeps these
        # in sync via ``vec.update_paths``.
        doc_row = self.rel.get_document(doc.doc_id) or {}
        doc_path = doc_row.get("path") or "/"

        items: list[VectorItem] = []
        for c in chunks:
            vec = embeddings.get(c.chunk_id)
            if vec is None:
                continue
            items.append(
                VectorItem(
                    chunk_id=c.chunk_id,
                    doc_id=c.doc_id,
                    parse_version=c.parse_version,
                    embedding=vec,
                    metadata={
                        "node_id": c.node_id,
                        "content_type": c.content_type,
                        "page_start": c.page_start,
                        "page_end": c.page_end,
                        "path": doc_path,
                    },
                )
            )
        self.vec.upsert(items)
        log.info("ingestion_writer vectors doc=%s count=%d", doc.doc_id, len(items))

    # ------------------------------------------------------------------
    def _lookup_active_version(self, doc_id: str) -> int | None:
        row = self.rel.get_document(doc_id)
        if not row:
            return None
        return row.get("active_parse_version")
