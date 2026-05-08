"""
End-to-end test for the PyMuPDF backend + pipeline.

Uses the sample_pdf fixture from conftest.py, runs the full
ParserPipeline, and asserts on the final normalized ParsedDocument.
"""

from __future__ import annotations

import pytest

from config import AppConfig
from parser.backends.pymupdf import PyMuPDFBackend
from parser.blob_store import LocalBlobStore, LocalStoreConfig
from parser.pipeline import ParserPipeline, _quick_profile
from parser.schema import BlockType, DocFormat

pytest.importorskip("fitz")


@pytest.fixture
def blob_store(tmp_path) -> LocalBlobStore:
    return LocalBlobStore(LocalStoreConfig(root=str(tmp_path / "figs")))


@pytest.fixture
def pipeline(tmp_path) -> ParserPipeline:
    cfg = AppConfig()
    cfg.storage.local.root = str(tmp_path / "figs")
    return ParserPipeline.from_config(cfg)


# ---------------------------------------------------------------------------
# Direct backend tests
# ---------------------------------------------------------------------------


class TestPyMuPDFBackendDirect:
    def test_parse_produces_blocks(self, sample_pdf, blob_store):
        cfg = AppConfig().parser.backends.pymupdf
        be = PyMuPDFBackend(cfg, blob_store)
        profile = _quick_profile(str(sample_pdf))
        result = be.parse(str(sample_pdf), "doc_test", 1, profile)

        assert result.format == DocFormat.PDF
        assert len(result.pages) == 4
        assert len(result.blocks) > 0
        # All blocks have stable ids of the documented shape
        for b in result.blocks:
            parts = b.block_id.split(":")
            assert parts[0] == "doc_test"
            assert parts[1] == "1"
            assert 1 <= int(parts[2]) <= 4

    def test_toc_extracted(self, sample_pdf, blob_store):
        cfg = AppConfig().parser.backends.pymupdf
        be = PyMuPDFBackend(cfg, blob_store)
        profile = _quick_profile(str(sample_pdf))
        result = be.parse(str(sample_pdf), "doc_test", 1, profile)
        assert result.toc is not None
        titles = [e.title for e in result.toc]
        assert titles == ["Introduction", "Methods", "Results"]

    def test_headings_detected(self, sample_pdf, blob_store):
        cfg = AppConfig().parser.backends.pymupdf
        be = PyMuPDFBackend(cfg, blob_store)
        profile = _quick_profile(str(sample_pdf))
        result = be.parse(str(sample_pdf), "doc_test", 1, profile)
        headings = [b for b in result.blocks if b.type == BlockType.HEADING]
        heading_texts = [b.text for b in headings]
        assert any("Introduction" in t for t in heading_texts)

    def test_bbox_is_bottom_left_origin(self, sample_pdf, blob_store):
        """
        After the y-flip in PyMuPDFBackend, bboxes must use
        bottom-left origin. The "Introduction" title was drawn at
        y=120 from the top (insert_text uses top-left); after the
        flip its y center should be near 842-120=722.
        """
        cfg = AppConfig().parser.backends.pymupdf
        be = PyMuPDFBackend(cfg, blob_store)
        profile = _quick_profile(str(sample_pdf))
        result = be.parse(str(sample_pdf), "doc_test", 1, profile)

        for b in result.blocks:
            x0, y0, x1, y1 = b.bbox
            assert x0 < x1
            assert y0 < y1
            assert 0 <= x0 <= 595
            assert 0 <= y0 <= 842
            assert 0 <= y1 <= 842

        # Title block on page 1 was drawn at top of page ->
        # in bottom-left coords its y center must be in the TOP half.
        intro = next(b for b in result.blocks if b.page_no == 1 and "Introduction" in b.text)
        y_center = (intro.bbox[1] + intro.bbox[3]) / 2
        assert y_center > 842 / 2, f"title should be in top half, got y={y_center}"


# ---------------------------------------------------------------------------
# Full pipeline tests
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def test_parse_roundtrip(self, pipeline, sample_pdf):
        result = pipeline.parse(sample_pdf, doc_id="doc_test", parse_version=1)
        assert result.parse_trace.backend == "pymupdf"
        assert result.parse_trace.duration_ms >= 0
        assert len(result.blocks) > 0

    def test_normalizer_marks_header(self, pipeline, sample_pdf):
        result = pipeline.parse(sample_pdf, doc_id="doc_test")
        # The header "OpenCraig Test Document" repeats on all 4 pages
        excluded = [b for b in result.blocks if b.excluded]
        assert len(excluded) > 0
        reasons = {b.excluded_reason for b in excluded}
        assert "header" in reasons or "footer" in reasons

    def test_reading_blocks_excludes_header_footer(self, pipeline, sample_pdf):
        result = pipeline.parse(sample_pdf, doc_id="doc_test")
        reading = result.reading_blocks()
        assert all(not b.excluded for b in reading)
        assert len(reading) < len(result.blocks)

    def test_blocks_by_id_lookup(self, pipeline, sample_pdf):
        result = pipeline.parse(sample_pdf, doc_id="doc_test")
        lookup = result.blocks_by_id()
        first = result.blocks[0]
        assert lookup[first.block_id] is first
