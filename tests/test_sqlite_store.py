"""Store end-to-end tests against SQLite (no external DB required)."""

from __future__ import annotations

import uuid

import pytest

from config import RelationalConfig, SQLiteConfig
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


@pytest.fixture
def sqlite_store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "hi.db")),
    )
    store = Store(cfg)
    store.connect()
    store.ensure_schema(with_vector=False, embedding_dim=1536)
    yield store
    store.close()


def _sample_doc(doc_id: str):
    blocks = [
        Block(
            block_id=f"{doc_id}:1:1:1",
            doc_id=doc_id,
            parse_version=1,
            page_no=1,
            seq=1,
            bbox=(72.0, 700.0, 520.0, 720.0),
            type=BlockType.HEADING,
            level=1,
            text="Intro",
        ),
        Block(
            block_id=f"{doc_id}:1:1:2",
            doc_id=doc_id,
            parse_version=1,
            page_no=1,
            seq=2,
            bbox=(72.0, 600.0, 520.0, 680.0),
            type=BlockType.PARAGRAPH,
            text="Body",
            cross_ref_targets=[f"{doc_id}:1:1:1"],
        ),
    ]
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
        element_types=["heading", "paragraph"],
        content_hash="h",
    )
    tree = DocTree(
        doc_id=doc_id,
        parse_version=1,
        root_id=root.node_id,
        nodes={root.node_id: root, section.node_id: section},
        quality_score=0.9,
        generation_method="headings",
    )
    chunks = [
        Chunk(
            chunk_id=f"{doc_id}:1:c1",
            doc_id=doc_id,
            parse_version=1,
            node_id=section.node_id,
            block_ids=[b.block_id for b in blocks],
            content="Intro\n\nBody",
            content_type="text",
            page_start=1,
            page_end=1,
            token_count=3,
            section_path=[doc_id, "Intro"],
            ancestor_node_ids=[root.node_id],
            cross_ref_chunk_ids=[],
        )
    ]
    return doc, tree, chunks


class TestSchemaAndRoundtrip:
    def test_ensure_schema_idempotent(self, sqlite_store):
        sqlite_store.ensure_schema(with_vector=False, embedding_dim=1536)
        sqlite_store.ensure_schema(with_vector=False, embedding_dim=1536)

    def test_full_write_and_readback(self, sqlite_store):
        doc_id = f"t_{uuid.uuid4().hex[:6]}"
        doc, tree, chunks = _sample_doc(doc_id)
        writer = IngestionWriter(sqlite_store, vector=None)
        writer.write(doc, tree, chunks)

        row = sqlite_store.get_document(doc_id)
        assert row is not None
        assert row["active_parse_version"] == 1
        assert row["doc_profile_json"]["format"] == "pdf"

        blocks = sqlite_store.get_blocks(doc_id, 1)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "heading"
        assert blocks[1]["cross_ref_targets"] == [f"{doc_id}:1:1:1"]
        assert blocks[0]["excluded"] is False

        t = sqlite_store.load_tree(doc_id, 1)
        assert t["generation_method"] == "headings"
        assert len(t["nodes"]) == 2

        loaded_chunks = sqlite_store.get_chunks(doc_id, 1)
        assert len(loaded_chunks) == 1
        assert loaded_chunks[0]["section_path"] == [doc_id, "Intro"]


class TestVersioning:
    def test_hard_overwrite_removes_old_version(self, sqlite_store):
        doc_id = f"t_{uuid.uuid4().hex[:6]}"
        doc, tree, chunks = _sample_doc(doc_id)
        writer = IngestionWriter(sqlite_store, vector=None)
        writer.write(doc, tree, chunks)

        # Re-ingest as version 2
        doc.parse_version = 2
        for b in doc.blocks:
            b.parse_version = 2
            b.block_id = b.block_id.replace(":1:", ":2:")
        tree.parse_version = 2
        new_nodes = {}
        for nid, n in tree.nodes.items():
            n.parse_version = 2
            n.node_id = n.node_id.replace(":1:", ":2:")
            if n.parent_id:
                n.parent_id = n.parent_id.replace(":1:", ":2:")
            n.children = [c.replace(":1:", ":2:") for c in n.children]
            n.block_ids = [b.replace(":1:", ":2:") for b in n.block_ids]
            new_nodes[n.node_id] = n
        tree.nodes = new_nodes
        tree.root_id = tree.root_id.replace(":1:", ":2:")
        for c in chunks:
            c.parse_version = 2
            c.chunk_id = c.chunk_id.replace(":1:", ":2:")
            c.node_id = c.node_id.replace(":1:", ":2:")
            c.block_ids = [b.replace(":1:", ":2:") for b in c.block_ids]
            c.ancestor_node_ids = [a.replace(":1:", ":2:") for a in c.ancestor_node_ids]

        writer.write(doc, tree, chunks)

        assert sqlite_store.get_blocks(doc_id, 1) == []
        assert len(sqlite_store.get_blocks(doc_id, 2)) == 2
        assert sqlite_store.get_document(doc_id)["active_parse_version"] == 2


class TestTransactionRollback:
    def test_rollback_on_error_preserves_state(self, sqlite_store):
        doc_id = f"t_{uuid.uuid4().hex[:6]}"
        doc, tree, chunks = _sample_doc(doc_id)
        writer = IngestionWriter(sqlite_store, vector=None)
        writer.write(doc, tree, chunks)

        # Corrupt chunks list so the second write should fail mid-transaction
        broken = list(chunks)
        broken.append(
            Chunk(
                chunk_id=broken[0].chunk_id,  # duplicate PK
                doc_id=doc_id,
                parse_version=1,
                node_id="bad",
                block_ids=[],
                content="dup",
                content_type="text",
                page_start=1,
                page_end=1,
                token_count=1,
            )
        )
        with pytest.raises(Exception):
            writer.write(doc, tree, broken)

        # After the failed rewrite, row state must still exist from the first
        # successful write (because the second call's delete+insert rolled back).
        row = sqlite_store.get_document(doc_id)
        assert row is not None
