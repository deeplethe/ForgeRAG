"""
Unit tests for the Phase-2.5 rich-output persister.

Covers:
- empty list → no scratch dir created
- single PNG → bytes correctly base64-decoded + written
- HTML / SVG / JSON → text-shaped MIMEs UTF-8 encoded
- Plotly bundle (already-parsed dict) → JSON-dumped
- multi-output call → all files share a batch token + sequential
  numbering + correct extensions
- text/plain only → skipped (already in stdout)
- unknown MIME bundle → skipped, doesn't crash
- richest-MIME pick: image/png wins over text/plain in same bundle
- README written exactly once, never overwrites a hand-edited one
- size_bytes accurate
- workdir-relative path uses POSIX separator (cross-platform safe)
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from persistence.rich_output_persister import (
    RICH_OUTPUT_SUBDIR,
    RichOutputRef,
    persist_rich_outputs,
)


def _png_b64() -> str:
    """Tiny but valid PNG (1x1 transparent pixel)."""
    return base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000005000100"
            "0d0a2db40000000049454e44ae426082"
        )
    ).decode("ascii")


def test_empty_list_creates_no_directory(tmp_path):
    refs = persist_rich_outputs([], tmp_path)
    assert refs == []
    assert not (tmp_path / RICH_OUTPUT_SUBDIR).exists()


def test_single_png_decoded_and_written(tmp_path):
    png = _png_b64()
    refs = persist_rich_outputs(
        [{"kind": "display_data", "data": {"image/png": png, "text/plain": "<Figure>"}}],
        tmp_path,
    )
    assert len(refs) == 1
    ref = refs[0]
    assert ref.mime == "image/png"
    assert ref.path.startswith(f"{RICH_OUTPUT_SUBDIR}/")
    assert ref.path.endswith(".png")
    # File on disk has the raw PNG bytes (not base64 string)
    target = tmp_path / ref.path
    assert target.exists()
    raw = target.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert ref.size_bytes == len(raw)


def test_html_written_as_utf8(tmp_path):
    html = "<table><tr><td>café</td></tr></table>"
    refs = persist_rich_outputs(
        [{"kind": "execute_result", "data": {"text/html": html, "text/plain": "..."}}],
        tmp_path,
    )
    assert len(refs) == 1
    assert refs[0].mime == "text/html"
    assert refs[0].path.endswith(".html")
    target = tmp_path / refs[0].path
    assert target.read_text(encoding="utf-8") == html


def test_svg_written_as_text(tmp_path):
    svg = '<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    refs = persist_rich_outputs(
        [{"kind": "display_data", "data": {"image/svg+xml": svg}}],
        tmp_path,
    )
    assert len(refs) == 1
    assert refs[0].mime == "image/svg+xml"
    assert refs[0].path.endswith(".svg")
    assert (tmp_path / refs[0].path).read_text() == svg


def test_plotly_dict_payload_serialised_to_json(tmp_path):
    plotly_bundle = {"data": [{"x": [1, 2, 3], "y": [4, 5, 6]}], "layout": {}}
    refs = persist_rich_outputs(
        [{"kind": "display_data", "data": {
            "application/vnd.plotly.v1+json": plotly_bundle,
            "text/plain": "<Figure>",
        }}],
        tmp_path,
    )
    assert len(refs) == 1
    assert refs[0].mime == "application/vnd.plotly.v1+json"
    assert refs[0].path.endswith(".plotly.json")
    on_disk = json.loads((tmp_path / refs[0].path).read_text())
    assert on_disk == plotly_bundle


def test_multi_output_shares_batch_token(tmp_path):
    refs = persist_rich_outputs(
        [
            {"kind": "display_data", "data": {"image/png": _png_b64()}},
            {"kind": "execute_result", "data": {"text/html": "<p>1</p>"}},
            {"kind": "display_data", "data": {"image/png": _png_b64()}},
        ],
        tmp_path,
    )
    assert len(refs) == 3
    # All three share the same batch token (chars before the dash)
    tokens = {Path(r.path).name.split("-")[0] for r in refs}
    assert len(tokens) == 1, f"expected one batch token, got {tokens}"
    # Sequential numbering
    seqs = [Path(r.path).stem.split("-")[1] for r in refs]
    assert seqs == ["01", "02", "03"]
    # Extensions track the chosen MIME
    assert [Path(r.path).suffix for r in refs] == [".png", ".html", ".png"]


def test_text_plain_only_is_skipped(tmp_path):
    """A bundle with ONLY text/plain produces nothing — that text is
    already in stdout."""
    refs = persist_rich_outputs(
        [{"kind": "execute_result", "data": {"text/plain": "42"}}],
        tmp_path,
    )
    assert refs == []


def test_unknown_mime_skipped_doesnt_crash(tmp_path):
    refs = persist_rich_outputs(
        [{"kind": "display_data", "data": {"application/x-mystery": "..."}}],
        tmp_path,
    )
    assert refs == []


def test_richer_mime_wins_over_text_plain(tmp_path):
    refs = persist_rich_outputs(
        [{"kind": "execute_result", "data": {
            "image/png": _png_b64(),
            "text/plain": "<matplotlib.Figure>",
        }}],
        tmp_path,
    )
    # png chosen, text/plain ignored (already in stdout)
    assert refs[0].mime == "image/png"


def test_readme_written_once(tmp_path):
    persist_rich_outputs(
        [{"kind": "display_data", "data": {"image/png": _png_b64()}}],
        tmp_path,
    )
    readme = tmp_path / RICH_OUTPUT_SUBDIR / "README.md"
    assert readme.exists()
    edited = "# operator-edited\n\nkeep me\n"
    readme.write_text(edited, encoding="utf-8")
    # Second batch — readme survives
    persist_rich_outputs(
        [{"kind": "display_data", "data": {"image/png": _png_b64()}}],
        tmp_path,
    )
    assert readme.read_text(encoding="utf-8") == edited


def test_path_is_posix_relative(tmp_path):
    """``path`` must use ``/`` separator regardless of host OS so
    the frontend can pass it straight to the project file-download
    URL builder."""
    refs = persist_rich_outputs(
        [{"kind": "display_data", "data": {"image/png": _png_b64()}}],
        tmp_path,
    )
    assert "/" in refs[0].path
    assert "\\" not in refs[0].path


def test_to_summary_dict_shape():
    ref = RichOutputRef(
        kind="display_data", mime="image/png",
        path="scratch/_rich_outputs/abc-01.png", size_bytes=42,
    )
    d = ref.to_summary_dict()
    assert d == {
        "kind": "display_data",
        "mime": "image/png",
        "path": "scratch/_rich_outputs/abc-01.png",
        "size_bytes": 42,
    }


def test_malformed_entries_skipped(tmp_path):
    """Defensive: handler shouldn't trust the shape from KernelManager
    100% — partial / bad entries skip cleanly."""
    refs = persist_rich_outputs(
        [
            "not a dict",
            {"kind": "display_data"},  # no data key
            {"data": "not a dict"},
            {"kind": "display_data", "data": {"image/png": _png_b64()}},  # valid
        ],
        tmp_path,
    )
    # Only the last entry persists — others silently skipped
    assert len(refs) == 1
    assert refs[0].mime == "image/png"
