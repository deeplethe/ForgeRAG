"""
Parser-layer configuration.

Single explicit ``parser.backend`` choice — no probe-driven tier
fallback chain. PyMuPDF is fast/baseline, MinerU pipeline is
layout-aware, MinerU VLM is the heaviest (vision model, best for
scanned/handwritten/very-complex layouts).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Backend sub-configs
# ---------------------------------------------------------------------------


class PyMuPDFConfig(BaseModel):
    """No knobs needed beyond the implicit selection at parser.backend."""


class MinerUConfig(BaseModel):
    """Settings shared by MinerU's pipeline + VLM modes.

    The ``parser.backend`` top-level choice picks ``pipeline`` (fast,
    layout-only) vs ``mineru-vlm`` (vision model). When VLM is selected,
    set ``server_url`` to use a remote inference server (``vlm-http-client``);
    otherwise the local ``vlm-auto-engine`` is used.
    """

    parse_method: Literal["auto", "txt", "ocr"] = "auto"
    device: Literal["cuda", "cpu"] = "cuda"
    # Primary OCR language (single lang per doc).
    lang: str = "ch"
    # Enable formula / table parsing.
    formula_enable: bool = True
    table_enable: bool = True
    # When set together with parser.backend=mineru-vlm, MinerU runs in
    # ``vlm-http-client`` mode pointing at this URL. Otherwise (vlm with
    # no server_url) MinerU runs ``vlm-auto-engine`` locally.
    server_url: str | None = None
    # Internal MinerU sub-backend identifier. NOT user-set in yaml — the
    # pipeline derives it from ``parser.backend`` + ``server_url`` and
    # injects via model_copy() before constructing MinerUBackend. Kept
    # here only because the adapter reads ``self.cfg.backend`` directly.
    backend: Literal[
        "pipeline",
        "vlm-auto-engine",
        "vlm-http-client",
        "vlm-sglang-client",
    ] = "pipeline"


class BackendsConfig(BaseModel):
    """Per-backend sub-configs.

    Only the one corresponding to ``parser.backend`` is read; the others
    are ignored. ``pymupdf`` carries no settings (placeholder).
    """

    pymupdf: PyMuPDFConfig = Field(default_factory=PyMuPDFConfig)
    mineru: MinerUConfig = Field(default_factory=MinerUConfig)


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


class NormalizeConfig(BaseModel):
    strip_header_footer: bool = True
    merge_cross_page_paragraphs: bool = True
    bind_captions: bool = True
    resolve_references: bool = True  # inline "see Figure N" / "见表 N"


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


class ChunkerConfig(BaseModel):
    # Target/max/min chunk size in approximate tokens
    target_tokens: int = 600
    max_tokens: int = 1000
    min_tokens: int = 100  # trailing chunks below this merge into previous

    # How to count tokens. "char_approx" has no extra dependency.
    tokenizer: Literal["char_approx"] = "char_approx"

    # Block isolation rules: these block types each get their own chunk
    # regardless of size, so structural integrity is preserved and
    # embeddings of heterogeneous content stay clean.
    isolate_tables: bool = True
    isolate_figures: bool = True
    isolate_formulas: bool = False  # inline formulas usually stay with text

    # Overlap strategy: N trailing blocks from chunk K become the first
    # N blocks of chunk K+1, improving recall at cost of duplication.
    # 0 = no overlap. Only applied within a single text run.
    overlap_blocks: int = 0


class TreeBuilderConfig(BaseModel):
    # LLM-driven tree + per-section summary in a single page-group pass.
    # Default is on so a properly-configured deployment gets rich trees
    # automatically; degrades gracefully to flat-fallback when ``model``
    # is unset (no API key required to run ForgeRAG bare-bones).
    llm_enabled: bool = True
    # Unified LLM fields (same names as every other module). Empty ``model``
    # is the kill-switch: the page-group strategy logs a warning and falls
    # back to the flat tree without touching the network. Populate
    # model + api_base + api_key_env (or api_key for dev) to wire it up.
    model: str | None = None  # e.g. "deepseek/deepseek-v4-flash"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None

    # Quality scoring parameters (used for tree_navigable decision)
    min_coverage: float = 0.80
    min_nodes: int = 3
    max_reasonable_depth: int = 6
    target_leaf_pages: float = 7.0  # ~1 leaf per 5-10 pages

    # Page-group strategy
    page_group_size: int = 5  # pages per group before LLM merge
    max_tokens_per_node: int = 8000  # subdivide leaf nodes exceeding this
    group_llm_max_chars: int = 40000  # max chars per LLM batch call

    # Summary enrichment concurrency
    summary_max_workers: int = 4  # parallel LLM calls for node summaries


# ---------------------------------------------------------------------------
# Parser section
# ---------------------------------------------------------------------------


class ParserSection(BaseModel):
    # Top-level explicit choice — one backend, no fallback chain.
    #
    #   pymupdf      — fast, CPU-only, baseline-quality text extraction.
    #                  Ships with the project; no extra deps.
    #   mineru       — MinerU's traditional layout-detection pipeline.
    #                  Best for complex tables / formulas / multi-column.
    #                  Heavy (PyTorch + GBs of model weights).
    #   mineru-vlm   — MinerU's VLM-backend mode. Highest quality on
    #                  scanned / handwritten / extremely complex layouts.
    #                  Most expensive; the same MinerU output schema, so
    #                  citations + bbox highlighting still work.
    backend: Literal["pymupdf", "mineru", "mineru-vlm"] = "pymupdf"

    backends: BackendsConfig = Field(default_factory=BackendsConfig)
    normalize: NormalizeConfig = Field(default_factory=NormalizeConfig)
    tree_builder: TreeBuilderConfig = Field(default_factory=TreeBuilderConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    ingest_max_workers: int = Field(
        default=10,
        description=(
            "Max concurrent document ingestion workers (parse + chunk + embed). Short, latency-sensitive jobs."
        ),
    )
    kg_max_workers: int = Field(
        default=10,
        description=(
            "Max concurrent KG extraction workers. Long-running jobs (minutes "
            "per doc), runs on its own thread pool so it can't starve the "
            "parse/embed pool. Default 10 matches ``ingest_max_workers``; "
            "lower if your LLM provider rate-limits below this concurrency."
        ),
    )
