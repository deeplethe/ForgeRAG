"""
Web-search configuration.

The web-search layer is isolated by design: this module only declares
intent (which provider, with what key, behind what budget). It is
*not* wired into ``/search`` or ``/query`` yet — the first real
consumer will be the agentic-search loop (Feature 4).

Two providers are supported in the first cut:

    tavily   -- LLM-tuned web search; results come with snippets that
                are already extractive summaries. Pricing roughly
                $5 per 1k queries.

    brave    -- Independent web index with raw snippets. Cheaper
                (~$3 per 1k) and a better-than-Bing free tier.

Auth follows the same convention as the embedder block: prefer
``api_key_env`` (read from the named env var at request time) and fall
back to ``api_key`` for plaintext storage in opencraig.yaml.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TavilyConfig(BaseModel):
    api_key: str | None = None  # direct key value
    api_key_env: str | None = "TAVILY_API_KEY"  # env var holding the key
    # ``basic`` ≈ snippets only; ``advanced`` runs Tavily's deeper
    # extraction step server-side. Default basic for cost.
    search_depth: Literal["basic", "advanced"] = "basic"
    timeout: float = 15.0


class BraveConfig(BaseModel):
    api_key: str | None = None
    api_key_env: str | None = "BRAVE_API_KEY"
    timeout: float = 15.0


class WebSearchCacheConfig(BaseModel):
    """In-memory LRU; persistence is deliberately not wired here.

    Web search results are short-lived (news, prices, headlines) and the
    cache exists to absorb same-query iteration from the agentic loop,
    not to be a long-term store. Process-local is fine."""

    enabled: bool = True
    max_entries: int = 256
    ttl_seconds: int = 300  # 5 min — long enough for one agentic session


class WebSearchCostConfig(BaseModel):
    """Soft cap on a single session's web-search spend.

    Counted per-call by the calling code (the module exposes a counter;
    integration is the caller's job). Hard cap raises; we never silently
    truncate paid responses."""

    cap_usd_per_session: float = 1.00
    # Per-call rough cost estimates used by the CostCounter when the
    # provider response doesn't carry billing metadata. Tunable per
    # vendor when they publish a real meter.
    cost_per_call_tavily_usd: float = 0.005
    cost_per_call_brave_usd: float = 0.003


class WebSearchConfig(BaseModel):
    """Top-level web-search section.

    Disabled by default — opt in by setting ``enabled: true`` and
    configuring at least one provider's credentials."""

    enabled: bool = False
    default_provider: Literal["tavily", "brave"] = "tavily"
    default_top_k: int = Field(8, ge=1, le=50)

    tavily: TavilyConfig | None = Field(default_factory=TavilyConfig)
    brave: BraveConfig | None = Field(default_factory=BraveConfig)

    cache: WebSearchCacheConfig = Field(default_factory=WebSearchCacheConfig)
    cost: WebSearchCostConfig = Field(default_factory=WebSearchCostConfig)

    # Hard truncation on fetched-page content, applied AFTER injection
    # stripping. Bound abuse (massive pages can't blow up the prompt).
    max_fetched_chars: int = Field(8000, ge=500)
