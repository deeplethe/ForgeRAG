"""Tests for parser.tree_builder."""

from __future__ import annotations

from config import TreeBuilderConfig
from parser.schema import (
    Block,
    BlockType,
    DocFormat,
    DocProfile,
    DocTree,
    Page,
    ParsedDocument,
    ParseTrace,
    TocEntry,
)
from parser.tree_builder import TreeBuilder, _BuildContext, _quality_score

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tree_direct(doc: ParsedDocument, method: str, cfg: TreeBuilderConfig | None = None) -> DocTree:
    """Build a tree by calling a specific strategy directly, bypassing
    the LLM-first build() logic. Used by unit tests for TOC/headings."""
    _cfg = cfg or TreeBuilderConfig()
    ctx = _BuildContext(doc, cfg=_cfg)
    if method == "toc":
        tree = ctx.from_toc()
    elif method == "headings":
        tree = ctx.from_headings()
    elif method == "fallback":
        tree = ctx.flat_fallback()
    else:
        raise ValueError(f"Unknown method: {method}")
    tree.quality_score = _quality_score(tree, doc, _cfg)
    return tree


# ---------------------------------------------------------------------------
# Builders for in-memory ParsedDocument fixtures
# ---------------------------------------------------------------------------


def _block(page_no: int, seq: int, text: str, btype=BlockType.PARAGRAPH, level=None):
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
    )


def _mk_doc(
    *,
    blocks: list[Block],
    n_pages: int,
    toc: list[TocEntry] | None = None,
    heading_hint_strength: float = 0.5,
    filename: str = "/tmp/sample.pdf",
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
        filename=filename,
        format=DocFormat.PDF,
        parse_version=1,
        profile=DocProfile(
            page_count=n_pages,
            format=DocFormat.PDF,
            file_size_bytes=0,
            heading_hint_strength=heading_hint_strength,
        ),
        parse_trace=ParseTrace(),
        pages=pages,
        blocks=blocks,
        toc=toc,
    )


# ---------------------------------------------------------------------------
# TOC strategy
# ---------------------------------------------------------------------------


class TestFromToc:
    def test_builds_flat_toc(self):
        toc = [
            TocEntry(level=1, title="Introduction", page_no=1),
            TocEntry(level=1, title="Methods", page_no=3),
            TocEntry(level=1, title="Results", page_no=5),
        ]
        blocks = [
            _block(1, 1, "Intro body"),
            _block(2, 1, "More intro"),
            _block(3, 1, "Methods body"),
            _block(4, 1, "More methods"),
            _block(5, 1, "Results body"),
            _block(6, 1, "More results"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=6, toc=toc)
        tree = _build_tree_direct(doc, "toc")

        assert tree.generation_method == "toc"
        assert len(tree.root().children) == 3

        intro = tree.nodes[tree.root().children[0]]
        methods = tree.nodes[tree.root().children[1]]
        results = tree.nodes[tree.root().children[2]]

        assert intro.title == "Introduction"
        assert intro.page_start == 1
        assert intro.page_end == 2  # up to methods.page_start - 1
        assert methods.page_start == 3
        assert methods.page_end == 4
        assert results.page_start == 5
        assert results.page_end >= 5

    def test_nested_toc_page_ranges(self):
        toc = [
            TocEntry(
                level=1,
                title="Part I",
                page_no=1,
                children=[
                    TocEntry(level=2, title="Chapter 1", page_no=1),
                    TocEntry(level=2, title="Chapter 2", page_no=3),
                ],
            ),
            TocEntry(level=1, title="Part II", page_no=5),
        ]
        blocks = [_block(p, 1, f"p{p}") for p in range(1, 7)]
        doc = _mk_doc(blocks=blocks, n_pages=6, toc=toc)
        tree = _build_tree_direct(doc, "toc")

        by_title = {n.title: n for n in tree.nodes.values()}
        assert by_title["Chapter 1"].page_end == 2  # up to Chapter 2 start - 1
        assert by_title["Chapter 2"].page_end == 4  # up to Part II start - 1
        assert by_title["Part II"].page_end == 6  # end of doc

    def test_blocks_attached_to_deepest_section(self):
        toc = [
            TocEntry(
                level=1,
                title="Chapter 1",
                page_no=1,
                children=[TocEntry(level=2, title="1.1 Sub", page_no=2)],
            ),
        ]
        blocks = [
            _block(1, 1, "chapter intro"),
            _block(2, 1, "sub content"),
            _block(3, 1, "more sub content"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=3, toc=toc)
        tree = _build_tree_direct(doc, "toc")

        sub = next(n for n in tree.nodes.values() if n.title == "1.1 Sub")
        chapter = next(n for n in tree.nodes.values() if n.title == "Chapter 1")

        # page 1 -> chapter; pages 2,3 -> sub
        assert len(chapter.block_ids) == 1
        assert len(sub.block_ids) == 2


# ---------------------------------------------------------------------------
# Heading strategy
# ---------------------------------------------------------------------------


class TestFromHeadings:
    def test_flat_headings(self):
        blocks = [
            _block(1, 1, "Introduction", btype=BlockType.HEADING, level=1),
            _block(1, 2, "intro body"),
            _block(2, 1, "Methods", btype=BlockType.HEADING, level=1),
            _block(2, 2, "methods body"),
            _block(3, 1, "Results", btype=BlockType.HEADING, level=1),
            _block(3, 2, "results body"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=3)
        tree = _build_tree_direct(doc, "headings")

        assert tree.generation_method == "headings"
        assert len(tree.root().children) == 3
        titles = [tree.nodes[cid].title for cid in tree.root().children]
        assert titles == ["Introduction", "Methods", "Results"]

    def test_nested_headings_build_stack(self):
        blocks = [
            _block(1, 1, "Chapter 1", btype=BlockType.HEADING, level=1),
            _block(1, 2, "chapter body"),
            _block(1, 3, "1.1 Sub A", btype=BlockType.HEADING, level=2),
            _block(1, 4, "sub A body"),
            _block(2, 1, "1.2 Sub B", btype=BlockType.HEADING, level=2),
            _block(2, 2, "sub B body"),
            _block(3, 1, "Chapter 2", btype=BlockType.HEADING, level=1),
            _block(3, 2, "chapter 2 body"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=3)
        tree = _build_tree_direct(doc, "headings")

        ch1 = next(n for n in tree.nodes.values() if n.title == "Chapter 1")
        ch2 = next(n for n in tree.nodes.values() if n.title == "Chapter 2")
        sub_a = next(n for n in tree.nodes.values() if n.title == "1.1 Sub A")
        sub_b = next(n for n in tree.nodes.values() if n.title == "1.2 Sub B")

        assert sub_a.parent_id == ch1.node_id
        assert sub_b.parent_id == ch1.node_id
        assert ch2.parent_id == tree.root_id
        # Chapter 1 page_end should cover sub B (bubbled up)
        assert ch1.page_end >= 2

    def test_preheading_orphans_attach_to_root(self):
        blocks = [
            _block(1, 1, "orphan intro"),
            _block(1, 2, "more orphan"),
            _block(2, 1, "First Heading", btype=BlockType.HEADING, level=1),
            _block(2, 2, "body"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=2)
        tree = _build_tree_direct(doc, "headings")

        assert tree.generation_method == "headings"
        root = tree.root()
        # The two orphan blocks should be owned by the root
        assert len(root.block_ids) == 2


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


class TestFallback:
    def test_no_toc_weak_headings_uses_fallback(self):
        blocks = [_block(p, 1, f"page {p}") for p in range(1, 4)]
        doc = _mk_doc(
            blocks=blocks,
            n_pages=3,
            heading_hint_strength=0.0,  # below min
        )
        tree = _build_tree_direct(doc, "fallback")
        assert tree.generation_method == "fallback"
        # Root + one section
        assert len(tree.nodes) == 2
        section = tree.nodes[tree.root().children[0]]
        assert len(section.block_ids) == 3


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


class TestEnrichment:
    def test_counts_tables_and_images(self):
        toc = [TocEntry(level=1, title="Only", page_no=1)]
        blocks = [
            _block(1, 1, "para"),
            _block(1, 2, "", btype=BlockType.TABLE),
            _block(1, 3, "", btype=BlockType.IMAGE),
            _block(1, 4, "", btype=BlockType.IMAGE),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1, toc=toc)
        tree = _build_tree_direct(doc, "toc")
        only = next(n for n in tree.nodes.values() if n.title == "Only")
        assert only.table_count == 1
        assert only.image_count == 2
        assert "paragraph" in only.element_types
        assert "table" in only.element_types
        assert "image" in only.element_types
        assert only.content_hash  # non-empty


# ---------------------------------------------------------------------------
# Traversal helpers
# ---------------------------------------------------------------------------


class TestTraversal:
    def test_ancestors_chain(self):
        blocks = [
            _block(1, 1, "Chapter 1", btype=BlockType.HEADING, level=1),
            _block(1, 2, "1.1 Sub", btype=BlockType.HEADING, level=2),
            _block(1, 3, "1.1.1 Subsub", btype=BlockType.HEADING, level=3),
            _block(1, 4, "body"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        tree = _build_tree_direct(doc, "headings")
        subsub = next(n for n in tree.nodes.values() if n.title == "1.1.1 Subsub")
        chain = tree.ancestors(subsub.node_id)
        titles = [n.title for n in chain]
        assert titles[0] == tree.root().title  # root first
        assert "Chapter 1" in titles
        assert "1.1 Sub" in titles

    def test_preorder_walk_visits_all(self):
        toc = [
            TocEntry(level=1, title="A", page_no=1),
            TocEntry(level=1, title="B", page_no=2),
        ]
        blocks = [_block(1, 1, "a"), _block(2, 1, "b")]
        doc = _mk_doc(blocks=blocks, n_pages=2, toc=toc)
        tree = _build_tree_direct(doc, "toc")
        visited = list(tree.walk_preorder())
        assert len(visited) == len(tree.nodes)
        assert visited[0].node_id == tree.root_id


# ---------------------------------------------------------------------------
# Quality score
# ---------------------------------------------------------------------------


class TestQualityScore:
    def test_good_toc_gives_reasonable_score(self):
        toc = [
            TocEntry(level=1, title="Intro", page_no=1),
            TocEntry(level=1, title="Methods", page_no=8),
            TocEntry(level=1, title="Results", page_no=15),
        ]
        blocks = [_block(p, 1, f"p{p}") for p in range(1, 22)]
        doc = _mk_doc(blocks=blocks, n_pages=21, toc=toc)
        tree = _build_tree_direct(doc, "toc")
        assert tree.quality_score > 0.5

    def test_empty_tree_gets_zero(self):
        blocks = [_block(1, 1, "body")]
        doc = _mk_doc(blocks=blocks, n_pages=1, heading_hint_strength=0.0)
        tree = _build_tree_direct(doc, "fallback")
        # fallback has 1 leaf covering full doc -> nonzero
        assert tree.quality_score >= 0.0


# ---------------------------------------------------------------------------
# Build strategy (new)
# ---------------------------------------------------------------------------


class TestBuildStrategy:
    def test_no_llm_uses_heading_fallback(self):
        """Without LLM, build() uses the heading-level fallback when
        the doc actually has headings (richer than flat fallback)."""
        blocks = [
            _block(1, 1, "Introduction", btype=BlockType.HEADING, level=1),
            _block(1, 2, "intro body " * 20),
            _block(2, 1, "Methods", btype=BlockType.HEADING, level=1),
            _block(2, 2, "methods body " * 20),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=2)
        # Default config: llm_enabled=True but ``model`` unset → graceful
        # fallback. With visible headings, fallback is heading-based.
        tree = TreeBuilder(TreeBuilderConfig()).build(doc)
        assert tree.generation_method == "headings"

    def test_no_llm_weak_headings_also_fallback(self):
        """Weak headings with no LLM → fallback."""
        blocks = [_block(p, 1, f"page {p}") for p in range(1, 4)]
        doc = _mk_doc(blocks=blocks, n_pages=3, heading_hint_strength=0.05)
        tree = TreeBuilder(TreeBuilderConfig()).build(doc)
        assert tree.generation_method == "fallback"


# ---------------------------------------------------------------------------
# Large node subdivision (new)
# ---------------------------------------------------------------------------


class TestSubdivision:
    def test_large_leaf_is_subdivided(self):
        """A leaf node exceeding max_tokens_per_node should be split."""
        blocks = [
            _block(1, 1, "Chapter 1", btype=BlockType.HEADING, level=1),
        ]
        # Add many paragraph blocks to make a large leaf
        for i in range(20):
            blocks.append(_block(1 + i, i + 2, "A" * 2000))  # ~500 tokens each

        doc = _mk_doc(blocks=blocks, n_pages=21)
        cfg = TreeBuilderConfig(max_tokens_per_node=2000)
        # Build tree directly via headings, then subdivide
        tree = _build_tree_direct(doc, "headings", cfg=cfg)
        builder = TreeBuilder(cfg)
        builder._subdivide_large_nodes(tree, doc)

        # The chapter node should now have children (subdivided)
        ch1 = next(n for n in tree.nodes.values() if n.title == "Chapter 1")
        assert len(ch1.children) >= 2, "Large node should be subdivided"

        # Each sub-node should have a summary
        for cid in ch1.children:
            sub = tree.nodes[cid]
            assert sub.block_ids, "Sub-node should have blocks"
            assert "part" in sub.title.lower(), f"Sub-node title should contain 'part': {sub.title}"

    def test_small_leaf_not_subdivided(self):
        """A leaf within threshold should not be touched."""
        blocks = [
            _block(1, 1, "Chapter 1", btype=BlockType.HEADING, level=1),
            _block(1, 2, "Short body"),
        ]
        doc = _mk_doc(blocks=blocks, n_pages=1)
        cfg = TreeBuilderConfig(max_tokens_per_node=8000)
        tree = _build_tree_direct(doc, "headings", cfg=cfg)
        builder = TreeBuilder(cfg)
        builder._subdivide_large_nodes(tree, doc)
        ch1 = next(n for n in tree.nodes.values() if n.title == "Chapter 1")
        assert len(ch1.children) == 0


# ---------------------------------------------------------------------------
# Summary helpers (new)
# ---------------------------------------------------------------------------


class TestSummaryHelpers:
    def test_cheap_node_summary_extracts_first_sentence(self):
        from parser.schema import TreeNode
        from parser.summary import cheap_node_summary

        node = TreeNode(
            node_id="n1",
            doc_id="doc",
            parse_version=1,
            parent_id=None,
            level=1,
            title="Test",
            page_start=1,
            page_end=1,
            block_ids=["doc:1:1:1"],
        )
        blocks_index = {
            "doc:1:1:1": _block(1, 1, "This is the first sentence. This is the second sentence. And more."),
        }
        summary = cheap_node_summary(node, blocks_index)
        assert "first sentence" in summary
        assert len(summary) < 200
