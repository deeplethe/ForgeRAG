"""
Image-as-document backend.

Native parser for raster image uploads (PNG, JPEG, WEBP, GIF, BMP,
TIFF). Produces a one-block ``ParsedDocument`` whose single block:

    * has type ``BlockType.IMAGE``
    * carries the original image as a content-addressed blob via
      ``image_storage_key`` (frontend renders it with a plain ``<img>``
      tag, no PDF wrapper)
    * has a sentinel zero ``bbox`` because there's no spatial layout
      to reference inside a single image. Citation-highlight code paths
      remain compatible (``bbox = (0, 0, 0, 0)`` renders a zero-area
      rectangle = invisible) without forcing every downstream consumer
      to handle an Optional bbox.

The block's ``text`` field starts empty and gets filled by
``image_enrichment`` later in the pipeline (VLM describes the image).
That description becomes the chunk content → embedded by the text
embedder, indexed by BM25, and fed into KG extraction. So image docs
participate in retrieval through their VLM-generated description, not
through any image-embedding path (out of scope for v1; see follow-up
notes in ``docs/architecture.md`` once added).

Edge cases handled at parse time:
    * Multi-page TIFF: only the first page is ingested. We log a
      warning and discard the rest. Most TIFFs in the wild are single-
      page; multi-page support would inflate the schema (one block per
      page? one doc per page?) without clear product value yet.
    * Animated GIF: stored as-is. Browsers play the animation in the
      ``<img>`` tag automatically, so we don't pick a frame.
    * SVG / HEIC / AVIF: not supported here. SVG is vector and would
      need rasterisation; HEIC needs ``pillow-heif``. v1 keeps deps
      slim — the pipeline rejects these via the ``IMAGE_EXTENSIONS``
      whitelist before reaching this parser.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..blob_store import BlobStore, image_key
from ..schema import (
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


# Filenames-extension → canonical MIME, used when the OS / Pillow
# can't be relied on for content-type. Keeps frontend ``<img>`` tags
# happy (browsers refuse to render a binary-octet-stream blob).
_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _mime_from_path(path: str) -> str:
    return _EXT_TO_MIME.get(Path(path).suffix.lower(), "application/octet-stream")


class ImageBackend(ParserBackend):
    """One-block parser for image-as-document uploads.

    Doesn't take a config because there's nothing to tune yet — the
    behaviour is "store the bytes verbatim, emit one IMAGE block".
    Lives alongside PDF parsers so the pipeline can pick a backend by
    format without special-casing.
    """

    name = "image"

    def __init__(self, blob_store: BlobStore):
        super().__init__(blob_store)

    def parse(
        self,
        path: str,
        doc_id: str,
        parse_version: int,
        profile: DocProfile,
    ) -> ParsedDocument:
        try:
            from PIL import Image as PILImage
        except ImportError as e:
            # Pillow is a transitive dep of multiple things we already
            # require (fpdf2, pymupdf), but guard explicitly so a slim
            # install gets a useful error instead of an obscure import
            # crash mid-ingest.
            raise BackendUnavailable("Pillow required for image-as-document parsing") from e

        src = Path(path)
        mime = _mime_from_path(path)

        # Read just enough metadata to record the image's intrinsic
        # size — the page dimensions on the resulting "page" object
        # mirror the image pixel size so frontend code doing
        # ``page_width / page_height`` math has a sensible value.
        try:
            with PILImage.open(src) as img:
                width_px, height_px = img.size
                # Multi-frame check (TIFF / animated GIF). For TIFFs we
                # warn + take frame 0; for GIFs we don't enumerate
                # frames because the blob is stored as-is and the
                # browser plays the animation natively.
                n_frames = getattr(img, "n_frames", 1)
                if n_frames > 1 and src.suffix.lower() in (".tif", ".tiff"):
                    log.warning(
                        "Multi-page TIFF (%d pages) — only first page is ingested: %s",
                        n_frames,
                        src.name,
                    )
        except Exception as e:
            raise BackendUnavailable(f"Pillow failed to open {src.name}: {e}") from e

        # Persist the original bytes through the BlobStore using the
        # canonical image-key layout (matches PDF backends — keeps the
        # blob store path scheme uniform across image sources).
        # ``put(key, bytes, mime)`` is the project-wide signature, not
        # a content-addressed putter — caller chooses the key.
        ext = src.suffix.lstrip(".").lower() or "png"
        storage_key = image_key(doc_id, parse_version, page_no=1, block_seq=0, ext=ext)
        with src.open("rb") as fh:
            self.blob_store.put(storage_key, fh.read(), mime)

        block = Block(
            block_id=f"{doc_id}:{parse_version}:1:0",
            doc_id=doc_id,
            parse_version=parse_version,
            page_no=1,
            seq=0,
            # Sentinel bbox — image-as-document has no internal spatial
            # layout, so there's no meaningful sub-region to highlight
            # on citation click. (0,0,0,0) renders as a zero-area
            # rectangle in the frontend (invisible), which matches the
            # intent better than a full-page bbox would.
            bbox=(0.0, 0.0, 0.0, 0.0),
            type=BlockType.IMAGE,
            # ``text`` left empty here. The image_enrichment phase
            # walks IMAGE blocks and fills it with a VLM-generated
            # description; that description is what gets embedded,
            # BM25-indexed, and KG-extracted from. If image_enrichment
            # is disabled, the block stays text-empty and the doc is
            # not retrievable — by design (no description, no signal).
            text="",
            image_storage_key=storage_key,
            image_mime=mime,
        )

        page = Page(
            page_no=1,
            width=float(width_px),
            height=float(height_px),
            block_ids=[block.block_id],
        )

        return ParsedDocument(
            doc_id=doc_id,
            filename=src.name,
            format=DocFormat.IMAGE,
            parse_version=parse_version,
            profile=profile,
            parse_trace=ParseTrace(),  # pipeline fills backend + duration
            pages=[page],
            blocks=[block],
            toc=None,
        )
