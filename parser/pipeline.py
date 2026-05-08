"""
Parser pipeline — the single public entry point.

Single explicit backend choice (``parser.backend``); no probe-driven
tier fallback chain. The pipeline:

    1. Quick-profiles the document (format, page count, file size).
    2. Dispatches to the chosen backend exactly once.
    3. Runs the normalizer.

If the chosen backend's optional dependency is missing, we raise a
clear error rather than silently fall back to PyMuPDF — the user
asked for that backend explicitly.

Typical usage:

    from config import load_config
    from parser.pipeline import ParserPipeline

    cfg = load_config("opencraig.yaml")
    pipeline = ParserPipeline.from_config(cfg)
    doc = pipeline.parse("paper.pdf", doc_id="doc_abc", parse_version=1)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import fitz  # PyMuPDF — already a transitive baseline dep

from config import AppConfig

from .backends.base import BackendUnavailable, ParserBackend
from .backends.pymupdf import PyMuPDFBackend
from .blob_store import BlobStore, make_blob_store
from .normalizer import normalize
from .schema import DocFormat, DocProfile, ParsedDocument, ParseTrace

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


_EXT_TO_FORMAT = {
    ".pdf": DocFormat.PDF,
    ".docx": DocFormat.DOCX,
    ".pptx": DocFormat.PPTX,
    ".html": DocFormat.HTML,
    ".htm": DocFormat.HTML,
    ".md": DocFormat.TEXT,
    ".txt": DocFormat.TEXT,
    # Spreadsheet-as-document path — parsed directly via
    # ``parser.backends.spreadsheet.SpreadsheetBackend``, no PDF
    # round-trip, no LibreOffice dependency.
    ".xlsx": DocFormat.SPREADSHEET,
    ".csv": DocFormat.SPREADSHEET,
    ".tsv": DocFormat.SPREADSHEET,
    # Image-as-document path — parsed directly via
    # ``parser.backends.image.ImageBackend``.
    ".png": DocFormat.IMAGE,
    ".jpg": DocFormat.IMAGE,
    ".jpeg": DocFormat.IMAGE,
    ".webp": DocFormat.IMAGE,
    ".gif": DocFormat.IMAGE,
    ".bmp": DocFormat.IMAGE,
    ".tif": DocFormat.IMAGE,
    ".tiff": DocFormat.IMAGE,
    # Legacy binary Office formats (.doc / .ppt / .xls) intentionally
    # absent — rejected at upload via ``LEGACY_OFFICE_EXTENSIONS``.
}


def _quick_profile(path: str) -> DocProfile:
    """Cheap inspection: format, page count, size. No heuristics."""
    p = Path(path)
    ext = p.suffix.lower()
    fmt = _EXT_TO_FORMAT.get(ext, DocFormat.PDF)
    size = p.stat().st_size if p.exists() else 0
    page_count = 1
    if fmt == DocFormat.PDF:
        try:
            with fitz.open(path) as doc:
                page_count = doc.page_count
        except Exception as e:
            log.warning("quick_profile: failed to open PDF for page count: %s", e)
    return DocProfile(
        page_count=page_count,
        format=fmt,
        file_size_bytes=size,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class ParserPipeline:
    def __init__(
        self,
        cfg: AppConfig,
        blob_store: BlobStore,
        backend: ParserBackend,
    ):
        self.cfg = cfg
        self.blob_store = blob_store
        self.backend = backend

    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg: AppConfig) -> ParserPipeline:
        """Build a pipeline from a validated AppConfig."""
        blob_store = make_blob_store(cfg.storage.to_dataclass())
        backend = _build_backend(cfg, blob_store)
        return cls(cfg=cfg, blob_store=blob_store, backend=backend)

    # ------------------------------------------------------------------
    def parse(
        self,
        path: str | Path,
        *,
        doc_id: str,
        parse_version: int = 1,
    ) -> ParsedDocument:
        """Parse a single document end-to-end."""
        path_str = str(path)

        # 1. Quick profile (page count + format)
        profile = _quick_profile(path_str)

        # 2. Pick backend by format. PDFs (and anything previously
        # converted to PDF upstream) use the configured backend
        # (pymupdf / mineru / mineru-vlm). Image-as-document uploads
        # bypass the PDF backends and go straight through
        # ``ImageBackend`` — a one-block parser that stores the
        # original bytes via the BlobStore and emits a single
        # ``BlockType.IMAGE`` block. The image_enrichment phase later
        # in the ingest pipeline fills the block's ``text`` with a
        # VLM-generated description; that description is what enters
        # retrieval / KG.
        if profile.format == DocFormat.IMAGE:
            from .backends.image import ImageBackend

            backend = ImageBackend(self.blob_store)
        elif profile.format == DocFormat.SPREADSHEET:
            # Spreadsheet-as-document path. Same shape as the image
            # branch above — no PDF round-trip, no LibreOffice, no
            # MinerU. ``SpreadsheetBackend`` produces one
            # ``BlockType.TABLE`` block per sheet; the
            # ``table_enrichment`` phase fills ``block.text`` with
            # an LLM-generated description later in the ingest
            # pipeline.
            from .backends.spreadsheet import SpreadsheetBackend

            backend = SpreadsheetBackend(self.blob_store)
        else:
            backend = self.backend

        log.info("parse start doc_id=%s path=%s backend=%s", doc_id, path_str, backend.name)

        # 3. Single-backend parse
        t0 = time.time()
        try:
            result = backend.parse(
                path=path_str,
                doc_id=doc_id,
                parse_version=parse_version,
                profile=profile,
            )
        except BackendUnavailable as e:
            log.error("backend %s unavailable: %s", backend.name, e)
            raise
        duration_ms = int((time.time() - t0) * 1000)
        result.parse_trace = ParseTrace(backend=backend.name, duration_ms=duration_ms)
        log.info(
            "parse done doc_id=%s backend=%s blocks=%d duration=%dms",
            doc_id,
            backend.name,
            len(result.blocks),
            duration_ms,
        )

        # 3. Normalizer (always runs; controlled by config switches)
        result = normalize(result, self.cfg.parser.normalize)
        excluded = sum(1 for b in result.blocks if b.excluded)
        log.info(
            "normalize done doc_id=%s excluded_blocks=%d reading_blocks=%d",
            doc_id,
            excluded,
            len(result.blocks) - excluded,
        )
        return result


# ---------------------------------------------------------------------------
# Backend wiring
# ---------------------------------------------------------------------------


def _build_backend(cfg: AppConfig, blob_store: BlobStore) -> ParserBackend:
    """Instantiate the single backend the user picked.

    Heavy backends (MinerU) are imported lazily so PyMuPDF-only
    deployments don't pay the optional-dep cost at import time.
    """
    choice = cfg.parser.backend  # "pymupdf" | "mineru" | "mineru-vlm"
    if choice == "pymupdf":
        return PyMuPDFBackend(cfg.parser.backends.pymupdf, blob_store)

    if choice in ("mineru", "mineru-vlm"):
        try:
            from .backends.mineru import MinerUBackend  # type: ignore
        except ImportError as e:
            raise BackendUnavailable(
                f"parser.backend={choice!r} requires the 'mineru' package. "
                "Install with: pip install mineru. "
                "Or pick parser.backend=pymupdf in your config."
            ) from e

        # Derive MinerU's internal sub-backend from the top-level choice +
        # whether the user provided a server_url for remote inference.
        mineru_cfg = cfg.parser.backends.mineru
        if choice == "mineru":
            sub = "pipeline"
        else:  # "mineru-vlm"
            sub = "vlm-http-client" if mineru_cfg.server_url else "vlm-auto-engine"
        return MinerUBackend(mineru_cfg.model_copy(update={"backend": sub}), blob_store)

    raise ValueError(f"unknown parser.backend: {choice!r}")


# ---------------------------------------------------------------------------
# Convenience functional API
# ---------------------------------------------------------------------------


_default_pipeline: ParserPipeline | None = None


def parse(
    path: str | Path,
    *,
    doc_id: str,
    parse_version: int = 1,
    cfg: AppConfig | None = None,
) -> ParsedDocument:
    """One-shot parse using a cached default pipeline. For production
    use prefer constructing ParserPipeline explicitly and reusing it."""
    global _default_pipeline
    if _default_pipeline is None or cfg is not None:
        from config import load_config

        _default_pipeline = ParserPipeline.from_config(cfg or load_config())
    return _default_pipeline.parse(path, doc_id=doc_id, parse_version=parse_version)
