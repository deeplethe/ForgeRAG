"""Cache configuration."""

from pydantic import BaseModel, Field


class LLMCacheSubconfig(BaseModel):
    """Disk-backed cache for INGEST-side LLM calls only.

    Targets the high-volume deterministic-prompt callers (KG extraction,
    tree builder, tree-summary enrichment, image-enrichment VLM). Query-
    side callers (QU, rerank, generation, tree-nav) deliberately bypass
    this cache — see ``forgerag/llm_cache.py`` for the rationale.

    Default ON because the killer use case is failure recovery: when a
    long ingest crashes mid-corpus (rate-limit, balance, network), the
    cache turns the retry from "burn all the tokens again" into
    "resume from where we died". Cost vs. risk strongly favours on.

    Set ``enabled: false`` to skip caching entirely (saves disk; loses
    crash-recovery + dev-iteration speed).
    """

    enabled: bool = True
    directory: str = "./storage/llm_cache"
    # Hard disk cap; oldest-evicted via diskcache's LRU. 0 = unlimited.
    size_limit_gb: float = 5.0


class CacheConfig(BaseModel):
    bm25_persistence: bool = True
    embedding_cache: bool = True
    bm25_path: str = "./storage/bm25_index.pkl"
    embedding_path: str = "./storage/embedding_cache.pkl"
    llm: LLMCacheSubconfig = Field(default_factory=LLMCacheSubconfig)
