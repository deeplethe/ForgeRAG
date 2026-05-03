"""Unit tests for ``parser.backends.spreadsheet.SpreadsheetBackend``.

The spreadsheet-as-document parser has four contracts that downstream
consumers depend on:

  1. Returns a ``ParsedDocument`` with ``DocFormat.SPREADSHEET`` and
     **one** ``BlockType.TABLE`` block per sheet (csv/tsv = 1, xlsx = N).
  2. Each TABLE block carries the full sheet rendered as GFM markdown
     in ``table_markdown`` AND a deterministic-metadata fallback in
     ``block.text`` (later overwritten by the table_enrichment phase
     when an LLM is configured).
  3. Each block carries the sentinel zero ``bbox`` — there's no
     spatial layout to highlight.
  4. ``Page.name`` carries the sheet display name (xlsx sheet title /
     csv stem) so the SpreadsheetViewer tab strip has a label.

``count_cells`` is also tested because the upload route gate depends
on its accuracy at the ``SPREADSHEET_MAX_CELLS`` boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parser.backends.spreadsheet import (
    SpreadsheetBackend,
    count_cells,
    split_table_into_row_groups,
)
from parser.schema import BlockType, DocFormat, DocProfile

# Skip if openpyxl isn't installed — the xlsx path explicitly raises
# BackendUnavailable when the import fails, but the tests need it to
# verify behavior.
openpyxl = pytest.importorskip("openpyxl")


class _NullBlobStore:
    """SpreadsheetBackend never writes blobs (no per-cell images, no
    per-page rasters), but ParserBackend's __init__ requires one."""

    def put(self, key: str, data: bytes, mime: str) -> str:  # pragma: no cover
        return key

    def get(self, key: str) -> bytes:  # pragma: no cover
        raise FileNotFoundError(key)


# ---------------------------------------------------------------------------
# Test fixture builders
# ---------------------------------------------------------------------------


def _make_xlsx(path: Path, sheets: dict[str, list[list]]) -> None:
    """Write a multi-sheet xlsx where each entry is ``sheet_name -> rows``.

    First row of each sheet is treated as a header by the backend's
    heuristic when it doesn't look like data.
    """
    wb = openpyxl.Workbook()
    # openpyxl creates a default "Sheet"; remove it before populating.
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for r in rows:
            ws.append(r)
    wb.save(str(path))


def _make_csv(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.write_text(content, encoding=encoding)


# ---------------------------------------------------------------------------
# CSV path
# ---------------------------------------------------------------------------


def test_csv_produces_one_table_block(tmp_path: Path):
    p = tmp_path / "sales.csv"
    _make_csv(
        p,
        "region,quarter,revenue\n"
        "EMEA,Q1,1200\n"
        "NA,Q1,3400\n"
        "APAC,Q1,2200\n",
    )

    backend = SpreadsheetBackend(_NullBlobStore())
    profile = DocProfile(format=DocFormat.SPREADSHEET, page_count=1, file_size_bytes=p.stat().st_size)
    result = backend.parse(path=str(p), doc_id="doc_csv", parse_version=1, profile=profile)

    assert result.format == DocFormat.SPREADSHEET
    assert result.filename == "sales.csv"
    assert len(result.blocks) == 1
    assert len(result.pages) == 1


def test_csv_block_shape(tmp_path: Path):
    p = tmp_path / "users.csv"
    _make_csv(p, "id,name\n1,alice\n2,bob\n")

    backend = SpreadsheetBackend(_NullBlobStore())
    profile = DocProfile(format=DocFormat.SPREADSHEET, page_count=1, file_size_bytes=p.stat().st_size)
    result = backend.parse(path=str(p), doc_id="doc_u", parse_version=1, profile=profile)

    block = result.blocks[0]
    assert block.type == BlockType.TABLE
    # Sentinel zero bbox — no spatial layout for spreadsheets.
    assert block.bbox == (0.0, 0.0, 0.0, 0.0)
    # Description-fallback text is set by the backend (later overwritten
    # by table_enrichment if LLM is configured).
    assert block.text and "Sheet:" in block.text
    # table_markdown carries the full sheet rendered as GFM.
    assert block.table_markdown
    assert "| id | name |" in block.table_markdown
    assert "alice" in block.table_markdown
    assert "bob" in block.table_markdown


def test_csv_page_carries_sheet_name(tmp_path: Path):
    p = tmp_path / "inventory.csv"
    _make_csv(p, "sku,qty\nA,10\nB,20\n")

    backend = SpreadsheetBackend(_NullBlobStore())
    profile = DocProfile(format=DocFormat.SPREADSHEET, page_count=1, file_size_bytes=p.stat().st_size)
    result = backend.parse(path=str(p), doc_id="doc_inv", parse_version=1, profile=profile)

    # Single page; CSV sheet name = file stem (so the viewer's tab
    # strip has a meaningful label rather than "Sheet 1").
    assert result.pages[0].name == "inventory"
    assert result.pages[0].page_no == 1


def test_tsv_uses_tab_delimiter(tmp_path: Path):
    """TSV files use ``\\t`` as the delimiter; the parser distinguishes
    by extension."""
    p = tmp_path / "log.tsv"
    p.write_text("col1\tcol2\nfoo\tbar\nbaz\tqux\n", encoding="utf-8")

    backend = SpreadsheetBackend(_NullBlobStore())
    profile = DocProfile(format=DocFormat.SPREADSHEET, page_count=1, file_size_bytes=p.stat().st_size)
    result = backend.parse(path=str(p), doc_id="doc_t", parse_version=1, profile=profile)

    md = result.blocks[0].table_markdown
    # Headers correctly split on tab — if delimiter were comma we'd
    # see one fat column "col1\tcol2".
    assert "| col1 | col2 |" in md
    assert "foo" in md and "bar" in md


# ---------------------------------------------------------------------------
# XLSX path
# ---------------------------------------------------------------------------


def test_xlsx_one_block_per_sheet(tmp_path: Path):
    p = tmp_path / "report.xlsx"
    _make_xlsx(
        p,
        {
            "Summary": [["metric", "value"], ["users", 100], ["orders", 25]],
            "Detail":  [["id", "amount"], [1, 19.5], [2, 22.0]],
            "Notes":   [["text"], ["see Q3 review"]],
        },
    )

    backend = SpreadsheetBackend(_NullBlobStore())
    profile = DocProfile(format=DocFormat.SPREADSHEET, page_count=3, file_size_bytes=p.stat().st_size)
    result = backend.parse(path=str(p), doc_id="doc_xlsx", parse_version=1, profile=profile)

    assert result.format == DocFormat.SPREADSHEET
    # Three sheets → three TABLE blocks → three pages.
    assert len(result.blocks) == 3
    assert len(result.pages) == 3
    for blk in result.blocks:
        assert blk.type == BlockType.TABLE


def test_xlsx_page_names_match_sheet_titles(tmp_path: Path):
    p = tmp_path / "multi.xlsx"
    _make_xlsx(
        p,
        {
            "Sales Data":  [["region", "rev"], ["EMEA", 100]],
            "Forecast":    [["yr", "rev"], [2025, 120]],
        },
    )

    backend = SpreadsheetBackend(_NullBlobStore())
    profile = DocProfile(format=DocFormat.SPREADSHEET, page_count=2, file_size_bytes=p.stat().st_size)
    result = backend.parse(path=str(p), doc_id="doc_m", parse_version=1, profile=profile)

    names = [pg.name for pg in result.pages]
    # Sheet display names — fed into ``Page.name`` so the
    # SpreadsheetViewer tab strip shows them and the heading
    # injected into block.text by table_enrichment uses them.
    assert names == ["Sales Data", "Forecast"]
    # ``page_no`` is 1-indexed in the order sheets appear in the workbook.
    assert [pg.page_no for pg in result.pages] == [1, 2]


def test_xlsx_block_ids_are_unique_per_sheet(tmp_path: Path):
    """The chunker keys retrieval citations off block_id, so a
    collision across sheets would silently merge their citations."""
    p = tmp_path / "two_sheets.xlsx"
    _make_xlsx(
        p,
        {
            "A": [["c"], [1]],
            "B": [["c"], [2]],
        },
    )

    backend = SpreadsheetBackend(_NullBlobStore())
    profile = DocProfile(format=DocFormat.SPREADSHEET, page_count=2, file_size_bytes=p.stat().st_size)
    result = backend.parse(path=str(p), doc_id="doc_two", parse_version=3, profile=profile)

    ids = [blk.block_id for blk in result.blocks]
    assert len(ids) == len(set(ids))
    # ID format embeds doc_id + parse_version + page_no, so re-ingest
    # at the same parse_version cleanly overwrites.
    assert all(b.startswith("doc_two:3:") for b in ids)


# ---------------------------------------------------------------------------
# count_cells (upload-route gate)
# ---------------------------------------------------------------------------


def test_count_cells_csv(tmp_path: Path):
    p = tmp_path / "small.csv"
    _make_csv(p, "a,b,c\n1,2,3\n4,5,6\n")
    # 3 rows × 3 cols (header counted — matches the upload-route
    # invariant where the gate counts every cell stored on disk).
    assert count_cells(p) == 9


def test_count_cells_xlsx(tmp_path: Path):
    p = tmp_path / "x.xlsx"
    _make_xlsx(
        p,
        {
            "S1": [["a", "b"], [1, 2], [3, 4]],         # 3 × 2 = 6
            "S2": [["x", "y", "z"], [10, 20, 30]],      # 2 × 3 = 6
        },
    )
    assert count_cells(p) == 12


def test_count_cells_unknown_extension_returns_zero(tmp_path: Path):
    """Defensive: a non-spreadsheet extension shouldn't crash the gate
    (it just sees zero and lets the rest of the pipeline reject)."""
    p = tmp_path / "weird.dat"
    p.write_bytes(b"not a spreadsheet")
    assert count_cells(p) == 0


# ---------------------------------------------------------------------------
# split_table_into_row_groups (table_enrichment map-reduce input)
# ---------------------------------------------------------------------------


def test_split_table_keeps_header_in_each_fragment():
    md = (
        "| a | b |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "| 3 | 4 |\n"
        "| 5 | 6 |\n"
        "| 7 | 8 |\n"
        "| 9 | 0 |\n"
    )
    fragments = split_table_into_row_groups(md, rows_per_group=2)
    # 5 rows / 2 per group → 3 fragments.
    assert len(fragments) == 3
    for frag in fragments:
        # Each fragment is itself a valid markdown table — header +
        # separator + at least one data row. The map-reduce step
        # in graph.summarize relies on this invariant.
        assert frag.startswith("| a | b |")
        assert "|---|---|" in frag


def test_split_table_returns_single_fragment_when_under_limit():
    md = (
        "| col |\n"
        "|---|\n"
        "| x |\n"
        "| y |\n"
    )
    # Two data rows < default threshold → one fragment, returned verbatim.
    assert split_table_into_row_groups(md, rows_per_group=200) == [md]


def test_split_table_handles_empty_table():
    assert split_table_into_row_groups("", rows_per_group=10) == []
    # Header-only (zero data rows) returns the table as-is.
    md_header_only = "| a |\n|---|\n"
    assert split_table_into_row_groups(md_header_only, rows_per_group=10) == [md_header_only]
