"""Unit tests for ``ingestion.table_enrichment``.

The table_enrichment phase has three contracts:

  1. When disabled, returns 0 and leaves block.text untouched (the
     SpreadsheetBackend's deterministic-metadata fallback survives).
  2. When enabled, every TABLE block gets its block.text overwritten
     with the LLM-generated description (prepended with a
     ``## Sheet: <name>`` heading for retrieval anchoring).
  3. Routes through ``graph.summarize.summarize_descriptions`` so the
     map-reduce + recursive-reduce machinery is reused (no duplicate
     LLM-orchestration logic).

LLM round-trips are stubbed via patching ``summarize_descriptions``
so the test doesn't need network or model credentials.
"""

from __future__ import annotations

from unittest.mock import patch

from config.tables import TableEnrichmentConfig
from ingestion.table_enrichment import enrich_tables
from parser.schema import Block, BlockType, DocFormat, Page, ParsedDocument, ParseTrace


def _make_parsed_with_two_tables() -> ParsedDocument:
    """Build a 2-sheet ParsedDocument that mimics what
    SpreadsheetBackend emits."""
    blocks = [
        Block(
            block_id="d:1:1:0",
            doc_id="d",
            parse_version=1,
            page_no=1,
            seq=0,
            bbox=(0.0, 0.0, 0.0, 0.0),
            type=BlockType.TABLE,
            text="## Sheet: Sales\n\nColumns (3): region, quarter, revenue\nRow count: 4\nSource: ...",
            table_markdown="| region | quarter | revenue |\n|---|---|---|\n| EMEA | Q1 | 1200 |\n",
            table_html=None,
        ),
        Block(
            block_id="d:1:2:0",
            doc_id="d",
            parse_version=1,
            page_no=2,
            seq=0,
            bbox=(0.0, 0.0, 0.0, 0.0),
            type=BlockType.TABLE,
            text="## Sheet: Forecast\n\nColumns (2): year, revenue\nRow count: 3\nSource: ...",
            table_markdown="| year | revenue |\n|---|---|\n| 2025 | 5000 |\n",
            table_html=None,
        ),
    ]
    pages = [
        Page(page_no=1, width=0.0, height=0.0, name="Sales",    block_ids=["d:1:1:0"]),
        Page(page_no=2, width=0.0, height=0.0, name="Forecast", block_ids=["d:1:2:0"]),
    ]
    from parser.schema import DocProfile

    return ParsedDocument(
        doc_id="d",
        filename="report.xlsx",
        format=DocFormat.SPREADSHEET,
        parse_version=1,
        profile=DocProfile(format=DocFormat.SPREADSHEET, page_count=2, file_size_bytes=1234),
        parse_trace=ParseTrace(),
        pages=pages,
        blocks=blocks,
        toc=None,
    )


def test_enrichment_skips_non_spreadsheet_format():
    """Mineru's PDF backend ALSO emits BlockType.TABLE for native PDF
    tables, but their architecture is different: chunker takes
    block.table_markdown for PDFs, so any description we'd write into
    block.text would be silently discarded. Enrichment must skip
    non-SPREADSHEET docs to avoid burning LLM tokens for nothing.

    Regression: previously the gate was only ``cfg.enabled`` — a deploy
    with table_enrichment turned on for spreadsheets would also pay
    the LLM bill for every PDF table block.
    """
    parsed = _make_parsed_with_two_tables()
    parsed.format = DocFormat.PDF  # simulate a PDF doc with TABLE blocks
    parsed.profile.format = DocFormat.PDF
    before = [b.text for b in parsed.blocks]

    cfg = TableEnrichmentConfig(enabled=True, model="stub", api_key="test")
    # No patch — if the gate is broken, summarize_descriptions would
    # try a real LLM call and the test would crash on missing creds.
    n = enrich_tables(parsed, cfg)

    assert n == 0
    # Block text untouched.
    assert [b.text for b in parsed.blocks] == before


def test_enrichment_disabled_is_a_noop():
    parsed = _make_parsed_with_two_tables()
    before = [b.text for b in parsed.blocks]

    cfg = TableEnrichmentConfig(enabled=False)
    n = enrich_tables(parsed, cfg)

    assert n == 0
    # Deterministic fallback text survives untouched.
    assert [b.text for b in parsed.blocks] == before


def test_enrichment_overwrites_block_text_for_each_sheet():
    parsed = _make_parsed_with_two_tables()
    cfg = TableEnrichmentConfig(enabled=True, model="stub", api_key="test")

    # Stub summarize_descriptions to return a deterministic per-sheet
    # description. The patched symbol matches the import in
    # ingestion/table_enrichment.py (from graph.summarize import ...).
    def fake_summarize(*, name, kind, fragments, cfg):
        return f"This sheet shows {name} data with {len(fragments)} fragment(s)."

    with patch("ingestion.table_enrichment.summarize_descriptions", side_effect=fake_summarize):
        n = enrich_tables(parsed, cfg)

    assert n == 2
    # Block text now starts with the heading anchor and contains the
    # LLM description body. Order isn't guaranteed (ThreadPoolExecutor),
    # so look up by block_id.
    by_id = {b.block_id: b for b in parsed.blocks}
    assert by_id["d:1:1:0"].text.startswith("## Sheet: Sales")
    assert "Sales data" in by_id["d:1:1:0"].text
    assert by_id["d:1:2:0"].text.startswith("## Sheet: Forecast")
    assert "Forecast data" in by_id["d:1:2:0"].text


def test_enrichment_uses_sheet_name_in_summarize_call():
    """The sheet name is the retrieval anchor — table_enrichment must
    pass it to summarize_descriptions so it lands in the LLM prompt
    (and the cache key)."""
    parsed = _make_parsed_with_two_tables()
    cfg = TableEnrichmentConfig(enabled=True, model="stub", api_key="test")

    captured: list[str] = []

    def fake_summarize(*, name, kind, fragments, cfg):
        captured.append(name)
        assert kind == "table"  # always "table" for this code path
        return f"desc-of-{name}"

    with patch("ingestion.table_enrichment.summarize_descriptions", side_effect=fake_summarize):
        enrich_tables(parsed, cfg)

    assert sorted(captured) == ["Forecast", "Sales"]


def test_enrichment_falls_back_on_llm_failure():
    """If summarize_descriptions raises, the deterministic-metadata
    fallback must remain in block.text — the doc stays retrievable
    via the fallback even though the description is degraded."""
    parsed = _make_parsed_with_two_tables()
    original_texts = {b.block_id: b.text for b in parsed.blocks}
    cfg = TableEnrichmentConfig(enabled=True, model="stub", api_key="test")

    def boom(*, name, kind, fragments, cfg):
        raise RuntimeError("simulated LLM 503")

    with patch("ingestion.table_enrichment.summarize_descriptions", side_effect=boom):
        n = enrich_tables(parsed, cfg)

    # Zero enriched, but no exception escapes the phase.
    assert n == 0
    # Original deterministic descriptions are still in place.
    for b in parsed.blocks:
        assert b.text == original_texts[b.block_id]


def test_collect_table_chunks_skips_non_spreadsheet_docs():
    """_collect_table_chunks (in IngestionPipeline) is the gate that
    decides which TABLE chunks bypass LLM-driven KG extraction and get
    a deterministic ``entity_type=TABLE`` entity injected instead.

    PDF tables (mineru emits BlockType.TABLE for them) MUST stay on
    the standard LLM-extraction path — their chunk content is the
    rendered markdown, which is the right input for entity / relation
    extraction. Skipping PDFs here would silently lose every entity
    found inside a PDF table.

    Regression test: previously the gate only checked block.type ==
    table; any TABLE block would be routed to the placeholder
    injection. This pin enforces the format gate.
    """
    from unittest.mock import MagicMock

    from ingestion import IngestionPipeline

    pipe = IngestionPipeline(
        file_store=MagicMock(),
        parser=MagicMock(),
        tree_builder=MagicMock(),
        chunker=MagicMock(),
        relational_store=MagicMock(),
    )

    # Non-spreadsheet doc with a TABLE block — gate should bail out
    # BEFORE touching the relational store, so we can assert no DB
    # calls were made (proves the early return, not just an empty result).
    pdf_doc_row = {"format": "pdf", "active_parse_version": 1}
    result = pipe._collect_table_chunks(
        doc_id="doc_pdf",
        parse_version=1,
        doc_row=pdf_doc_row,
    )
    assert result == {}
    pipe.rel.get_blocks.assert_not_called()
    pipe.rel.find_chunk_by_block_id.assert_not_called()


def test_enrichment_skips_non_table_blocks():
    """Mixed-format docs (theoretical: a future PDF that emits one
    BlockType.TABLE among many TEXT blocks) should only enrich the
    TABLE ones."""
    parsed = _make_parsed_with_two_tables()
    # Inject a TEXT block that should be ignored.
    parsed.blocks.append(
        Block(
            block_id="d:1:1:1",
            doc_id="d",
            parse_version=1,
            page_no=1,
            seq=1,
            bbox=(0.0, 0.0, 1.0, 1.0),
            type=BlockType.PARAGRAPH,
            text="A footnote that mentions revenue but is not a table.",
        )
    )

    cfg = TableEnrichmentConfig(enabled=True, model="stub", api_key="test")
    with patch(
        "ingestion.table_enrichment.summarize_descriptions",
        side_effect=lambda **kw: "stubbed",
    ):
        n = enrich_tables(parsed, cfg)

    # Two TABLE blocks → two enrichments. TEXT block untouched.
    assert n == 2
    text_block = next(b for b in parsed.blocks if b.type == BlockType.PARAGRAPH)
    assert "footnote" in text_block.text
