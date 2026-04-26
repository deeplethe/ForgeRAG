"""Tests for parser.normalizer -- run on in-memory ParsedDocument."""

from __future__ import annotations

from config import NormalizeConfig
from parser.normalizer import normalize
from parser.schema import (
    Block,
    BlockType,
    DocFormat,
    DocProfile,
    Page,
    ParsedDocument,
    ParseTrace,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _mk_doc(pages_blocks: list[list[Block]], page_h: float = 842.0) -> ParsedDocument:
    pages = []
    flat: list[Block] = []
    for idx, blocks in enumerate(pages_blocks, start=1):
        pages.append(
            Page(
                page_no=idx,
                width=595.0,
                height=page_h,
                block_ids=[b.block_id for b in blocks],
            )
        )
        flat.extend(blocks)
    return ParsedDocument(
        doc_id="doc_test",
        filename="/tmp/fake.pdf",
        format=DocFormat.PDF,
        parse_version=1,
        profile=DocProfile(
            page_count=len(pages_blocks),
            format=DocFormat.PDF,
            file_size_bytes=0,
            heading_hint_strength=0.5,
        ),
        parse_trace=ParseTrace(),
        pages=pages,
        blocks=flat,
    )


def _block(
    page_no: int,
    seq: int,
    text: str,
    btype: BlockType = BlockType.PARAGRAPH,
    bbox=(72.0, 700.0, 520.0, 720.0),
) -> Block:
    return Block(
        block_id=f"doc_test:1:{page_no}:{seq}",
        doc_id="doc_test",
        parse_version=1,
        page_no=page_no,
        seq=seq,
        bbox=bbox,
        type=btype,
        text=text,
    )


_CFG = NormalizeConfig()


# ---------------------------------------------------------------------------
# Header / footer detection
# ---------------------------------------------------------------------------


class TestHeaderFooter:
    def test_recurring_top_block_marked_header(self):
        # A header near top of every page should be excluded
        pages = []
        for p in range(1, 6):
            pages.append(
                [
                    _block(
                        p,
                        1,
                        "Confidential Report",
                        bbox=(72, 820, 400, 835),  # close to top (y ~ 820 of 842)
                    ),
                    _block(
                        p,
                        2,
                        f"Body of page {p}",
                        bbox=(72, 400, 520, 420),
                    ),
                ]
            )
        doc = _mk_doc(pages)
        normalize(doc, _CFG)

        headers = [b for b in doc.blocks if b.type == BlockType.HEADER]
        bodies = [b for b in doc.blocks if b.type == BlockType.PARAGRAPH and not b.excluded]
        assert len(headers) == 5
        assert all(b.excluded for b in headers)
        assert len(bodies) == 5

    def test_page_numbers_collapse_via_normalization(self):
        # "Page 1", "Page 2" ... should be detected as a single footer group
        pages = []
        for p in range(1, 6):
            pages.append(
                [
                    _block(p, 1, "Body content", bbox=(72, 400, 520, 420)),
                    _block(p, 2, f"Page {p}", bbox=(280, 20, 320, 35)),  # footer
                ]
            )
        doc = _mk_doc(pages)
        normalize(doc, _CFG)

        footers = [b for b in doc.blocks if b.type == BlockType.FOOTER]
        assert len(footers) == 5
        assert all(b.excluded and b.excluded_reason == "footer" for b in footers)

    def test_short_doc_skips_detection(self):
        # Fewer than 3 pages -> no header/footer analysis
        pages = [
            [_block(1, 1, "Same text", bbox=(72, 820, 400, 835))],
            [_block(2, 1, "Same text", bbox=(72, 820, 400, 835))],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)
        assert not any(b.excluded for b in doc.blocks)


# ---------------------------------------------------------------------------
# Cross-page paragraph merging
# ---------------------------------------------------------------------------


class TestCrossPageMerge:
    def test_merges_lowercase_continuation(self):
        pages = [
            [_block(1, 1, "The quick brown fox jumps over the")],
            [_block(2, 1, "lazy dog and then keeps running.")],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)

        # Head of page 2 should be excluded with merged_into reason
        b1 = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 1)
        b2 = next(b for b in doc.blocks if b.page_no == 2 and b.seq == 1)
        assert "lazy dog" in b1.text
        assert b2.excluded
        assert b2.excluded_reason.startswith("merged_into:")

    def test_does_not_merge_when_sentence_ended(self):
        pages = [
            [_block(1, 1, "Complete sentence ends here.")],
            [_block(2, 1, "new sentence starts here.")],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)
        assert not any(b.excluded for b in doc.blocks)

    def test_does_not_merge_when_next_is_capitalized(self):
        pages = [
            [_block(1, 1, "Sentence without ending punct")],
            [_block(2, 1, "New Paragraph Starts Here.")],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)
        assert not any(b.excluded for b in doc.blocks)


# ---------------------------------------------------------------------------
# Caption binding
# ---------------------------------------------------------------------------


class TestCaptionBinding:
    def test_binds_figure_caption_after_figure(self):
        [
            [
                _block(1, 1, "", btype=BlockType.FIGURE),
                _block(1, 2, "Figure 1: Architecture diagram"),
            ]
        ] * 4  # pad to 4 pages so header/footer rules are harmless
        # Rebuild distinct instances (avoid shared refs)
        fresh_pages = []
        for p in range(1, 5):
            fresh_pages.append(
                [
                    _block(p, 1, "", btype=BlockType.FIGURE),
                    _block(p, 2, f"Figure {p}: A caption"),
                ]
            )
        doc = _mk_doc(fresh_pages)
        normalize(doc, _CFG)

        captions = [b for b in doc.blocks if b.type == BlockType.CAPTION]
        assert len(captions) == 4
        for cap in captions:
            assert cap.caption_of is not None
            fig = next(b for b in doc.blocks if b.block_id == cap.caption_of)
            assert fig.type == BlockType.FIGURE
            assert fig.figure_caption == cap.text

    def test_chinese_caption_detected(self):
        pages = [
            [
                _block(p, 1, "", btype=BlockType.FIGURE),
                _block(p, 2, f"图 {p}:系统整体架构"),
            ]
            for p in range(1, 5)
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)
        captions = [b for b in doc.blocks if b.type == BlockType.CAPTION]
        assert len(captions) == 4


# ---------------------------------------------------------------------------
# Inline reference resolution
# ---------------------------------------------------------------------------


class TestReferenceResolution:
    def test_english_figure_reference_resolved(self):
        # Fix: the list comp above yields wrong shapes. Rebuild cleanly:
        pages = [
            [
                _block(1, 1, "", btype=BlockType.FIGURE),
                _block(1, 2, "Figure 1: Overall architecture"),
                _block(
                    1,
                    3,
                    "The overall design is shown in Figure 1, which depicts the flow.",
                ),
            ],
            [_block(2, 1, "Body of page 2 with no references.")],
            [_block(3, 1, "Body of page 3.")],
            [_block(4, 1, "Body of page 4.")],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)

        source = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 3)
        figure = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 1)
        assert source.cross_ref_targets == [figure.block_id]

    def test_chinese_table_reference_resolved(self):
        pages = [
            [
                _block(1, 1, "", btype=BlockType.TABLE),
                _block(1, 2, "表 3:主要实验结果"),
                _block(1, 3, "主要实验结果见表 3,可以看出我们的方法显著优于基线。"),
            ],
            [_block(2, 1, "Body of page 2.")],
            [_block(3, 1, "Body of page 3.")],
            [_block(4, 1, "Body of page 4.")],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)

        source = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 3)
        table = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 1)
        assert table.block_id in source.cross_ref_targets

    def test_multiple_refs_deduped(self):
        pages = [
            [
                _block(1, 1, "", btype=BlockType.FIGURE),
                _block(1, 2, "Figure 1: Architecture"),
                _block(
                    1,
                    3,
                    "See Figure 1. As shown in Figure 1, the pipeline is simple.",
                ),
            ],
            [_block(2, 1, "body")],
            [_block(3, 1, "body")],
            [_block(4, 1, "body")],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)
        src = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 3)
        assert len(src.cross_ref_targets) == 1  # deduped

    def test_unknown_label_ignored(self):
        pages = [
            [
                _block(1, 1, "", btype=BlockType.FIGURE),
                _block(1, 2, "Figure 1: Architecture"),
                _block(1, 3, "As discussed in Figure 9, there is more."),
            ],
            [_block(2, 1, "body")],
            [_block(3, 1, "body")],
            [_block(4, 1, "body")],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)
        src = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 3)
        assert src.cross_ref_targets == []

    def test_self_reference_ignored(self):
        # A caption block must not record itself as its own cross-ref.
        pages = [
            [
                _block(1, 1, "", btype=BlockType.FIGURE),
                _block(1, 2, "Figure 1: see Figure 1 for details"),
            ],
            [_block(2, 1, "body")],
            [_block(3, 1, "body")],
            [_block(4, 1, "body")],
        ]
        doc = _mk_doc(pages)
        normalize(doc, _CFG)
        cap = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 2)
        # caption has its own block_id excluded from its cross_ref_targets;
        # and captions are not scanned for inline refs anyway.
        assert cap.cross_ref_targets == []

    def test_disabled_by_config(self):
        from config import NormalizeConfig

        pages = [
            [
                _block(1, 1, "", btype=BlockType.FIGURE),
                _block(1, 2, "Figure 1: Architecture"),
                _block(1, 3, "See Figure 1 for the flow."),
            ],
            [_block(2, 1, "body")],
            [_block(3, 1, "body")],
            [_block(4, 1, "body")],
        ]
        doc = _mk_doc(pages)
        cfg = NormalizeConfig(resolve_references=False)
        normalize(doc, cfg)
        src = next(b for b in doc.blocks if b.page_no == 1 and b.seq == 3)
        assert src.cross_ref_targets == []
