"""
MinerU backend -- Layer 1.

Uses the MinerU Python API `mineru.cli.common.do_parse`. The
signature has been stable across 2.x and 3.0 releases, so one
wrapper handles both. The only moving piece is `backend=`: 3.0
renamed the VLM backends and added the hybrid family.

    backend values (3.0, preferred):
        pipeline              fast, CPU/GPU, stable
        hybrid-auto-engine    next-gen, best accuracy, local
        hybrid-http-client    hybrid via remote OpenAI-style server
        vlm-auto-engine       VLM backend, local
        vlm-http-client       VLM backend, remote

    backend values (2.x legacy, still accepted when running 2.x):
        vlm-transformers
        vlm-sglang-engine
        vlm-sglang-client

Any *-http-client / *-sglang-client backend requires cfg.server_url.

Why the API over subprocess:
    - No process-start overhead per document
    - Model handles stay loaded across calls
    - Cleaner error propagation (real exceptions, not exit codes)

Output layout (2.x and 3.0 identical):
    <out_dir>/<stem>/<parse_method>/<stem>_content_list.json
    <out_dir>/<stem>/<parse_method>/<stem>_middle.json
    <out_dir>/<stem>/<parse_method>/images/*.jpg

content_list.json is a flat array of items:
    {
      "type": "text" | "title" | "image" | "table" | "equation",
      "text": "...",               # for text/title
      "text_level": 1,             # for titles
      "img_path": "images/xxx.jpg",# for image/table
      "table_body": "<html>",      # for table
      "page_idx": 0,               # 0-based
      "bbox": [x0, y0, x1, y1]     # PDF points, origin TOP-LEFT
    }

We convert bbox to our convention (origin bottom-left) using the
page height obtained by opening the PDF with PyMuPDF -- cheap and
already a dep.

If the API call fails or the output layout doesn't match, we
raise BackendUnavailable so the router falls through to PyMuPDF.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from config import MinerUConfig

from ..blob_store import BlobStore, figure_key
from ..schema import (
    BBox,
    Block,
    BlockType,
    DocFormat,
    DocProfile,
    Page,
    ParsedDocument,
    ParseTrace,
)
from .base import BackendUnavailable, ParserBackend

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MinerU type -> our BlockType
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "text": BlockType.PARAGRAPH,
    "title": BlockType.HEADING,
    "image": BlockType.FIGURE,
    "table": BlockType.TABLE,
    "equation": BlockType.FORMULA,
    "formula": BlockType.FORMULA,
    "list": BlockType.LIST,
}

# Minimum area (in PDF points²) for a figure to be kept.
# Anything smaller is likely a decorative element (line, border, icon).
# 50×50 pt ≈ 17×17 mm — small but still meaningful.
_MIN_FIGURE_AREA = 2500.0  # 50 * 50


# ---------------------------------------------------------------------------


class MinerUBackend(ParserBackend):
    name = "mineru"

    def __init__(self, cfg: MinerUConfig, blob_store: BlobStore):
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
        in_path = Path(path).resolve()
        if not in_path.exists():
            raise BackendUnavailable(f"input not found: {path}")

        _tmp_root = Path("./storage/tmp")
        _tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mineru_", dir=str(_tmp_root)) as tmp:
            out_dir = Path(tmp)
            self._run_api(in_path, out_dir)
            content_list, images_dir = self._locate_outputs(out_dir, in_path.stem)

            page_dims = _page_dimensions(in_path)  # {page_no: (w, h)}
            blocks, page_block_map = self._map_blocks(
                content_list=content_list,
                images_dir=images_dir,
                page_dims=page_dims,
                doc_id=doc_id,
                parse_version=parse_version,
            )

        pages = [
            Page(
                page_no=pn,
                width=page_dims.get(pn, (0.0, 0.0))[0],
                height=page_dims.get(pn, (0.0, 0.0))[1],
                block_ids=page_block_map.get(pn, []),
            )
            for pn in sorted(page_dims.keys())
        ]

        return ParsedDocument(
            doc_id=doc_id,
            filename=in_path.name,
            format=DocFormat.PDF,
            parse_version=parse_version,
            profile=profile,
            parse_trace=ParseTrace(),
            pages=pages,
            blocks=blocks,
            toc=None,  # MinerU does not reliably expose PDF TOC; leave to probe
        )

    # ==================================================================
    # Internals
    # ==================================================================

    def _run_api(self, in_path: Path, out_dir: Path) -> None:
        """
        Call mineru.cli.common.do_parse. This is the canonical
        high-level API in MinerU 2.x; it handles both the pipeline
        and the VLM backends behind a single signature.
        """
        try:
            from mineru.cli.common import do_parse, read_fn
        except ImportError as e:
            raise BackendUnavailable(f"mineru not importable: {e}") from e

        try:
            pdf_bytes = read_fn(in_path)
        except Exception as e:
            raise BackendUnavailable(f"mineru read_fn failed: {e}") from e

        kwargs: dict[str, Any] = dict(
            output_dir=str(out_dir),
            pdf_file_names=[in_path.stem],
            pdf_bytes_list=[pdf_bytes],
            p_lang_list=[self.cfg.lang],
            backend=self.cfg.backend,
            parse_method=self.cfg.parse_method,
            formula_enable=self.cfg.formula_enable,
            table_enable=self.cfg.table_enable,
        )
        # Remote backends require server_url. Covers both the MinerU
        # 3.0 *-http-client names and the 2.x *-sglang-client name.
        if self.cfg.backend.endswith("-http-client") or self.cfg.backend == "vlm-sglang-client":
            if not self.cfg.server_url:
                raise BackendUnavailable(f"mineru backend={self.cfg.backend} requires server_url")
            kwargs["server_url"] = self.cfg.server_url

        log.info(
            "mineru api call doc=%s backend=%s method=%s",
            in_path.name,
            self.cfg.backend,
            self.cfg.parse_method,
        )
        try:
            do_parse(**kwargs)
        except TypeError as e:
            # Version-skew safety net: retry with only the core kwargs
            log.warning("mineru do_parse kwargs mismatch (%s); retrying minimal", e)
            try:
                do_parse(
                    output_dir=str(out_dir),
                    pdf_file_names=[in_path.stem],
                    pdf_bytes_list=[pdf_bytes],
                    p_lang_list=[self.cfg.lang],
                    backend=self.cfg.backend,
                    parse_method=self.cfg.parse_method,
                )
            except Exception as e2:
                raise BackendUnavailable(f"mineru do_parse failed: {e2}") from e2
        except Exception as e:
            raise BackendUnavailable(f"mineru do_parse failed: {e}") from e

    # ------------------------------------------------------------------
    def _locate_outputs(self, out_dir: Path, stem: str) -> tuple[list[dict[str, Any]], Path | None]:
        """
        Find content_list.json and the images directory regardless of
        minor layout variations across MinerU versions.
        """
        candidates = list(out_dir.rglob("*_content_list.json"))
        if not candidates:
            # Some versions use different suffix
            candidates = list(out_dir.rglob("content_list.json"))
        if not candidates:
            raise BackendUnavailable(f"mineru output not found under {out_dir}")
        content_path = candidates[0]
        with open(content_path, encoding="utf-8") as f:
            content_list = json.load(f)

        images_dir = content_path.parent / "images"
        if not images_dir.exists():
            images_dir = None  # type: ignore
        return content_list, images_dir

    # ------------------------------------------------------------------
    def _map_blocks(
        self,
        *,
        content_list: list[dict[str, Any]],
        images_dir: Path | None,
        page_dims: dict[int, tuple[float, float]],
        doc_id: str,
        parse_version: int,
    ) -> tuple[list[Block], dict[int, list[str]]]:
        blocks: list[Block] = []
        page_seq: dict[int, int] = {}
        page_block_map: dict[int, list[str]] = {}

        for item in content_list:
            mtype_raw = item.get("type", "text")
            btype = _TYPE_MAP.get(mtype_raw, BlockType.PARAGRAPH)
            page_no = int(item.get("page_idx", 0)) + 1  # -> 1-based
            page_dim = page_dims.get(page_no)
            raw_bbox = item.get("bbox") or [0.0, 0.0, 0.0, 0.0]
            bbox = _bbox_to_bottomleft(raw_bbox, page_dim)

            seq = page_seq.get(page_no, 0) + 1
            page_seq[page_no] = seq
            block_id = f"{doc_id}:{parse_version}:{page_no}:{seq}"

            text = (item.get("text") or "").strip()
            level = item.get("text_level") if btype == BlockType.HEADING else None

            # Demote tiny "figures" to paragraphs — they are usually
            # decorative PDF elements (lines, borders, small icons).
            if btype == BlockType.FIGURE:
                bw = abs(bbox[2] - bbox[0])
                bh = abs(bbox[3] - bbox[1])
                if bw * bh < _MIN_FIGURE_AREA:
                    log.debug(
                        "demoting tiny figure %s (%.0f×%.0f = %.0f pt²)",
                        block_id,
                        bw,
                        bh,
                        bw * bh,
                    )
                    btype = BlockType.PARAGRAPH

            # Figure / table payload handling
            figure_storage_key = None
            figure_mime = None
            table_html = None
            table_markdown = None
            formula_latex = None

            if btype == BlockType.FIGURE and images_dir is not None:
                rel = item.get("img_path")
                if rel:
                    stored = self._store_image(
                        images_dir=images_dir,
                        rel=rel,
                        doc_id=doc_id,
                        parse_version=parse_version,
                        page_no=page_no,
                        seq=seq,
                    )
                    if stored:
                        figure_storage_key, figure_mime = stored

            if btype == BlockType.TABLE:
                table_html = item.get("table_body") or item.get("html")
                # MinerU may also emit a rasterized table image
                if images_dir is not None and item.get("img_path"):
                    stored = self._store_image(
                        images_dir=images_dir,
                        rel=item["img_path"],
                        doc_id=doc_id,
                        parse_version=parse_version,
                        page_no=page_no,
                        seq=seq,
                    )
                    if stored:
                        figure_storage_key, figure_mime = stored
                # Keep a plain-text view in `text` for LLM consumption
                if not text and table_html:
                    text = _html_to_text(table_html)

            if btype == BlockType.FORMULA:
                formula_latex = item.get("latex") or item.get("text")
                if not text:
                    text = formula_latex or ""

            blocks.append(
                Block(
                    block_id=block_id,
                    doc_id=doc_id,
                    parse_version=parse_version,
                    page_no=page_no,
                    seq=seq,
                    bbox=bbox,
                    type=btype,
                    level=level,
                    text=text,
                    confidence=float(item.get("score", 1.0) or 1.0),
                    table_html=table_html,
                    table_markdown=table_markdown,
                    figure_storage_key=figure_storage_key,
                    figure_mime=figure_mime,
                    formula_latex=formula_latex,
                )
            )
            page_block_map.setdefault(page_no, []).append(block_id)

        return blocks, page_block_map

    # ------------------------------------------------------------------
    def _store_image(
        self,
        *,
        images_dir: Path,
        rel: str,
        doc_id: str,
        parse_version: int,
        page_no: int,
        seq: int,
    ) -> tuple[str, str] | None:
        """
        Copy an image MinerU wrote to its temp dir into our BlobStore.
        Returns (storage_key, mime) or None if the file is missing.
        """
        # rel is like "images/xxx.jpg" -- relative to content_list.json dir
        src = (images_dir.parent / rel).resolve()
        if not src.exists():
            # Fall back: maybe rel already contains the subdir
            src = (images_dir / Path(rel).name).resolve()
        if not src.exists():
            log.warning("mineru image missing: %s", rel)
            return None

        ext = src.suffix.lstrip(".").lower() or "png"
        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(ext, "application/octet-stream")

        key = figure_key(doc_id, parse_version, page_no, seq, ext=ext)
        self.blob_store.put(key, src.read_bytes(), mime)
        return key, mime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page_dimensions(pdf_path: Path) -> dict[int, tuple[float, float]]:
    """Return {page_no(1-based): (width, height)} using PyMuPDF."""
    try:
        import fitz
    except ImportError as e:
        raise BackendUnavailable("PyMuPDF required for page dims") from e
    dims: dict[int, tuple[float, float]] = {}
    doc = fitz.open(pdf_path)
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            dims[i + 1] = (page.rect.width, page.rect.height)
    finally:
        doc.close()
    return dims


def _bbox_to_bottomleft(raw: list[float], page_dim: tuple[float, float] | None) -> BBox:
    """
    MinerU emits bbox in PDF points with origin TOP-LEFT.
    Our schema requires origin BOTTOM-LEFT. We flip y using
    page height.
    """
    if len(raw) != 4:
        return (0.0, 0.0, 0.0, 0.0)
    x0, y0_tl, x1, y1_tl = raw
    if page_dim is None:
        return (float(x0), float(y0_tl), float(x1), float(y1_tl))
    _, page_h = page_dim
    y0 = page_h - y1_tl
    y1 = page_h - y0_tl
    return (float(x0), float(y0), float(x1), float(y1))


def _html_to_text(html: str) -> str:
    """Crude HTML -> plain text for table fallback (no extra deps)."""
    import re

    text = re.sub(r"</(tr|p|div|h\d)>", "\n", html, flags=re.I)
    text = re.sub(r"</t[dh]>", " | ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()
