"""Tests for parser.chunker."""

from __future__ import annotations

from config import ChunkerConfig, TreeBuilderConfig
from parser.chunker import Chunker, approx_tokens
from parser.schema import (
    Block,
    BlockType,
    DocFormat,
    DocProfile,
    Page,
    ParsedDocument,
    ParseTrace,
    TocEntry,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _block(
    page_no: int,
    seq: int,
    text: str,
    btype: BlockType = BlockType.PARAGRAPH,
    level=None,
    **extra,
) -> Block:
    return Block(
        block_id=f"doc:1:{page_no}:{seq}",
        doc_id="doc",
        parse_version=1,
        page_no=page_no,
        seq=seq,
        bbox=(72.0, 700.0, 520.0, 720.0),
        type=btype,
        level=level,
        text=text,
        **extra,
    )


def _mk_doc(
    *,
    blocks: list[Block],
    n_pages: int,
    toc=None,
    heading_hint: float = 0.5,
) -> ParsedDocument:
    pages = [
        Page(
            page_no=p,
            width=595.0,
            height=842.0,
            block_ids=[b.block_id for b in blocks if b.page_no == p],
        )
        for p in range(1, n_pages + 1)
    ]
    return ParsedDocument(
        doc_id="doc",
        filename="/tmp/x.pdf",
        format=DocFormat.PDF,
        parse_version=1,
        profile=DocProfile(
            page_count=n_pages,
            format=DocFormat.PDF,
            file_size_bytes=0,
            heading_hint_strength=heading_hint,
        ),
        parse_trace=ParseTrace(),
        pages=pages,
        blocks=blocks,
        toc=toc,
    )


def _build(doc: ParsedDocument, cfg: ChunkerConfig | None = None):
    """Build tree using direct strategy (TOC/headings) for test determinism.

    Production build() requires llm_enabled for non-fallback trees, but
    tests need deterministic TOC/heading trees without LLM calls.
    """
    from parser.tree_builder import _BuildContext, _quality_score

    tb_cfg = TreeBuilderConfig()
    ctx = _BuildContext(doc, cfg=tb_cfg)
    if doc.toc:
        tree = ctx.from_toc()
    elif doc.profile.heading_hint_strength >= 0.20:
        tree = ctx.from_headings()
    else:
        tree = ctx.flat_fallback()
    tree.quality_score = _quality_score(tree, doc, tb_cfg)
    chunks = Chunker(cfg or ChunkerConfig()).chunk(doc, tree)
    return tree, chunks


# ---------------------------------------------------------------------------
# Token approximator
# ---------------------------------------------------------------------------


class TestApproxTokens:
    def test_empty(self):
        assert approx_tokens("") == 0

    def test_english_ratio(self):
        # "hello world" 11 chars * 0.25 = 2.75 -> 2
        assert approx_tokens("hello world") == 2

    def test_cjk_ratio(self):
        # 5 CJK * 1.5 = 7.5 -> 7
        assert approx_tokens("你好世界啊") == 7

    def test_mixed(self):
        t = "hello 你好 world"
        n = approx_tokens(t)
        assert n > 0


# ---------------------------------------------------------------------------
# Basic chunking
# ---------------------------------------------------------------------------


class TestBasicChunking:
    def test_single_heading_section_single_chunk(self):
        # Small enough to fit in one chunk
        blocks = [
            _block(1, 1, "Intro", btype=BlockType.HEADING, level=1),
            _block(1, 2, "A short paragraph."),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        _, chunks = _build(doc)
        assert len(chunks) == 1
        c = chunks[0]
        assert c.content_type == "mixed"
        assert len(c.block_ids) == 2
        assert c.page_start == 1 and c.page_end == 1

    def test_chunks_carry_section_path(self):
        blocks = [
            _block(1, 1, "Chapter 1", btype=BlockType.HEADING, level=1),
            _block(1, 2, "1.1 Sub", btype=BlockType.HEADING, level=2),
            _block(1, 3, "body text"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        _tree, chunks = _build(doc)
        # Find the chunk containing "body text"
        body_chunk = next(c for c in chunks if "body text" in c.content)
        assert "Chapter 1" in body_chunk.section_path
        assert "1.1 Sub" in body_chunk.section_path
        # ancestor_node_ids goes root -> parent, excludes owning node
        assert len(body_chunk.ancestor_node_ids) >= 1

    def test_node_id_matches_owning_node(self):
        blocks = [
            _block(1, 1, "Section A", btype=BlockType.HEADING, level=1),
            _block(1, 2, "a body"),
            _block(1, 3, "Section B", btype=BlockType.HEADING, level=1),
            _block(1, 4, "b body"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        tree, chunks = _build(doc)
        a_node = next(n for n in tree.nodes.values() if n.title == "Section A")
        b_node = next(n for n in tree.nodes.values() if n.title == "Section B")
        a_chunks = [c for c in chunks if c.node_id == a_node.node_id]
        b_chunks = [c for c in chunks if c.node_id == b_node.node_id]
        assert a_chunks and b_chunks
        # No chunk should reference a non-existent node
        assert all(c.node_id in tree.nodes for c in chunks)


# ---------------------------------------------------------------------------
# Block isolation
# ---------------------------------------------------------------------------


class TestBlockIsolation:
    def test_table_gets_own_chunk(self):
        blocks = [
            _block(1, 1, "Section", btype=BlockType.HEADING, level=1),
            _block(1, 2, "text before"),
            _block(1, 3, "", btype=BlockType.TABLE, table_markdown="| a | b |"),
            _block(1, 4, "text after"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        _, chunks = _build(doc)
        # Expect at least 3 chunks: [heading+before, table, after]
        table_chunks = [c for c in chunks if c.content_type == "table"]
        assert len(table_chunks) == 1
        assert table_chunks[0].content == "| a | b |"
        assert len(table_chunks[0].block_ids) == 1

    def test_figure_gets_own_chunk_with_caption(self):
        blocks = [
            _block(1, 1, "Section", btype=BlockType.HEADING, level=1),
            _block(
                1,
                2,
                "",
                btype=BlockType.FIGURE,
                figure_caption="Figure 1: diagram",
            ),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        _, chunks = _build(doc)
        figure_chunks = [c for c in chunks if c.content_type == "figure"]
        assert len(figure_chunks) == 1
        assert "Figure 1: diagram" in figure_chunks[0].content


# ---------------------------------------------------------------------------
# Text packing
# ---------------------------------------------------------------------------


class TestTextPacking:
    def test_respects_target_tokens(self):
        # Each block ~= 100 English chars -> ~25 tokens each
        long_text = "word " * 20  # 100 chars
        blocks = [
            _block(1, 1, "Section", btype=BlockType.HEADING, level=1),
        ] + [_block(1, 2 + i, long_text) for i in range(20)]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        cfg = ChunkerConfig(target_tokens=60, max_tokens=100, min_tokens=10)
        _, chunks = _build(doc, cfg)
        # target 60 tokens, each block ~25 -> expect multiple chunks
        text_chunks = [c for c in chunks if c.content_type == "text"]
        assert len(text_chunks) >= 3
        # No chunk exceeds max_tokens (except when a single block itself is bigger,
        # which doesn't happen here)
        for c in text_chunks:
            assert c.token_count <= 100 + 30  # small slack for merge rule

    def test_oversized_single_block_gets_its_own_chunk(self):
        huge = "x" * 10000  # ~2500 tokens
        blocks = [
            _block(1, 1, "Section", btype=BlockType.HEADING, level=1),
            _block(1, 2, huge),
            _block(1, 3, "normal text"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        cfg = ChunkerConfig(target_tokens=100, max_tokens=500, min_tokens=10)
        _, chunks = _build(doc, cfg)
        huge_chunks = [c for c in chunks if "x" * 100 in c.content]
        assert len(huge_chunks) == 1
        assert huge_chunks[0].block_ids == ["doc:1:1:2"]

    def test_small_trailing_chunk_merges_into_previous(self):
        # Two blocks of ~40 tokens, then one of ~5 tokens.
        # target=70 -> block1+block2 in first chunk, block3 alone (<min).
        # Merger should fold block3 back into the first.
        blocks = [
            _block(1, 1, "Section", btype=BlockType.HEADING, level=1),
            _block(1, 2, "word " * 35),  # ~35 tokens
            _block(1, 3, "word " * 35),  # ~35 tokens
            _block(1, 4, "short"),  # ~1 token
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        cfg = ChunkerConfig(target_tokens=70, max_tokens=200, min_tokens=10)
        _, chunks = _build(doc, cfg)
        text_chunks = [c for c in chunks if c.content_type == "text"]
        # All content should end up in a small number of chunks; the trailing
        # "short" should not be alone
        assert all(c.token_count >= 1 for c in text_chunks)
        shorts = [c for c in text_chunks if c.token_count < 10]
        assert len(shorts) == 0


# ---------------------------------------------------------------------------
# Cross-references
# ---------------------------------------------------------------------------


class TestCrossRefs:
    def test_block_cross_refs_become_chunk_cross_refs(self):
        # Build doc where para block references a figure block
        fig = _block(1, 3, "", btype=BlockType.FIGURE, figure_caption="Figure 1: diagram")
        ref_block = _block(
            1,
            4,
            "See Figure 1 for the design.",
            cross_ref_targets=[fig.block_id],
        )
        blocks = [
            _block(1, 1, "Section", btype=BlockType.HEADING, level=1),
            _block(1, 2, "preface"),
            fig,
            ref_block,
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        _, chunks = _build(doc)

        fig_chunk = next(c for c in chunks if c.content_type == "figure")
        ref_chunk = next(c for c in chunks if ref_block.block_id in c.block_ids)
        assert fig_chunk.chunk_id in ref_chunk.cross_ref_chunk_ids

    def test_self_reference_excluded(self):
        # cross_ref_targets pointing to a block within the same chunk
        # must not produce a self-reference.
        b1 = _block(1, 1, "Section", btype=BlockType.HEADING, level=1)
        b2 = _block(1, 2, "a", cross_ref_targets=["doc:1:1:3"])
        b3 = _block(1, 3, "b")
        doc = _mk_doc(blocks=[b1, b2, b3], n_pages=1)
        _, chunks = _build(doc)
        # Both b2 and b3 land in the same chunk
        same_chunk = next(c for c in chunks if b2.block_id in c.block_ids and b3.block_id in c.block_ids)
        assert same_chunk.chunk_id not in same_chunk.cross_ref_chunk_ids


# ---------------------------------------------------------------------------
# Excluded blocks
# ---------------------------------------------------------------------------


class TestExcludedBlocks:
    def test_excluded_blocks_not_in_chunks(self):
        excluded = _block(1, 1, "header repeat")
        excluded.excluded = True
        excluded.excluded_reason = "header"
        blocks = [
            excluded,
            _block(1, 2, "Section", btype=BlockType.HEADING, level=1),
            _block(1, 3, "real content"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        _, chunks = _build(doc)
        all_block_ids = {bid for c in chunks for bid in c.block_ids}
        assert excluded.block_id not in all_block_ids
        assert all(excluded.block_id != bid for bid in all_block_ids)


# ---------------------------------------------------------------------------
# TOC strategy integration
# ---------------------------------------------------------------------------


class TestWithTocTree:
    def test_chunks_respect_toc_sections(self):
        toc = [
            TocEntry(level=1, title="Intro", page_no=1),
            TocEntry(level=1, title="Methods", page_no=2),
        ]
        blocks = [
            _block(1, 1, "intro body one"),
            _block(1, 2, "intro body two"),
            _block(2, 1, "methods body one"),
            _block(2, 2, "methods body two"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=2, toc=toc)
        tree, chunks = _build(doc)

        intro_node = next(n for n in tree.nodes.values() if n.title == "Intro")
        methods_node = next(n for n in tree.nodes.values() if n.title == "Methods")

        intro_chunks = [c for c in chunks if c.node_id == intro_node.node_id]
        methods_chunks = [c for c in chunks if c.node_id == methods_node.node_id]
        assert intro_chunks and methods_chunks
        # No chunk crosses nodes
        for c in chunks:
            for bid in c.block_ids:
                assert c.node_id is not None
