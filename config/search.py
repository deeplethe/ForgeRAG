"""
Search-page configuration.

The Search page (POST /api/v1/search) is the dedicated keyword
finder — distinct from the agent's chat path. It runs BM25 over
the corpus and returns ranked passages grouped by file. To make
keyword search work across languages without abandoning BM25 (we
want the matched-token highlighting BM25 gives us), this module
configures a *query translation* pre-pass: a small LLM rewrites
the query into the target language(s), and the union of original
+ translated terms is sent into BM25.

Why this lives separate from RetrievalSection:
  RetrievalSection is the agent's retrieval pipeline (vector + KG
  + tree + rerank). The Search page deliberately stays pure-BM25
  for predictability and highlight support; sharing the heavy
  config there would couple the two unrelated subsystems.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TranslationConfig(BaseModel):
    """LLM-driven query translation for the Search page.

    Picks a small/cheap model — same one the rest of the
    lightweight tasks use (parser tree builder, query
    understanding, KG extraction). Thinking is hard-disabled at
    the call site (``extra_body={"thinking": {"type": "disabled"}}``)
    so e.g. DeepSeek's reasoning models don't burn tokens on a
    one-line translation. An LRU cache memoizes the translation
    so repeating the same query (the common case for human
    search) skips the LLM entirely.
    """

    enabled: bool = True

    # Same shape as every other LLM-using config block in the
    # project (RetrievalSection.query_understanding, KGExtraction,
    # parser.tree_builder, …): a litellm-style ``provider/model``
    # string, optional explicit key, optional env-var fallback,
    # optional API base.
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None

    # Languages we translate INTO. ISO 639-1 codes. The detected
    # source language is removed from this list at request time
    # so we don't translate ``"bee"`` to English (a no-op LLM
    # round-trip). Order in the list is preserved into the
    # expanded query — earlier entries influence BM25 scoring
    # slightly more on ties.
    target_languages: list[str] = Field(
        default_factory=lambda: ["en", "zh"],
        description="ISO 639-1 codes to translate the query into.",
    )

    # LRU size. Search is bursty, the same query is often re-typed
    # across page refreshes; 1024 fits a long working session
    # without growing unbounded. Lifetime: process — restart drops
    # the cache, the next first-search rewarms it.
    cache_size: int = 1024

    # Per-call LLM timeout (seconds). Translation is one short
    # round trip; capping at 8s keeps the search page responsive
    # even when the provider is slow. On timeout we fall back to
    # the original query (BM25 still runs, just no expansion).
    timeout: float = 8.0


class SearchConfig(BaseModel):
    """Top-level Search-page config. Currently just translation;
    leave the section in place for future BM25 tuning / synonyms /
    spell-correction additions without growing AppConfig further."""

    translation: TranslationConfig = Field(default_factory=TranslationConfig)
