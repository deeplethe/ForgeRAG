"""
PyMuPDF backend -- Layer 0, zero-dep, final fallback.

This backend is expected to always be available (PyMuPDF is a hard
dep of the parser package) and to always produce a ParsedDocument,
even if the content is poor. It relies on:

    - page.get_toc()      for embedded TOC (lossless when present)
    - page.get_text("dict") for blocks + bbox + font sizes
    - font-size based heuristic to tag headings

It does NOT attempt table structure recovery, OCR, multicolumn
reading-order fix, or figure extraction-as-image. Those are the
job of MinerU / VLM backends. Figures are still recorded as image
blocks (bbox only) so citations can highlight them.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from config import PyMuPDFConfig

from ..blob_store import BlobStore
from ..schema import (
    BBox,
    Block,
    BlockType,
    DocFormat,
    DocProfile,
    Page,
    ParsedDocument,
    TocEntry,
)
from .base import BackendUnavailable, ParserBackend


class PyMuPDFBackend(ParserBackend):
    name = "pymupdf"

    def __init__(self, cfg: PyMuPDFConfig, blob_store: BlobStore):
        super().__init__(blob_store)
        self.cfg = cfg

    # ------------------------------------------------------------------
    def parse(
        self,
        path: str,
        doc_id: str,
        parse_version: int,
        profile: DocProfile,
    ) -> ParsedDocument:
        try:
            import fitz
        except ImportError as e:
            raise BackendUnavailable("PyMuPDF not installed") from e

        doc = fitz.open(path)
        try:
            # 1. font-size analysis -> heading thresholds
            body_size, heading_sizes = self._analyze_fonts(doc)

            # 2. TOC
            toc = _extract_toc(doc)

            # 3. per-page blocks
            pages: list[Page] = []
            blocks: list[Block] = []

            for page_idx in range(doc.page_count):
                page = doc.load_page(page_idx)
                page_no = page_idx + 1
                # pdf.js renders using the intersection of CropBox and
                # MediaBox.  PyMuPDF normalises page.rect to (0,0) so
                # we must add back the effective origin offset.
                cb = page.cropbox
                mb = page.mediabox
                eff_x0 = max(cb.x0, mb.x0)
                eff_y0 = max(cb.y0, mb.y0)
                page_blocks = self._parse_page(
                    page=page,
                    page_no=page_no,
                    page_height=page.rect.height,
                    doc_id=doc_id,
                    parse_version=parse_version,
                    body_size=body_size,
                    heading_sizes=heading_sizes,
                    cropbox_x0=eff_x0,
                    cropbox_y0=eff_y0,
                )
                blocks.extend(page_blocks)
                pages.append(
                    Page(
                        page_no=page_no,
                        width=page.rect.width,
                        height=page.rect.height,
                        block_ids=[b.block_id for b in page_blocks],
                    )
                )

            from ..schema import ParseTrace

            return ParsedDocument(
                doc_id=doc_id,
                filename=Path(path).name,
                format=DocFormat.PDF,
                parse_version=parse_version,
                profile=profile,
                parse_trace=ParseTrace(),  # pipeline overrides with backend + duration
                pages=pages,
                blocks=blocks,
                toc=toc,
            )
        finally:
            doc.close()

    # ==================================================================
    # Internal helpers
    # ==================================================================

    @staticmethod
    def _analyze_fonts(doc) -> tuple[int, set[int]]:
        """
        Walk a sample of pages, build a histogram of rounded font
        sizes weighted by character count, return (body_size, heading_sizes).

        body_size = the most common size (by char count).
        heading_sizes = sizes strictly larger than body that represent
                        less than 15% of total characters (i.e. not body
                        continuation, actually headings).
        """
        sizes: Counter[int] = Counter()
        sample_n = min(doc.page_count, 50)
        for i in range(sample_n):
            page = doc.load_page(i)
            for blk in page.get_text("dict").get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                for line in blk.get("lines", []):
                    for span in line.get("spans", []):
                        sz = span.get("size")
                        if sz:
                            sizes[round(sz)] += len(span.get("text", ""))
        if not sizes:
            return 10, set()
        body_size = max(sizes.items(), key=lambda kv: kv[1])[0]
        total = sum(sizes.values())
        heading_sizes = {s for s, c in sizes.items() if s > body_size and c / total < 0.15}
        return body_size, heading_sizes

    def _parse_page(
        self,
        *,
        page,
        page_no: int,
        page_height: float,
        doc_id: str,
        parse_version: int,
        body_size: int,
        heading_sizes: set[int],
        cropbox_x0: float = 0.0,
        cropbox_y0: float = 0.0,
    ) -> list[Block]:
        blocks_out: list[Block] = []
        seq = 0

        text_dict = page.get_text("dict")
        for raw_blk in text_dict.get("blocks", []):
            blk_type = raw_blk.get("type", 0)
            # fitz returns bbox in top-left origin; flip to bottom-left to
            # match ParsedDocument.schema contract.  Add CropBox offsets so
            # coordinates align with pdf.js's native coordinate system.
            bbox: BBox = _flip_y(
                raw_blk.get("bbox", (0, 0, 0, 0)),
                page_height,
                x_offset=cropbox_x0,
                y_offset=cropbox_y0,
            )

            if blk_type == 1:
                # Image block -- extract bytes to BlobStore + record bbox.
                # Skip tiny decorative elements (lines, borders, icons)
                bw = abs(bbox[2] - bbox[0])
                bh = abs(bbox[3] - bbox[1])
                if bw * bh < 2500:  # < 50×50 pt
                    continue
                seq += 1
                storage_key = None
                mime = None
                img_bytes = raw_blk.get("image")
                img_ext = (raw_blk.get("ext") or "png").lower()
                if img_bytes and len(img_bytes) > 100:  # skip tiny placeholders
                    from ..blob_store import image_key as _fkey

                    mime = {
                        "png": "image/png",
                        "jpg": "image/jpeg",
                        "jpeg": "image/jpeg",
                        "webp": "image/webp",
                    }.get(img_ext, "image/png")
                    storage_key = _fkey(doc_id, parse_version, page_no, seq, ext=img_ext)
                    try:
                        self.blob_store.put(storage_key, img_bytes, mime)
                    except Exception:
                        storage_key = None
                blocks_out.append(
                    Block(
                        block_id=_bid(doc_id, parse_version, page_no, seq),
                        doc_id=doc_id,
                        parse_version=parse_version,
                        page_no=page_no,
                        seq=seq,
                        bbox=bbox,
                        type=BlockType.IMAGE,
                        text="",
                        confidence=0.5,
                        image_storage_key=storage_key,
                        image_mime=mime,
                    )
                )
                continue

            # Text block
            lines = raw_blk.get("lines", [])
            if not lines:
                continue

            text_parts: list[str] = []
            line_sizes: list[int] = []
            for line in lines:
                spans = line.get("spans", [])
                for span in spans:
                    txt = span.get("text", "")
                    if not txt:
                        continue
                    text_parts.append(txt)
                    sz = span.get("size")
                    if sz:
                        line_sizes.append(round(sz))
                text_parts.append(" ")
            text = "".join(text_parts).strip()
            if not text:
                continue

            # Heading detection: majority font size of this block
            block_size = max(set(line_sizes), key=line_sizes.count) if line_sizes else body_size
            if block_size in heading_sizes:
                btype = BlockType.HEADING
                level = _heading_level(block_size, sorted(heading_sizes, reverse=True))
            else:
                btype = BlockType.PARAGRAPH
                level = None

            seq += 1
            blocks_out.append(
                Block(
                    block_id=_bid(doc_id, parse_version, page_no, seq),
                    doc_id=doc_id,
                    parse_version=parse_version,
                    page_no=page_no,
                    seq=seq,
                    bbox=bbox,
                    type=btype,
                    level=level,
                    text=text,
                    confidence=1.0,
                )
            )

        return blocks_out


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _flip_y(
    raw_bbox,
    page_height: float,
    *,
    x_offset: float = 0.0,
    y_offset: float = 0.0,
) -> BBox:
    """Convert fitz's top-left bbox to PDF-native bottom-left coordinates.

    PyMuPDF normalises page coordinates so that page.rect starts at (0,0).
    If the original PDF CropBox has a non-zero origin (e.g. x0=66.33)
    the normalised coordinates will be shifted.  ``x_offset`` / ``y_offset``
    are the CropBox origin values that must be added back so coordinates
    align with what pdf.js ``viewport.convertToViewportPoint()`` expects.
    """
    x0, y0_tl, x1, y1_tl = raw_bbox
    return (
        float(x0 + x_offset),
        float(page_height - y1_tl + y_offset),
        float(x1 + x_offset),
        float(page_height - y0_tl + y_offset),
    )


def _bid(doc_id: str, parse_version: int, page_no: int, seq: int) -> str:
    return f"{doc_id}:{parse_version}:{page_no}:{seq}"


def _heading_level(size: int, heading_sizes_desc: list[int]) -> int:
    """Map a heading font size to a level 1..6."""
    try:
        idx = heading_sizes_desc.index(size)
    except ValueError:
        return 6
    return min(6, idx + 1)


def _extract_toc(doc) -> list[TocEntry] | None:
    """
    Convert PyMuPDF's flat TOC (list of [level, title, page]) into a
    nested TocEntry tree.
    """
    raw = doc.get_toc(simple=True) or []
    if not raw:
        return None

    root: list[TocEntry] = []
    stack: list[TocEntry] = []
    for level, title, page in raw:
        entry = TocEntry(level=level, title=title, page_no=page)
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(entry)
        else:
            root.append(entry)
        stack.append(entry)
    return root
