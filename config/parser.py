"""
Parser-layer configuration.

Holds backend enable/disable flags, quality thresholds, probe
thresholds, and normalizer switches. Storage config is intentionally
separate (see config/storage.py) because it is shared with
non-parser modules.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class PyMuPDFConfig(BaseModel):
    enabled: bool = True
    min_quality: float = 0.0  # always passes -- final fallback


class MinerUConfig(BaseModel):
    enabled: bool = False
    min_quality: float = 0.70
    # Backend mode. Supports both MinerU 2.x and 3.0 names; the
    # `do_parse` signature is backward compatible across releases.
    #
    # MinerU 3.0 (default in upstream is hybrid-auto-engine):
    #     pipeline              -- fast, CPU/GPU, stable
    #     hybrid-auto-engine    -- next-gen, best accuracy, local
    #     hybrid-http-client    -- hybrid via remote OpenAI-style server
    #     vlm-auto-engine       -- VLM backend, local
    #     vlm-http-client       -- VLM backend, remote
    #
    # MinerU 2.x legacy names (still accepted when running 2.x):
    #     vlm-transformers      -- VLM via transformers
    #     vlm-sglang-engine     -- VLM via sglang
    #     vlm-sglang-client     -- VLM via remote sglang server
    backend: Literal[
        "pipeline",
        "hybrid-auto-engine",
        "hybrid-http-client",
        "vlm-auto-engine",
        "vlm-http-client",
        "vlm-transformers",
        "vlm-sglang-engine",
        "vlm-sglang-client",
    ] = "pipeline"

    parse_method: Literal["auto", "txt", "ocr"] = "auto"
    device: Literal["cuda", "cpu"] = "cuda"
    # Primary OCR language (single lang per doc in both 2.x and 3.0)
    lang: str = "ch"
    # Enable formula / table parsing
    formula_enable: bool = True
    table_enable: bool = True
    # Required for any *-http-client / *-sglang-client backend.
    # Ignored otherwise.
    server_url: str | None = None


class VLMConfig(BaseModel):
    enabled: bool = False
    min_quality: float = 0.75
    model: str = "Qwen2.5-VL-7B"
    mode: Literal["whole_document"] = "whole_document"
    max_pages: int = 200  # refuse docs larger than this


class DoclingConfig(BaseModel):
    enabled: bool = False
    min_quality: float = 0.60


class BackendsConfig(BaseModel):
    pymupdf: PyMuPDFConfig = Field(default_factory=PyMuPDFConfig)
    mineru: MinerUConfig = Field(default_factory=MinerUConfig)
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    docling: DoclingConfig = Field(default_factory=DoclingConfig)


# ---------------------------------------------------------------------------
# Probe thresholds
# ---------------------------------------------------------------------------


class ProbeConfig(BaseModel):
    scanned_ratio_threshold: float = 0.30
    text_density_min: int = 50  # chars per page
    table_density_threshold: float = 0.15
    multicolumn_x_cluster_gap: float = 50.0  # pt
    heading_hint_min_strength: float = 0.20

    # Complexity bucket thresholds
    complex_page_count: int = 100
    complex_scanned_ratio: float = 0.30
    medium_page_count: int = 20


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
    llm_enabled: bool = False  # LLM builds tree + summary via page-group strategy
    # Unified LLM fields (same names as every other module). Empty model
    # disables the LLM tree builder entirely; populate model + api_base +
    # api_key_env (or api_key for dev) to wire it up.
    model: str | None = None  # e.g. "openai/gpt-4o-mini"
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
    backends: BackendsConfig = Field(default_factory=BackendsConfig)
    probe: ProbeConfig = Field(default_factory=ProbeConfig)
    normalize: NormalizeConfig = Field(default_factory=NormalizeConfig)
    tree_builder: TreeBuilderConfig = Field(default_factory=TreeBuilderConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    ingest_max_workers: int = Field(default=10, description="Max concurrent document ingestion workers")
