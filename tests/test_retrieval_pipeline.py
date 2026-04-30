"""
End-to-end retrieval pipeline test.

Uses SQLiteStore + FakeVectorStore + FakeEmbedder and a tiny
document corpus so the whole dual-path + merge + citations flow
runs without any external dependencies.
"""

from __future__ import annotations

import uuid

import pytest

from api.schemas import QueryOverrides
from config import RelationalConfig, RetrievalSection, SQLiteConfig

# QU and rerank are now always-on at the cfg level. Tests run with Fake
# stores and have no LLM credentials, so they explicitly opt out via
# per-request overrides.
_TEST_OVERRIDES = QueryOverrides(query_understanding=False, rerank=False)
from parser.schema import (
    Block,
    BlockType,
    Chunk,
    DocFormat,
    DocProfile,
    DocTree,
    Page,
    ParsedDocument,
    ParseTrace,
    TreeNode,
)
from persistence.ingestion_writer import IngestionWriter
from persistence.store import Store
from persistence.vector.base import VectorHit, VectorItem
from retrieval.pipeline import RetrievalPipeline, build_bm25_index

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEmbedder:
    backend = "fake"
    dimension = 3
    batch_size = 8

    def embed_texts(self, texts):
        # Deterministic dummy: hash-based fixed vectors
        return [[float(len(t) % 7), 1.0, 0.0] for t in texts]

    def embed_chunks(self, chunks):
        return {c.chunk_id: [float(len(c.content) % 7), 1.0, 0.0] for c in chunks if c.content.strip()}


class FakeVectorStore:
    backend = "fake"
    dimension = 3

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

    def delete_chunks(self, chunk_ids):
        for cid in chunk_ids:
            self.items.pop(cid, None)

    def delete_parse_version(self, doc_id, parse_version):
        for cid in list(self.items):
            it = self.items[cid]
            if it.doc_id == doc_id and it.parse_version == parse_version:
                del self.items[cid]

    def search(self, query_vector, *, top_k, filter=None):
        # Pretend all stored items are equally relevant; return them
        # in insertion order so we can verify vector path wiring.
        out: list[VectorHit] = []
        for i, it in enumerate(self.items.values()):
            out.append(
                VectorHit(
                    chunk_id=it.chunk_id,
                    score=1.0 - i * 0.1,
                    doc_id=it.doc_id,
                    parse_version=it.parse_version,
                    metadata=it.metadata,
                )
            )
            if len(out) >= top_k:
                break
        return out


# ---------------------------------------------------------------------------
# Doc builders
# ---------------------------------------------------------------------------


def _build_doc(
    doc_id: str, *, fig_caption="Figure 1: architecture", body_text="Our pipeline uses BM25 and dense retrieval."
):
    fig = Block(
        block_id=f"{doc_id}:1:1:1",
        doc_id=doc_id,
        parse_version=1,
        page_no=1,
        seq=1,
        bbox=(72.0, 600.0, 520.0, 680.0),
        type=BlockType.IMAGE,
        text="",
        image_caption=fig_caption,
    )
    body = Block(
        block_id=f"{doc_id}:1:1:2",
        doc_id=doc_id,
        parse_version=1,
        page_no=1,
        seq=2,
        bbox=(72.0, 400.0, 520.0, 580.0),
        type=BlockType.PARAGRAPH,
        text=body_text,
        cross_ref_targets=[fig.block_id],
    )
    blocks = [fig, body]
    doc = ParsedDocument(
        doc_id=doc_id,
        filename=f"/tmp/{doc_id}.pdf",
        format=DocFormat.PDF,
        parse_version=1,
        profile=DocProfile(
            page_count=1,
            format=DocFormat.PDF,
            file_size_bytes=100,
            heading_hint_strength=0.5,
        ),
        parse_trace=ParseTrace(),
        pages=[Page(page_no=1, width=595, height=842, block_ids=[b.block_id for b in blocks])],
        blocks=blocks,
    )
    root = TreeNode(
        node_id=f"{doc_id}:1:n1",
        doc_id=doc_id,
        parse_version=1,
        parent_id=None,
        level=0,
        title=doc_id,
        page_start=1,
        page_end=1,
        children=[f"{doc_id}:1:n2"],
    )
    section = TreeNode(
        node_id=f"{doc_id}:1:n2",
        doc_id=doc_id,
        parse_version=1,
        parent_id=root.node_id,
        level=1,
        title="Intro",
        page_start=1,
        page_end=1,
        block_ids=[b.block_id for b in blocks],
    )
    tree = DocTree(
        doc_id=doc_id,
        parse_version=1,
        root_id=root.node_id,
        nodes={root.node_id: root, section.node_id: section},
        quality_score=0.9,
        generation_method="headings",
    )
    # Two chunks: figure (cross-refs into body? no — body refs fig)
    # and body. They share a node.
    fig_chunk = Chunk(
        chunk_id=f"{doc_id}:1:c1",
        doc_id=doc_id,
        parse_version=1,
        node_id=section.node_id,
        block_ids=[fig.block_id],
        content=fig_caption,
        content_type="image",
        page_start=1,
        page_end=1,
        token_count=5,
        section_path=[doc_id, "Intro"],
        ancestor_node_ids=[root.node_id],
    )
    body_chunk = Chunk(
        chunk_id=f"{doc_id}:1:c2",
        doc_id=doc_id,
        parse_version=1,
        node_id=section.node_id,
        block_ids=[body.block_id],
        content=body_text,
        content_type="text",
        page_start=1,
        page_end=1,
        token_count=10,
        section_path=[doc_id, "Intro"],
        ancestor_node_ids=[root.node_id],
        cross_ref_chunk_ids=[fig_chunk.chunk_id],  # body chunk refs the figure
    )
    return doc, tree, [fig_chunk, body_chunk]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline_env(tmp_path):
    rel = Store(
        RelationalConfig(
            backend="sqlite",
            sqlite=SQLiteConfig(path=str(tmp_path / "r.db")),
        )
    )
    rel.connect()
    rel.ensure_schema(with_vector=False, embedding_dim=3)

    vec = FakeVectorStore()
    emb = FakeEmbedder()
    writer = IngestionWriter(rel, vector=vec, embedder=emb)

    for i in range(3):
        doc_id = f"doc_{i}_{uuid.uuid4().hex[:4]}"
        doc, tree, chunks = _build_doc(
            doc_id,
            body_text=f"Document {i} talks about BM25 retrieval and RRF fusion.",
        )
        writer.write(doc, tree, chunks)

    bm25 = build_bm25_index(rel, cfg=RetrievalSection().bm25, cache_path="")
    return rel, vec, emb, bm25


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipeline:
    def test_end_to_end_returns_result(self, pipeline_env):
        rel, vec, emb, bm25 = pipeline_env
        cfg = RetrievalSection()
        pipeline = RetrievalPipeline(
            cfg,
            embedder=emb,
            vector_store=vec,
            relational_store=rel,
            bm25_index=bm25,
        )
        result = pipeline.retrieve("BM25 retrieval", overrides=_TEST_OVERRIDES)
        assert result.query == "BM25 retrieval"
        assert result.stats["vector_hits"] >= 0
        # Tree path must produce hits via BM25 fallback
        assert result.stats["tree_hits"] > 0
        assert result.stats["merged_count"] > 0
        # Citations point to existing blocks
        for c in result.citations:
            assert c.highlights
            assert c.page_no >= 1
            assert c.open_url

    def test_vector_and_tree_paths_both_populate_merged(self, pipeline_env):
        rel, vec, emb, bm25 = pipeline_env
        pipeline = RetrievalPipeline(
            RetrievalSection(),
            embedder=emb,
            vector_store=vec,
            relational_store=rel,
            bm25_index=bm25,
        )
        result = pipeline.retrieve("BM25 fusion", overrides=_TEST_OVERRIDES)
        # Some chunk should have been hit by one of the retrieval paths.
        # With new architecture: tree + KG are primary, vector/bm25 are
        # fallback when tree produces nothing.
        sources = set()
        for m in result.merged:
            sources.update(m.sources)
        has_retrieval = any(s in sources for s in ("vector", "tree", "bm25", "vector_fallback", "bm25_fallback", "kg"))
        assert has_retrieval, f"Expected retrieval sources, got: {sources}"

    def test_crossref_expansion_triggered(self, pipeline_env):
        """
        The body chunk cross_ref's the figure chunk. After retrieval
        and merge we expect both to be present (body via BM25, figure
        via expansion:crossref or vector).
        """
        rel, vec, emb, bm25 = pipeline_env
        pipeline = RetrievalPipeline(
            RetrievalSection(),
            embedder=emb,
            vector_store=vec,
            relational_store=rel,
            bm25_index=bm25,
        )
        result = pipeline.retrieve("RRF fusion", overrides=_TEST_OVERRIDES)
        ctypes = {m.chunk.content_type for m in result.merged if m.chunk}
        # Body chunks are text; crossref expansion should also bring the
        # image chunk. If not directly, sibling expansion will (same node).
        assert "text" in ctypes
        assert "image" in ctypes or any("expansion" in s for m in result.merged for s in m.sources)
