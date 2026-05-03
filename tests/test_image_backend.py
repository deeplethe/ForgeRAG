"""Unit tests for ``parser.backends.image.ImageBackend``.

The image-as-document parser is small but it has three contracts that
matter to downstream consumers:

  1. Returns a one-block ``ParsedDocument`` with ``BlockType.IMAGE``
     and ``DocFormat.IMAGE``.
  2. The block stores the original bytes through the BlobStore under
     the canonical ``image_key`` layout, and ``image_storage_key`` /
     ``image_mime`` point at it correctly.
  3. The block carries the sentinel zero ``bbox`` (no spatial layout
     for image-as-document — citation highlight degenerates to a
     zero-area rect, which renders as nothing in the frontend).

Pillow is stubbed for the metadata read so the test doesn't need a
real PNG decoder available — keeps the test light and skip-resistant
on minimal CI installs.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from parser.backends.image import ImageBackend
from parser.schema import BlockType, DocFormat, DocProfile


def _make_tiny_png(path: Path, width: int = 4, height: int = 3) -> None:
    """Hand-roll a minimal valid PNG so tests don't need image fixtures.

    Just enough header + IHDR + IDAT (one zlib-compressed scanline of
    zeros) + IEND for Pillow to open() and read .size off it. Smaller
    than a fixture file and avoids binary blobs in the test tree.
    """
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b"".join(b"\x00" + b"\x00" * (3 * width) for _ in range(height))
    idat = zlib.compress(raw)
    path.write_bytes(sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


class _CapturingBlobStore:
    """In-memory BlobStore stub that captures (key, data, mime) tuples
    so tests can assert what the backend wrote without round-tripping
    through a real LocalBlobStore."""

    def __init__(self):
        self.calls: list[tuple[str, bytes, str]] = []

    def put(self, key: str, data: bytes, mime: str) -> str:
        self.calls.append((key, data, mime))
        return key

    # The rest of the BlobStore protocol is unused by the backend but
    # included to satisfy any structural-typing checks downstream.
    def get(self, key: str) -> bytes:
        for k, d, _ in self.calls:
            if k == key:
                return d
        raise FileNotFoundError(key)


# Skip the whole module if Pillow isn't available — the backend
# explicitly raises BackendUnavailable when import fails, but the
# tests themselves need PIL to verify metadata extraction.
PIL = pytest.importorskip("PIL.Image")


def test_image_backend_returns_one_block(tmp_path: Path):
    img = tmp_path / "tomato.png"
    _make_tiny_png(img, width=10, height=6)

    blob = _CapturingBlobStore()
    backend = ImageBackend(blob)
    profile = DocProfile(format=DocFormat.IMAGE, page_count=1, file_size_bytes=img.stat().st_size)

    result = backend.parse(
        path=str(img),
        doc_id="doc_test",
        parse_version=1,
        profile=profile,
    )

    assert result.format == DocFormat.IMAGE
    assert result.filename == "tomato.png"
    assert len(result.blocks) == 1
    assert len(result.pages) == 1


def test_image_backend_block_shape(tmp_path: Path):
    img = tmp_path / "blueprint.jpg"
    _make_tiny_png(img, width=20, height=20)  # PNG bytes; .jpg mime via extension

    blob = _CapturingBlobStore()
    backend = ImageBackend(blob)
    profile = DocProfile(format=DocFormat.IMAGE, page_count=1, file_size_bytes=img.stat().st_size)
    result = backend.parse(path=str(img), doc_id="doc_x", parse_version=1, profile=profile)

    block = result.blocks[0]
    assert block.type == BlockType.IMAGE
    assert block.text == ""  # filled later by image_enrichment
    # Sentinel zero bbox — no spatial layout for image-as-document.
    assert block.bbox == (0.0, 0.0, 0.0, 0.0)
    # MIME comes from extension lookup, not file inspection.
    assert block.image_mime == "image/jpeg"
    # Storage key follows the canonical ``images/{doc}/v{ver}/...`` layout.
    assert block.image_storage_key is not None
    assert block.image_storage_key.startswith("images/doc_x/v1/")


def test_image_backend_persists_bytes_through_blob_store(tmp_path: Path):
    img = tmp_path / "diagram.png"
    _make_tiny_png(img, width=8, height=8)
    expected_bytes = img.read_bytes()

    blob = _CapturingBlobStore()
    backend = ImageBackend(blob)
    profile = DocProfile(format=DocFormat.IMAGE, page_count=1, file_size_bytes=img.stat().st_size)
    backend.parse(path=str(img), doc_id="doc_y", parse_version=2, profile=profile)

    # Exactly one put() — image-as-document is a single-block doc.
    assert len(blob.calls) == 1
    key, data, mime = blob.calls[0]
    assert key.startswith("images/doc_y/v2/")
    assert data == expected_bytes
    assert mime == "image/png"


def test_image_backend_page_dimensions_match_pixel_size(tmp_path: Path):
    img = tmp_path / "chart.png"
    _make_tiny_png(img, width=200, height=80)

    blob = _CapturingBlobStore()
    backend = ImageBackend(blob)
    profile = DocProfile(format=DocFormat.IMAGE, page_count=1, file_size_bytes=img.stat().st_size)
    result = backend.parse(path=str(img), doc_id="doc_z", parse_version=1, profile=profile)

    page = result.pages[0]
    # Page dimensions mirror pixel size so any downstream code doing
    # geometry math (KG mini, citation viewer) gets a meaningful
    # aspect ratio without special-casing IMAGE format.
    assert page.width == 200.0
    assert page.height == 80.0


def test_image_backend_unknown_extension_falls_to_octet_stream(tmp_path: Path):
    """Defense in depth — if a file with an unfamiliar extension
    sneaks through the upstream IMAGE_EXTENSIONS gate, the backend
    shouldn't crash; it should record ``application/octet-stream``
    so the frontend's <img> tag fails visibly rather than rendering
    something nonsensical."""
    img = tmp_path / "weird.qoi"  # not in our MIME map
    _make_tiny_png(img, width=4, height=4)  # but it's actually a PNG body

    blob = _CapturingBlobStore()
    backend = ImageBackend(blob)
    profile = DocProfile(format=DocFormat.IMAGE, page_count=1, file_size_bytes=img.stat().st_size)
    result = backend.parse(path=str(img), doc_id="doc_q", parse_version=1, profile=profile)

    assert result.blocks[0].image_mime == "application/octet-stream"
