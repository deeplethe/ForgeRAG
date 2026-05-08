# Roadmap: Spreadsheet-as-Document Support

**Status:** in progress on `feat/spreadsheet-as-document`
**Last updated:** 2026-05-04

This document captures the design decisions for native spreadsheet (`.xlsx` / `.csv` / `.tsv`) support and the implementation plan. It is self-contained on purpose — readable without the prior design discussion — so context-window compression can't lose the key calls.

---

## TL;DR

A spreadsheet upload becomes a native first-class `Document` (parallel to image-as-document) with one `TABLE` block per sheet. **Only the description is embedded; the actual data is preserved but not chunked into the retrieval index.** This means:

* 1 spreadsheet → N `TABLE` blocks (N = sheet count) → N chunks → N embeddings.
* Each chunk's text is a **rich description** of the sheet (LLM-generated when `table_enrichment.enabled = true`, falling back to deterministic metadata when disabled).
* The full table markdown is stored on `block.table_markdown` and the original file lives in BlobStore — accessible to a future agent / data-analysis tool, but not seen by the embedder or generator.
* Citations point to "this sheet"; viewer opens the right tab. No cell-range highlight (deferred until an agent layer needs it).
* Cell-count hard limit at 5M; soft warning at 500K.
* No LibreOffice dependency. No row-level chunking. No row-level KG entities.

---

## Architectural decisions (with rejected alternatives)

### 1. xlsx parsing — `openpyxl` direct read (no PDF round-trip)

**Chosen:** `openpyxl.load_workbook(read_only=True, data_only=True)`. Streams rows iteratively, bounded memory.

**Rejected:** LibreOffice → PDF → MinerU (RAG-Anything's path). Reasons:
* 150MB+ runtime dependency.
* 60s subprocess per upload.
* Lossy round-trip (cell types, formulas, sheet names get squashed into rendered PDF).
* MinerU's table-from-PDF extraction is itself approximate; we have the structured source already.

**Rejected:** pandas. 50MB+ dep weight (numpy etc.) for what `openpyxl` does directly.

### 2. Chunking granularity — 1 sheet = 1 chunk

**Chosen:** description-only chunking. The chunk content is metadata + LLM summary; the full table body is stored separately on `block.table_markdown` for future retrieval by agents.

**Rejected:** RAG-Anything's "whole table body in chunk content". Their `modal_chunk` includes the full markdown body, which:
* blows up the embedder's context limit on large tables (silent failure),
* dilutes the embedding signal (data dominates over the description),
* couples retrieval unit to context unit unnecessarily.

**Rejected:** row-group chunking (D from earlier discussion). Adds complexity (sibling-pin in merge, header/row-group split) and pollutes BM25/vector with thousands of similar chunks per upload. The "find a specific row by value" problem it solves is properly an agent-layer concern (text-to-SQL / pandas), not a RAG-layer concern.

**Rejected:** description-only with sample rows embedded. The added value is small (a few rows can't represent 50K rows statistically) and it introduces a weird half-state ("sometimes rows are there, sometimes not").

**Implication:** value-level queries fail by design. "What's TSLA's Q3 revenue from that sheet?" → generator answers "I see the table exists; please open the data viewer or use a data-analysis agent". Agent layer (future) calls a `query_spreadsheet(doc_id, sheet_name, query)` tool that loads `block.table_markdown` and runs structured extraction.

### 3. Description generation — table_enrichment phase, hard-required

**Chosen:** mirror `image_enrichment`. Adds `config/tables.py` with `TableEnrichmentConfig(enabled, model, ...)`. When `enabled=true`, every TABLE block goes through an LLM pass that writes `block.text` (the description) using:

* Sheet name + columns
* Row count + cell count
* LLM-generated summary

Falls back to a deterministic metadata-only description when `enabled=false`, **but uploads of spreadsheets are rejected (415) when enrichment is off** — same hard gate as images-without-VLM. Otherwise we'd silently store unsearchable docs.

**Rejected:** soft fallback (heuristic description when no LLM). Same reasoning as images: silently-stored-but-unfindable docs are worse UX than an upfront refusal with a clear message.

### 4. Large-table description — map-reduce via existing `graph/summarize.py`

**Chosen:** for tables that fit in the LLM context (~30K tokens of markdown ≈ 3000 rows × 30 cols), single-pass LLM call generates the description. For tables that don't fit, split rows into row-groups and feed to `graph.summarize.summarize_descriptions(kind="table", fragments=row_groups, cfg=...)`. The existing map-reduce + recursive reduce in `graph/summarize.py` (originally for entity description compaction) handles it without modification — only addition is a `kind="table"` branch in the prompt template.

LLM cost for a 50K-row table ≈ $0.05 with gpt-4o-mini (~250 row-group summarizations + a few reduce passes).

**Rejected:** truncation (first N rows only). Misses tail patterns (time-series end, distribution tails).

**Rejected:** sampling (first/last/random rows). Better than truncation but still misses clusters; map-reduce gives full coverage at trivial extra cost when we already have the infrastructure.

### 5. Multi-sheet workbooks — one Document, sheets distinguished by page_no

**Chosen:** an xlsx with N sheets becomes 1 Document with N pages. `page_no = sheet_index + 1`. Sheet name preserved in two places:

* `Page.name: str | None` — new optional field, structured access for the frontend tab strip.
* Injected into `block.text` as `## Sheet: {name}` so retrieval naturally sees it (BM25 / vector search can find sheets by name; KG entity description includes it).

**Rejected:** N separate Documents (one per sheet). Wrong mental model: user uploaded one file. Recycle bin / scope filter / KG path would all have to special-case.

**Rejected:** new `Block.sheet_name` field. Larger schema impact than `Page.name`; 99% of blocks (PDFs etc.) wouldn't use it.

### 6. CSV encoding — `charset-normalizer` auto-detect

**Chosen:** `charset_normalizer.from_path()` on upload. Picks best-guess encoding. Fallback is utf-8.

**Rejected:** force utf-8. Breaks every Chinese CSV exported from Excel (commonly GBK).

**Rejected:** require user to declare encoding at upload. UX friction.

### 7. Frontend viewer — marked.js render, simple

**Chosen:** render `block.table_markdown` via the already-imported `marked` library. Multi-sheet: tab strip across the top. Big tables: cap rendering to first 200 rows + "showing 200 of X rows; download original to see all". No cell range highlight (no cell_ref to highlight).

**Rejected:** RevoGrid integration. Was on the table when we thought we needed cell range selection; description-only retrieval drops that need entirely. RevoGrid stays a future option if/when an agent layer wants to highlight specific rows.

### 8. Bbox — sentinel zero `(0,0,0,0)`

**Chosen:** same as image-as-document. Schema unchanged. Citation highlight degenerates to a zero-area rectangle (invisible). Frontend SpreadsheetViewer doesn't draw bboxes anyway, so sentinel is irrelevant in practice.

**Rejected:** `Block.cell_ref: str | None` field. Was on the table for cell-range citation; description-only design makes citations point to the whole sheet, so cell_ref has nowhere to point.

**Rejected:** nullable bbox. Schema-wide refactor, unjustified.

### 9. Citation behavior — open viewer at the right sheet tab

**Chosen:** `[c_N]` resolves chunk → block → `(doc_id, page_no)` → frontend opens DocDetail → SpreadsheetViewer mounts → tab switches to `pages[page_no - 1]`. Same code shape as image-as-document; just a different viewer component.

**Rejected:** scroll-to-row, range highlight. No granularity to point to under description-only.

### 10. KG integration — table = 1 entity (`entity_type="TABLE"`)

**Chosen:** during KG extraction, if the source block is `BlockType.TABLE`, skip the standard chunk-LLM-extract path and instead inject **one** entity with `entity_type="TABLE"`, `name=Page.name`, `description=block.text`. No relations extracted from row data.

**Rejected:** running KG extraction on the description chunk like any other chunk. Would generate weird "entities" like the column names ("Region", "Revenue") which are schema, not concepts.

**Rejected:** column-level entities. Most column names are not concepts (Region, Revenue, Date) and create graph noise.

### 11. Cell-count hard limit — 5M cells

**Chosen:** at the upload route, after charset detection / before storing the blob, count `sum(rows × cols)` across all sheets via `openpyxl(read_only=True)`. If > 5M → 415 reject with `"Spreadsheet too large for retrieval-style RAG (~5M cells max). For analytical queries on this scale, use SQL / Polars / DuckDB."`. Soft warning (log only) above 500K.

**Rejected:** RAG-Anything's "no limit" approach. Their pipeline silently fails on huge tables (LibreOffice subprocess timeout, embedder context overflow). We refuse upfront with a clear message.

**Rejected:** row-count limit. Cell count is a better proxy for actual processing cost (a 30-col × 200K-row table and a 200-col × 30K-row table cost the same; both have 6M cells).

---

## Concrete implementation plan

### Files to add

| Path | Purpose |
|---|---|
| `parser/backends/spreadsheet.py` | New `SpreadsheetBackend` (parses xlsx/csv/tsv → ParsedDocument with TABLE blocks) |
| `config/tables.py` | New `TableEnrichmentConfig`, `SPREADSHEET_EXTENSIONS`, `is_spreadsheet_upload_configured()` |
| `ingestion/table_enrichment.py` | New phase that walks TABLE blocks and fills `block.text` via single-pass or map-reduce LLM call |
| `web/src/components/SpreadsheetViewer.vue` | New viewer (marked.js + tab strip + 200-row cap) |
| `tests/test_spreadsheet_backend.py` | Unit tests for the new backend (csv, xlsx single-sheet, xlsx multi-sheet, oversize reject) |
| `tests/test_table_enrichment.py` | Unit tests for the enrichment phase (fits-in-context, map-reduce path, llm-fail fallback) |

### Files to modify

| Path | Change |
|---|---|
| `parser/schema.py` | Add `Page.name: str \| None = None` (optional, defaults None for non-spreadsheet docs) |
| `parser/pipeline.py` | Add `DocFormat.SPREADSHEET` dispatch in `parse()` (parallel to `DocFormat.IMAGE` route) |
| `persistence/serde.py` | Round-trip `Page.name` in `_page_to_dict` / `_page_from_dict` |
| `ingestion/converter.py` | (Already done in prior PR) — `.xls` is already in `LEGACY_OFFICE_EXTENSIONS`; ensure `.xlsx` / `.csv` / `.tsv` are NOT in `CONVERTIBLE_EXTENSIONS` (they bypass conversion) |
| `ingestion/pipeline.py` | (a) Skip `_convert_to_pdf` for spreadsheet extensions. (b) Wire `_table_enrichment_phase` after parse / before chunk. (c) Inject TABLE entity into KG path (skipping standard chunk-based extraction for TABLE blocks). |
| `graph/summarize.py` | Extend `PROMPT_USER` template with a `kind="table"` branch (or accept a `kind`-based prompt selector); the map-reduce machinery itself is already generic. |
| `api/routes/files.py` | Reject spreadsheet uploads (415) when enrichment not configured, AND reject when cell-count > 5M. |
| `api/routes/health.py` | Add `features.spreadsheet_upload` (bool), `features.spreadsheet_extensions` (list), `features.spreadsheet_max_cells` (int). |
| `web/src/stores/capabilities.js` | Add `spreadsheetUpload`, `spreadsheetExtensions`, `spreadsheetMaxCells` to state and `classify(file)` (new `reason: 'spreadsheet_disabled'` and `reason: 'spreadsheet_too_large'`). |
| `web/src/views/Workspace.vue` | `safeEnqueue` / its toast handler covers the new reasons. |
| `web/src/views/DocDetail.vue` | Add `isSpreadsheet` computed; route to `<SpreadsheetViewer>` when true. |
| `requirements.txt` | Confirm `openpyxl` and `charset-normalizer` are present (charset-normalizer almost certainly pulled by requests; openpyxl is already in the converter path). |
| `README.md`, `README_CN.md`, `docs/getting-started.md` | Add spreadsheet to the multi-format highlight + drag-drop instructions. Note that uploads need `table_enrichment.enabled=true`. |
| `docs/configuration.md` | New section for `retrieval.kg_extraction.table_enrichment` (or wherever it lives) and for the cell-count threshold. |

### Schema delta

```python
# parser/schema.py — Page
@dataclass
class Page:
    page_no: int
    width: float
    height: float
    name: str | None = None          # NEW: sheet name for spreadsheets
    block_ids: list[str] = field(default_factory=list)
```

```python
# parser/schema.py — DocFormat
class DocFormat(str, Enum):
    PDF = "pdf"
    IMAGE = "image"
    SPREADSHEET = "spreadsheet"      # NEW
    # ...
```

```python
# parser/pipeline.py — _EXT_TO_FORMAT
_EXT_TO_FORMAT = {
    # ... existing ...
    ".xlsx": DocFormat.SPREADSHEET,  # NEW
    ".csv":  DocFormat.SPREADSHEET,  # NEW
    ".tsv":  DocFormat.SPREADSHEET,  # NEW
}
```

```python
# config/tables.py — new module
class TableEnrichmentConfig(BaseModel):
    enabled: bool = False
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    max_workers: int = 4
    # Map-reduce knobs (defaults reuse SummarizeConfig):
    rows_per_group: int = 200
    summary_max_tokens: int = 600
    context_size: int = 12000
    max_iterations: int = 5

SPREADSHEET_EXTENSIONS: tuple[str, ...] = (".xlsx", ".csv", ".tsv")
SPREADSHEET_MAX_CELLS: int = 5_000_000
SPREADSHEET_WARN_CELLS: int = 500_000

def is_spreadsheet_upload_configured(cfg: TableEnrichmentConfig) -> bool:
    """Same shape as is_image_upload_configured()."""
    if not cfg.enabled or not cfg.model:
        return False
    if cfg.api_key_env:
        return bool(os.environ.get(cfg.api_key_env))
    return True
```

### Block content — concrete shape

Block produced by `SpreadsheetBackend` for a single sheet:

```python
Block(
    block_id=f"{doc_id}:{parse_version}:{page_no}:0",
    doc_id=doc_id,
    parse_version=parse_version,
    page_no=sheet_index + 1,
    seq=0,
    bbox=(0.0, 0.0, 0.0, 0.0),         # sentinel
    type=BlockType.TABLE,
    text="",                           # filled by table_enrichment phase
    table_markdown=full_markdown,      # full data, preserved
    table_html=None,                   # not needed
)
```

After `table_enrichment` phase (single-pass, small table):

```python
block.text = """
[Sheet: Q3-Revenue]
Columns: Region, Company, Revenue, Profit (4 columns)
Row count: 47 rows
Source: sales_2024.xlsx, sheet 2 of 5
Summary: This table reports Q3 2024 revenue and profit across regions and tech companies, with quarterly comparisons highlighted in the rightmost column.
"""
```

### Pipeline phase order (with new phase)

```
upload → File row created
       → ingest()
         ├─ Phase 0: convert (skip for spreadsheet — handled by parser directly)
         ├─ Phase 1: parse (SpreadsheetBackend → ParsedDocument with TABLE blocks)
         ├─ Phase 1.5: image_enrichment (no-op for spreadsheet, no IMAGE blocks)
         ├─ Phase 1.6: table_enrichment (NEW — fills block.text via LLM)
         ├─ Phase 2: tree_build (trivial: 1 root + N leaves, one per sheet)
         ├─ Phase 3: chunker (1 chunk per TABLE block)
         ├─ Phase 4: embed (1 vector per chunk)
         ├─ Phase 5: KG extract — for TABLE blocks: inject 1 entity directly (skip chunk-LLM extract); for non-TABLE blocks: existing path
         └─ Phase 6: KG summarise (existing _summarize_phase, may not trigger if no relations)
```

### Capabilities (frontend)

```js
// web/src/stores/capabilities.js — classify() additions
const SPREADSHEET_EXTS = ['.xlsx', '.csv', '.tsv']

if (SPREADSHEET_EXTS.includes(ext)) {
  if (!this.spreadsheetUpload) {
    return { ok: false, reason: 'spreadsheet_disabled', ext }
  }
  if (file.size > this.spreadsheetMaxBytes) {
    return { ok: false, reason: 'spreadsheet_too_large', ext, size: file.size }
  }
  // Note: cell-count check only runs server-side (need to parse to count).
}
```

Workspace.vue toast strings:
* `spreadsheet_disabled` → "Enable table_enrichment in opencraig.yaml to upload spreadsheets."
* `spreadsheet_too_large` → "Spreadsheet too large for RAG. Use SQL / Polars / DuckDB for ~5M+ cell datasets."

---

## Test plan

### Unit tests (`tests/test_spreadsheet_backend.py`)

1. CSV parse — single sheet, utf-8 → 1 Page, 1 TABLE block, table_markdown round-trips.
2. CSV parse — GBK encoding (Chinese content) → charset_normalizer picks the right encoding, content not garbled.
3. xlsx parse — single sheet → 1 Page with `name="Sheet1"` (or whatever's in the file), 1 TABLE block.
4. xlsx parse — multi sheet → N Pages, N TABLE blocks, page_no monotonic.
5. xlsx parse — empty sheet → block has no rows, table_markdown is the header line + zero data rows.
6. Cell-count gate — > 5M cells raises `BackendUnavailable`.
7. Block.bbox is sentinel zero.
8. block.text is empty pre-enrichment (filled later).

### Unit tests (`tests/test_table_enrichment.py`)

1. Disabled config — block.text gets the deterministic fallback (metadata only, no LLM call).
2. Single-pass — small table: 1 LLM call, block.text contains the LLM summary.
3. Map-reduce path — large table: stubbed LLM, verify the map-reduce trigger condition fires and block.text ends up populated.
4. LLM failure — block.text falls back to deterministic metadata; ingest doesn't crash.

### Integration / smoke

* End-to-end ingest of a CSV via `IngestionPipeline` (test fixture).
* `/health` returns the new features payload.
* `/files` POST rejects 415 for spreadsheet without enrichment configured.
* `/files` POST rejects 415 for >5M cell file.

### Frontend

* SpreadsheetViewer renders a markdown table.
* Multi-sheet tab strip switches active sheet.
* Big table caps at 200 rows + shows the "showing 200 of X" message.

---

## Edge cases

| Case | Behavior |
|---|---|
| Empty xlsx (0 sheets) | Reject 422 at upload — "spreadsheet has no sheets". |
| Empty sheet | TABLE block created with header-only markdown; description says "0 data rows". |
| Sheet name with special chars (`/`, `:`, `\`) | Stored verbatim in `Page.name`; markdown-injected sheet-header escapes them. |
| CSV with no header row | Detected via heuristic (first row looks like data, not headers) → use `col_1, col_2, ...` synthetic headers, surface in description. |
| xlsx with charts / images | Ignored for v1. Charts on the sheet are not extracted; future work. |
| Formulas | Read computed values via `data_only=True`. If user saved without recalc, formulas appear as `None` — fall back to formula text or `""`. |
| TSV | Same path as CSV but with tab delimiter. charset_normalizer + standard `csv.reader(dialect="excel-tab")`. |
| BlobStore key for original file | Already handled — file stored on upload; no spreadsheet-specific path needed. Block.table_markdown is in-row in the SQL store. |
| Very wide table (1000+ columns) | Cell-count check catches this if it crosses 5M total. Otherwise pass through; description LLM may struggle but won't crash. |

---

## Future hooks (not in scope, but unblocked by this design)

1. **Agent integration:** a `query_spreadsheet(doc_id, sheet, question)` tool can fetch `block.table_markdown` (or download the original from BlobStore) and run pandas/duckdb on it.
2. **Cell-range citations:** if/when value-level citations are needed, add `Block.cell_ref` then; the frontend SpreadsheetViewer can be upgraded to RevoGrid for `setRange()` API.
3. **Row-group chunking:** could be added later for tables that warrant fine retrieval (e.g. opting in via config). Doesn't conflict with the current design — TABLE blocks would just produce extra child chunks.
4. **Charts / sparklines extraction:** xlsx with embedded charts. Out of scope for v1.

---

## Estimated effort

| Phase | Time |
|---|---|
| `SpreadsheetBackend` (parse + markdown render + cell count) | 2.5h |
| `Page.name` + serde round-trip | 30min |
| `config/tables.py` + `is_spreadsheet_upload_configured()` | 30min |
| `ingestion/table_enrichment.py` (single-pass + map-reduce delegation) | 1.5h |
| `graph/summarize.py` `kind="table"` branch | 30min |
| Pipeline wiring + KG entity injection for TABLE blocks | 1h |
| `api/routes/files.py` 415 gates + cell-count pre-check | 30min |
| `api/routes/health.py` features additions | 15min |
| Frontend capabilities store + Workspace toasts | 30min |
| `SpreadsheetViewer.vue` + DocDetail dispatch | 1h |
| Tests (parser + enrichment + integration) | 2h |
| Docs (README + getting-started + configuration) | 30min |

**Total: ~10h**

---

## Acceptance checklist

* [ ] Upload `.xlsx` (multi-sheet) → ingested, viewer shows tabs, each tab renders correct markdown.
* [ ] Upload `.csv` (utf-8 + GBK) → ingested, no encoding issues.
* [ ] Upload spreadsheet with `table_enrichment.enabled=false` → 415 with clear message.
* [ ] Upload `.xlsx` with > 5M cells → 415 with clear message.
* [ ] Question retrieves the right sheet by topic ("营收数据表" finds the Q3-Revenue sheet).
* [ ] Citation `[c_N]` opens the right sheet tab.
* [ ] Frontend toast fires immediately for spreadsheet-disabled / spreadsheet-too-large cases.
* [ ] Generator answer mentions "I see the table exists; for specific values open the viewer or use a data-analysis tool" when asked about cell-level data.
* [ ] `block.table_markdown` and original blob are still accessible via the API for future agent layer.
* [ ] 296+ tests pass; ruff clean; frontend builds.
