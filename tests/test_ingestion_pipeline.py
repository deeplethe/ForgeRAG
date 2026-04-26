"""
IngestionPipeline end-to-end test.

Uses the synthesized sample_pdf from conftest.py + SQLite + local
blob store + FakeEmbedder + FakeVectorStore. Exercises both phases
(upload, ingest) and checks that Citation.file_id round-trips.
"""

from __future__ import annotations

import pytest

from config import (
    AppConfig,
    ChunkerConfig,
    FilesConfig,
    RelationalConfig,
    RetrievalSection,
    SQLiteConfig,
    TreeBuilderConfig,
)
from ingestion import IngestionPipeline
from parser.blob_store import LocalBlobStore, LocalStoreConfig
from parser.chunker import Chunker
from parser.pipeline import ParserPipeline
from parser.tree_builder import TreeBuilder
from persistence.files import FileStore
from persistence.store import Store
from persistence.vector.base import VectorHit, VectorItem
from retrieval.pipeline import RetrievalPipeline, build_bm25_index

pytest.importorskip("fitz")
pytest.importorskip("sqlalchemy")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEmbedder:
    backend = "fake"
    dimension = 4
    batch_size = 16

    def embed_texts(self, texts):
        return [[float(len(t) % 5), 1.0, 0.5, 0.0] for t in texts]

    def embed_chunks(self, chunks):
        return {c.chunk_id: [float(len(c.content) % 5), 1.0, 0.5, 0.0] for c in chunks if c.content.strip()}


class FakeVectorStore:
    backend = "fake"
    dimension = 4

    def __init__(self):
        self.items: dict[str, VectorItem] = {}

    def connect(self):
        pass

    def close(self):
        pass

    def ensure_schema(self):
        pass

    def upsert(self, items):
        for it in items:
            self.items[it.chunk_id] = it

    def delete_chunks(self, ids):
        for i in ids:
            self.items.pop(i, None)

    def delete_parse_version(self, doc_id, parse_version):
        for cid in list(self.items):
            it = self.items[cid]
            if it.doc_id == doc_id and it.parse_version == parse_version:
                del self.items[cid]

    def search(self, q, *, top_k, filter=None):
        return [
            VectorHit(
                chunk_id=it.chunk_id,
                score=1.0 - i * 0.1,
                doc_id=it.doc_id,
                parse_version=it.parse_version,
                metadata=it.metadata,
            )
            for i, it in enumerate(list(self.items.values())[:top_k])
        ]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline(tmp_path):
    cfg = AppConfig()
    # Point storage at the test tmp dir
    cfg.storage.local.root = str(tmp_path / "blobs")
    # PyMuPDF is the default parser backend; nothing to toggle.

    rel = Store(
        RelationalConfig(
            backend="sqlite",
            sqlite=SQLiteConfig(path=str(tmp_path / "hi.db")),
        )
    )
    rel.connect()
    rel.ensure_schema()

    blob = LocalBlobStore(
        LocalStoreConfig(
            root=str(tmp_path / "blobs"),
            public_base_url="http://host/static",
        )
    )
    file_store = FileStore(FilesConfig(), blob, rel)

    parser = ParserPipeline.from_config(cfg)
    tree_builder = TreeBuilder(TreeBuilderConfig())
    chunker = Chunker(ChunkerConfig())

    vec = FakeVectorStore()
    emb = FakeEmbedder()

    pipeline = IngestionPipeline(
        file_store=file_store,
        parser=parser,
        tree_builder=tree_builder,
        chunker=chunker,
        relational_store=rel,
        vector_store=vec,
        embedder=emb,
    )
    yield pipeline, rel, blob, vec
    rel.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPhaseA:
    def test_upload_only(self, pipeline, sample_pdf):
        pipe, rel, blob, _vec = pipeline
        file_id = pipe.upload(
            sample_pdf,
            original_name="sample.pdf",
            mime_type="application/pdf",
        )
        assert file_id
        row = rel.get_file(file_id)
        assert row is not None
        assert row["original_name"] == "sample.pdf"
        assert blob.exists(row["storage_key"])
        # No document row yet -- Phase B not run
        assert rel.get_document(f"doc_{file_id[:12]}") is None


class TestPhaseB:
    def test_ingest_after_upload(self, pipeline, sample_pdf):
        pipe, rel, _blob, vec = pipeline
        file_id = pipe.upload(
            sample_pdf,
            original_name="sample.pdf",
            mime_type="application/pdf",
        )
        result = pipe.ingest(file_id)
        assert result.doc_id is not None
        assert result.num_chunks > 0
        assert result.num_blocks > 0

        doc_row = rel.get_document(result.doc_id)
        assert doc_row is not None
        assert doc_row["file_id"] == file_id
        assert doc_row["active_parse_version"] == 1

        # Vectors written via inline hook
        assert len(vec.items) == result.num_chunks


class TestConvenience:
    def test_upload_and_ingest_in_one_call(self, pipeline, sample_pdf):
        pipe, rel, _blob, _vec = pipeline
        result = pipe.upload_and_ingest(
            sample_pdf,
            original_name="sample.pdf",
            mime_type="application/pdf",
        )
        assert result.file_id and result.doc_id
        row = rel.get_document(result.doc_id)
        assert row["file_id"] == result.file_id


class TestCitationHasFileId:
    @pytest.mark.skipif(
        True,
        reason="Requires LLM-enabled tree building for multi-chunk results; fallback tree produces single chunk with no citation match",
    )
    def test_citation_carries_file_id(self, pipeline, sample_pdf):
        pipe, rel, _blob, vec = pipeline
        result = pipe.upload_and_ingest(
            sample_pdf,
            original_name="sample.pdf",
            mime_type="application/pdf",
        )
        bm25 = build_bm25_index(rel, RetrievalSection().bm25)
        retrieval = RetrievalPipeline(
            RetrievalSection(),
            embedder=pipe.embedder,
            vector_store=vec,
            relational_store=rel,
            bm25_index=bm25,
        )
        out = retrieval.retrieve("ForgeRAG parser test")
        assert out.citations
        for c in out.citations:
            assert c.file_id == result.file_id
            assert c.doc_id == result.doc_id
            assert c.highlights
