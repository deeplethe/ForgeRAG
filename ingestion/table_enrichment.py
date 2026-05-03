"""Table enrichment phase — generate LLM descriptions for spreadsheet
``BlockType.TABLE`` blocks emitted by ``SpreadsheetBackend``.

Mirrors ``parser.image_enrichment`` in shape:

  * Walks the parsed document
  * For each TABLE block, runs a single LLM call (small tables) or
    a map-reduce summarisation (large tables) to write a description
    into ``block.text``
  * Falls back gracefully on LLM failure (block.text keeps the
    deterministic metadata fallback set by the backend)

Why this lives in ``ingestion/`` rather than ``parser/`` (where
image_enrichment is): table enrichment uses the existing
``graph.summarize`` infrastructure (map-reduce + recursive reduce
for entity descriptions) which depends on the LLM cache + LLM
config layer that's an ingestion-level concern. Image enrichment
predates this and built its own VLM call wrapper — historical
inconsistency.

Trigger condition (single-pass vs map-reduce):
  * Estimate the markdown's token count via ``parser.chunker.approx_tokens``
  * If estimate <= ``cfg.context_size``: single-pass LLM call with
    the whole markdown as the only fragment
  * Otherwise: split the markdown into row-group fragments via
    ``SpreadsheetBackend.split_table_into_row_groups`` and feed to
    ``graph.summarize.summarize_descriptions(kind="table",
    fragments=..., cfg=SummarizeConfig(...))``. The map-reduce
    machinery handles the rest.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from config.tables import TableEnrichmentConfig
from graph.summarize import SummarizeConfig, summarize_descriptions
from parser.backends.spreadsheet import split_table_into_row_groups
from parser.chunker import approx_tokens
from parser.schema import Block, BlockType, DocFormat, ParsedDocument

log = logging.getLogger(__name__)


def _resolve_api_key(cfg: TableEnrichmentConfig) -> str | None:
    """Inline ``api_key`` wins; else look up ``api_key_env``; else None."""
    if cfg.api_key:
        return cfg.api_key
    if cfg.api_key_env:
        import os

        return os.environ.get(cfg.api_key_env)
    return None


def _build_summarize_cfg(cfg: TableEnrichmentConfig) -> SummarizeConfig:
    """Translate ``TableEnrichmentConfig`` → ``SummarizeConfig``.

    The map-reduce loop in ``graph.summarize.summarize_descriptions``
    works against ``SummarizeConfig``; this is a thin adapter so the
    table-enrichment path doesn't need to know the internals.
    """
    return SummarizeConfig(
        enabled=True,
        model=cfg.model,
        api_key=_resolve_api_key(cfg),
        api_base=cfg.api_base,
        timeout=cfg.timeout,
        max_output_tokens=cfg.summary_max_tokens,
        context_size=cfg.context_size,
        max_iterations=cfg.max_iterations,
        # ``trigger_tokens`` and ``force_on_count`` gates only matter
        # when we recurse from a map step that produced lots of
        # partial summaries — we want the reduce path to keep going
        # until everything fits in one call. Defaults are fine here;
        # they bias toward summarising rather than passing through.
    )


def _describe_one_table(
    block: Block,
    sheet_name: str,
    cfg: TableEnrichmentConfig,
) -> str | None:
    """Generate the description for one TABLE block.

    Returns the LLM-generated description, or ``None`` on failure.
    Caller is responsible for deciding whether to keep the fallback
    (block.text was already set by the backend to a deterministic
    metadata description).
    """
    md = block.table_markdown or block.text or ""
    if not md.strip():
        log.warning("table_enrichment: block %s has no markdown", block.block_id)
        return None

    sum_cfg = _build_summarize_cfg(cfg)
    estimated = approx_tokens(md)

    # Pick prompt by size:
    #   * Small (markdown fits well within the description budget)
    #     → "concrete" prompt: LLM is told to quote actual cell values
    #       inline, so the description for a 5-row table reads like
    #       "EMEA Q1 revenue was $1,200; NA was $3,400; ...". User can
    #       then answer "what was EMEA Q1?" straight from the chunk
    #       content without a follow-up agent round-trip.
    #   * Large (above threshold) → "abstract" prompt: LLM stays
    #     summary-style and refrains from row-dumps, which would just
    #     get truncated by the output budget anyway.
    # The block.table_markdown is preserved on the side for the viewer
    # / future agent regardless of which path runs — citations always
    # link back to the full data.
    use_small_prompt = estimated <= cfg.concrete_summary_max_tokens
    prompt_kind = "table_small" if use_small_prompt else "table"

    try:
        if estimated <= cfg.context_size:
            # Single-pass call. Wrap the whole markdown as one
            # "fragment" and let summarize_descriptions handle it
            # (single-pass branch since len(fragments) == 1).
            return summarize_descriptions(
                name=sheet_name,
                kind=prompt_kind,
                fragments=[md],
                cfg=sum_cfg,
            )

        # Doesn't fit one window — split into row-groups and let the
        # map-reduce loop in summarize_descriptions take over. Always
        # the abstract prompt here: by definition we exceeded
        # concrete_summary_max_tokens (which is ≤ context_size).
        fragments = split_table_into_row_groups(md, rows_per_group=cfg.rows_per_group)
        log.info(
            "table_enrichment: %s ~%d tokens > context %d → map-reduce on %d fragments",
            sheet_name,
            estimated,
            cfg.context_size,
            len(fragments),
        )
        return summarize_descriptions(
            name=sheet_name,
            kind="table",
            fragments=fragments,
            cfg=sum_cfg,
        )
    except Exception as exc:
        log.warning(
            "table_enrichment failed for sheet %r (%d tokens): %s",
            sheet_name,
            estimated,
            exc,
        )
        return None


def enrich_tables(parsed: ParsedDocument, cfg: TableEnrichmentConfig) -> int:
    """Walk the parsed doc and fill description text on every TABLE block.

    Returns the number of blocks successfully enriched. Failures
    leave the block's deterministic metadata fallback in place
    (``block.text`` is already populated by ``SpreadsheetBackend``).

    Concurrent: each TABLE block is independent, so we fan out via
    a ThreadPoolExecutor capped at ``cfg.max_workers``. Most
    spreadsheets have ≤ 5 sheets so this is rarely the bottleneck.
    """
    if not cfg.enabled:
        log.debug("table_enrichment disabled — using deterministic fallback descriptions")
        return 0

    # Spreadsheet-only gate. PDF / DOCX backends (mineru) ALSO emit
    # ``BlockType.TABLE`` for native PDF tables, but their architecture
    # is different: ``block.text`` is a flat HTML→text view and
    # ``block.table_markdown`` is the rendered table; the chunker
    # picks ``table_markdown`` for PDFs, so any description we'd
    # write into ``block.text`` is silently discarded by the chunker.
    # Running the LLM on PDF tables would waste calls + tokens with
    # zero retrieval benefit. Restrict to SPREADSHEET docs where the
    # description-only architecture actually consumes block.text.
    if parsed.format != DocFormat.SPREADSHEET:
        log.debug(
            "table_enrichment: skipping (format=%s, only SPREADSHEET is enriched)",
            parsed.format,
        )
        return 0

    # Build a quick page_no → page_name index so we can pass the
    # human-readable sheet name to the LLM (the block stores the
    # numeric page_no, but the prompt wants the sheet's display name).
    page_names: dict[int, str] = {}
    for page in parsed.pages:
        if page.name:
            page_names[page.page_no] = page.name

    targets: list[tuple[Block, str]] = []
    for block in parsed.blocks:
        if block.type != BlockType.TABLE:
            continue
        sheet_name = page_names.get(block.page_no, f"Sheet {block.page_no}")
        targets.append((block, sheet_name))

    if not targets:
        return 0

    log.info("table_enrichment: %d TABLE blocks to describe", len(targets))

    enriched_count = 0
    with ThreadPoolExecutor(max_workers=max(1, cfg.max_workers)) as pool:
        futures: dict[Any, Block] = {
            pool.submit(_describe_one_table, block, sheet_name, cfg): block
            for block, sheet_name in targets
        }
        for fut in as_completed(futures):
            block = futures[fut]
            try:
                description = fut.result()
            except Exception as exc:
                log.warning(
                    "table_enrichment: future raised for block %s: %s",
                    block.block_id,
                    exc,
                )
                continue
            if description and description.strip():
                # Prepend the sheet heading so the description starts
                # with a recognisable anchor for retrieval. The LLM
                # was told to mention the table name explicitly, but
                # this guards against models that occasionally elide
                # the lead-in.
                sheet_name = next(
                    (n for b, n in targets if b is block), f"Sheet {block.page_no}"
                )
                heading = f"## Sheet: {sheet_name}"
                # Don't double-up the heading if the LLM already put
                # one at the start.
                stripped = description.strip()
                if stripped.lower().startswith(heading.lower()):
                    block.text = stripped
                else:
                    block.text = f"{heading}\n\n{stripped}"
                enriched_count += 1

    log.info(
        "table_enrichment done: %d/%d sheets enriched",
        enriched_count,
        len(targets),
    )
    return enriched_count
