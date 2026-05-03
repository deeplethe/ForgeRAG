"""Table (spreadsheet) enrichment configuration.

Mirrors ``config/images.py`` — the design pattern for a "format
that needs an LLM to be retrievable" is the same:

  1. ``enabled`` switch + LLM credentials
  2. ``is_spreadsheet_upload_configured(cfg)`` predicate the upload
     route reads to decide whether to accept incoming uploads
  3. Frontend reads the same flag from ``/health.features`` to
     pre-flight the UI

The table_enrichment phase walks ``BlockType.TABLE`` blocks emitted
by ``parser.backends.spreadsheet.SpreadsheetBackend`` and writes a
descriptive summary into ``block.text``. That summary is what the
embedder + BM25 + KG path see — the actual table data stays on
``block.table_markdown`` for future use (agent layer, viewer
rendering) and is **not** part of the retrieval index.

For tables small enough to fit in the LLM context, the description
is generated in a single call. Larger tables are handled by
delegating to ``graph.summarize.summarize_descriptions(kind="table",
fragments=[row_groups], cfg=...)`` which already implements the
map-reduce + recursive reduce pattern (originally for entity
description compaction).
"""

from __future__ import annotations

import os

from pydantic import BaseModel


class TableEnrichmentConfig(BaseModel):
    """Knobs for the LLM-driven table description phase.

    Defaults: disabled. Spreadsheet uploads are rejected with HTTP
    415 unless this is turned on AND a model is set AND the
    credentials resolve. Same hard-gate model as image uploads —
    silently storing un-retrievable docs is worse UX than refusing
    upfront.
    """

    enabled: bool = False
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    timeout: float = 60.0

    # Concurrent enrichment calls. Each TABLE block is independent
    # so we fan out — most spreadsheets have < 10 sheets so this is
    # rarely the bottleneck, but it matches image_enrichment shape.
    max_workers: int = 4

    # Map-reduce knobs for tables that don't fit one LLM call.
    # ``rows_per_group`` is the row-batch size when splitting a big
    # sheet into fragments to feed
    # ``graph.summarize.summarize_descriptions``.
    rows_per_group: int = 200
    # Hard ceiling on the final summary length (tokens, soft cap
    # passed to the LLM prompt).
    summary_max_tokens: int = 600
    # Per-call input budget for the map-reduce loop. Tables with
    # markdown bigger than this will trigger map-reduce.
    context_size: int = 12000
    # Convergence guard — same default as SummarizeConfig.
    max_iterations: int = 5

    # Verbatim-data threshold (tokens). For tables whose rendered
    # markdown is small enough, we append the full markdown after
    # the LLM description so the chunk content carries BOTH a
    # narrative summary AND every cell value verbatim. Two wins:
    #   * BM25 / vector get lexical signal on cell values (e.g.
    #     "EMEA Q1 1200" matches the literal row), not just the
    #     abstract description.
    #   * Answer LLM sees actual numbers in context — no need to
    #     defer to a future agent tool for tiny lookup tables.
    # Above this threshold we fall back to description-only to keep
    # chunk content under the embedder context window (8K-32K for
    # most providers; description ~600 tok + verbatim 2000 tok =
    # 2.6K total fits everywhere).
    verbatim_max_tokens: int = 2000


# Extensions accepted as spreadsheet-as-document uploads. Mirrored
# on the frontend (``capabilities.classify``); we expose this via
# ``/health.features.spreadsheet_extensions`` so the two stay in
# sync without hardcoding on both sides.
SPREADSHEET_EXTENSIONS: tuple[str, ...] = (".xlsx", ".csv", ".tsv")

# Hard cell-count limit. Above this we refuse the upload at the
# /files route — RAG-style retrieval doesn't add value beyond this
# scale (one giant doc dominates BM25/vector + the embed cost is
# significant + the analytical query path is the wrong fit). 5M
# cells ≈ 100K rows × 50 cols, which is already the OLAP scale.
SPREADSHEET_MAX_CELLS: int = 5_000_000

# Soft warning threshold. We log and surface a warning toast on
# the frontend but still ingest. Tunable.
SPREADSHEET_WARN_CELLS: int = 500_000


def is_spreadsheet_upload_configured(cfg: TableEnrichmentConfig) -> bool:
    """``True`` iff the deployment can actually ingest spreadsheet uploads.

    Three conditions, all required (matches the image-upload check
    in ``config/images.py:is_image_upload_configured``):

      1. ``enabled`` switch is on
      2. A model name is set
      3. Credentials are reachable — inline ``api_key`` set, OR
         ``api_key_env`` resolves to a non-empty environment
         variable. Neither set → assume local provider (Ollama
         etc.) and let the call fail later if unreachable. We
         bias toward "claim configured" here so a working local
         setup isn't refused.
    """
    if not cfg.enabled:
        return False
    if not cfg.model:
        return False
    if cfg.api_key_env:
        return bool(os.environ.get(cfg.api_key_env))
    return True
