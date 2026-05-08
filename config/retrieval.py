"""
Retrieval configuration.

Structured around the dual-path architecture:

    query
      ├── vector path   (embedding -> VectorStore.search)
      ├── tree path     (BM25 doc prefilter -> [LLM nav] -> chunks)
      │
      └── merge (RRF + sibling/crossref expansion) -> rerank -> citations
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BM25Config(BaseModel):
    enabled: bool = True  # independent BM25 path into RRF
    k1: float = 1.5
    b: float = 0.75
    top_k: int = 30  # chunks returned by independent BM25 path
    # Number of docs to keep after BM25 prefilter on the tree path.
    doc_prefilter_top_k: int = 10


class VectorSearchConfig(BaseModel):
    enabled: bool = True
    top_k: int = 30
    # Optional hard filter passed to VectorStore.search()
    # e.g. {"content_type": "text"} to exclude table/figure chunks
    default_filter: dict | None = None


class TreeNavConfig(BaseModel):
    """LLM tree navigator config (PageIndex-style)."""

    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    temperature: float = 0.0
    timeout: float = 30.0
    max_nodes: int = 8  # LLM returns up to this many node_ids
    max_docs: int = 8  # max documents to navigate (top by cross-validation score)
    max_workers: int = 5  # parallel LLM calls for tree navigation
    target_chunks: int = 30  # early-stop once this many chunks accumulated
    system_prompt: str | None = None


class TreePathConfig(BaseModel):
    enabled: bool = True
    # When True, uses LLM to navigate the tree (PageIndex-style).
    # When False, falls back to BM25 top chunks within prefiltered docs.
    llm_nav_enabled: bool = True
    nav: TreeNavConfig = Field(default_factory=TreeNavConfig)
    # Max chunks to return from the tree path. Default 20 balances recall
    # with rerank latency — Run 5 bench showed 20 feeds the reranker enough
    # candidates without wasting time on obvious long-tail chunks.
    top_k: int = 20


class MergeConfig(BaseModel):
    rrf_k: int = 60

    # Sibling expansion: pull in other chunks of the same leaf node
    sibling_expansion_enabled: bool = True
    sibling_max_node_size: int = 5
    sibling_max_per_hit: int = 3
    sibling_score_discount: float = 0.5

    # Cross-reference expansion: follow chunk.cross_ref_chunk_ids
    crossref_expansion_enabled: bool = True
    crossref_max_per_hit: int = 5
    crossref_score_discount: float = 0.4

    # Descendant expansion (PageIndex-style): when a matched chunk
    # belongs to a non-leaf tree node (typically a heading), pull
    # content chunks from its child nodes so the LLM gets the
    # section body, not just the title.
    descendant_expansion_enabled: bool = True
    descendant_max_chunks: int = 8  # max child chunks per heading hit
    descendant_score_discount: float = 0.7  # higher than sibling — these are highly relevant
    descendant_min_token_threshold: int = 80  # only expand if the hit chunk is "thin" (heading-like)

    # Global cap on the merged candidate set
    global_budget_multiplier: float = 2.0

    # Cap passed to rerank / downstream
    candidate_limit: int = 60


class RerankConfig(BaseModel):
    # No ``enabled`` toggle: rerank is part of the pipeline. Per-query opt-out
    # via ``QueryOverrides.rerank=False`` (e.g. for benchmark A/B). Default
    # backend is ``llm_as_reranker`` (reuses generator credentials);
    # production deployments with a dedicated rerank API can switch to
    # ``rerank_api``; ``passthrough`` is the no-op baseline.
    backend: Literal["passthrough", "rerank_api", "llm_as_reranker"] = "llm_as_reranker"
    # on_failure="strict" raises the error so the UI lights up red on the
    # architecture graph; "passthrough" silently returns top_k by RRF order
    # (the legacy behaviour that hid bugs). Default is strict so users
    # discover misconfiguration immediately.
    on_failure: Literal["strict", "passthrough"] = "strict"
    model: str = "openai/gpt-4o-mini"
    top_k: int = 10
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    timeout: float = 30.0
    # Prompt budget (chars) for each candidate's snippet
    snippet_chars: int = 500
    system_prompt: str | None = None


class CitationsConfig(BaseModel):
    max_snippet_chars: int = 200
    # URL template for the online viewer. {doc_id}, {page_no},
    # {citation_id} are substituted.
    open_url_template: str = "/viewer/{doc_id}?page={page_no}&hl={citation_id}"


class QueryUnderstandingConfig(BaseModel):
    """Unified query understanding: intent + routing + expansion.

    No ``enabled`` toggle: QU runs on every retrieve(). Per-query opt-out
    via ``QueryOverrides.query_understanding=False``.
    """

    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    max_expansions: int = 3
    timeout: float = 10.0
    system_prompt: str | None = None
    user_prompt_template: str | None = None


class KGSummaryConfig(BaseModel):
    """Description-compaction settings (LightRAG-style).

    The graph stores merge entity / relation descriptions across
    upserts by substring-deduped concatenation, so a frequently-
    mentioned entity's description grows linearly with chunk count.
    When fragment count or token total cross the thresholds below,
    the ingest pipeline calls
    ``graph.summarize.summarize_descriptions`` to LLM-compact the
    accumulated fragments into a single canonical paragraph.
    Map-reduce + recursion handle the case where the fragment list
    is too big for one LLM call.

    Disable by setting ``enabled = False`` (descriptions then grow
    unbounded — fine for small corpora, bad for production).
    """

    enabled: bool = True

    # Trigger gates — either condition fires summarisation.
    # Defaults tuned for OpenCraig's typical fragment profile
    # (~50–100 tokens / fragment): 1200 tokens ≈ 12 average
    # fragments, 8 fragments is the count-based escape hatch for
    # when many small fragments slip under the token gate.
    trigger_tokens: int = 1200
    force_on_count: int = 8

    # Output length target — a soft prompt-side ceiling.
    max_output_tokens: int = 600

    # Map-reduce input window. If total fragment tokens exceed
    # this, we split into ≥2-fragment chunks, summarise each, then
    # loop on the chunk summaries until the total fits.
    context_size: int = 12000

    # Convergence guard for the map-reduce loop.
    max_iterations: int = 5

    # LLM call params. By default reuses the KG extraction model so
    # ingest sticks to a single provider; override to point at a
    # cheaper / faster model dedicated to summarisation.
    model: str | None = None  # None = inherit from KGExtractionConfig.model
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    timeout: float = 60.0

    # Number of entities + relations to summarise in parallel
    # post-upsert. Higher → faster wall-time on large ingests but
    # more concurrent provider load.
    max_workers: int = 5

    # Language directive for the prompt. Default tells the LLM to
    # follow the input language — works for monolingual EN, ZH, and
    # mixed corpora. Override to e.g. ``"Write the entire output in
    # Chinese"`` to force a canonical language.
    language: str = "Write the entire output in the original language of the input descriptions"


class KGExtractionConfig(BaseModel):
    """Ingestion-time entity/relation extraction settings.

    No ``enabled`` toggle: when ``graph_store`` is configured (i.e.
    Neo4j credentials are set), every ingest runs KG extraction.
    To opt out entirely, leave the graph store unconfigured.
    Entity-name and relation-description embeddings are likewise
    always computed because both downstream paths
    (``EntityDisambiguation``, ``KGPath.relation_weight`` semantic
    search) silently degrade without them.
    """

    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    # Per-chunk extraction means ~5x more API calls than the prior
    # batched path; 10 workers keeps wall-time comparable. Lower if
    # the upstream provider rate-limits below this concurrency.
    max_workers: int = 10
    timeout: float = 120.0

    # Description compaction settings — see ``KGSummaryConfig``.
    # Replaces the previous ``merge_description_threshold`` /
    # ``merge_description_max_chars`` flat fields, which only ran
    # against the per-chunk extraction batch (never the cumulative
    # graph state) and were therefore mostly a no-op.
    summary: KGSummaryConfig = Field(default_factory=KGSummaryConfig)


class KGPathConfig(BaseModel):
    """Knowledge graph retrieval path settings.

    No ``enabled`` toggle: when ``graph_store`` is configured the
    KG path participates in retrieval. Per-query opt-out via
    ``QueryOverrides.kg_path=False``.
    """

    model: str = "openai/gpt-4o-mini"  # LLM for extracting entities from query
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    top_k: int = 30
    # max_hops default 1: 2-hop expansion from hub entities (e.g. "Company"
    # in legal corpora) can explode to 3000+ nodes. Users who want deeper
    # traversal can raise this, but 1-hop is the safe default.
    max_hops: int = 1
    local_weight: float = 0.5
    global_weight: float = 0.2
    # Relation semantic search
    relation_weight: float = 0.1
    relation_top_k: int = 10


class RetrievalSection(BaseModel):
    query_understanding: QueryUnderstandingConfig = Field(default_factory=QueryUnderstandingConfig)
    bm25: BM25Config = Field(default_factory=BM25Config)
    vector: VectorSearchConfig = Field(default_factory=VectorSearchConfig)
    tree_path: TreePathConfig = Field(default_factory=TreePathConfig)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    citations: CitationsConfig = Field(default_factory=CitationsConfig)
    kg_extraction: KGExtractionConfig = Field(default_factory=KGExtractionConfig)
    kg_path: KGPathConfig = Field(default_factory=KGPathConfig)
