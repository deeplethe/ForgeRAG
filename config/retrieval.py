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

    provider_id: str | None = None  # resolved at startup from llm_providers table
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    temperature: float = 0.0
    max_tokens: int = 1024
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
    # Max chunks to return from the tree path
    top_k: int = 30


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
    provider_id: str | None = None  # resolved at startup from llm_providers table
    enabled: bool = False
    backend: Literal["passthrough", "litellm"] = "passthrough"
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


class QueryExpansionConfig(BaseModel):
    """Legacy — kept for backward compat. Use QueryUnderstandingConfig."""

    provider_id: str | None = None
    enabled: bool = False
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    max_expansions: int = 3
    timeout: float = 15.0


class QueryUnderstandingConfig(BaseModel):
    """Unified query understanding: intent + routing + expansion."""

    provider_id: str | None = None
    enabled: bool = False
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    max_expansions: int = 3
    timeout: float = 10.0
    system_prompt: str | None = None
    user_prompt_template: str | None = None


class KGExtractionConfig(BaseModel):
    """Ingestion-time entity/relation extraction settings."""

    enabled: bool = False
    provider_id: str | None = None
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    max_workers: int = 5
    timeout: float = 120.0
    embed_relations: bool = False
    embed_entity_names: bool = False
    # Description merge: LLM-consolidate fragmented entity/relation descriptions.
    # When an entity accumulates many description fragments (from multiple chunks
    # or documents), an LLM call synthesizes them into one concise description.
    merge_description_threshold: int = 6  # fragment count that triggers LLM merge
    merge_description_max_chars: int = 2000  # char length that triggers LLM merge


class KGPathConfig(BaseModel):
    """Knowledge graph retrieval path settings."""

    enabled: bool = False
    provider_id: str | None = None  # LLM for extracting entities from query
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    top_k: int = 30
    max_hops: int = 2
    local_weight: float = 0.5
    global_weight: float = 0.2
    # Community-based retrieval
    community_weight: float = 0.2
    community_top_k: int = 5
    # Relation semantic search
    relation_weight: float = 0.1
    relation_top_k: int = 10


class RetrievalSection(BaseModel):
    query_expansion: QueryExpansionConfig = Field(default_factory=QueryExpansionConfig)
    query_understanding: QueryUnderstandingConfig = Field(default_factory=QueryUnderstandingConfig)
    bm25: BM25Config = Field(default_factory=BM25Config)
    vector: VectorSearchConfig = Field(default_factory=VectorSearchConfig)
    tree_path: TreePathConfig = Field(default_factory=TreePathConfig)
    merge: MergeConfig = Field(default_factory=MergeConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    citations: CitationsConfig = Field(default_factory=CitationsConfig)
    kg_extraction: KGExtractionConfig = Field(default_factory=KGExtractionConfig)
    kg_path: KGPathConfig = Field(default_factory=KGPathConfig)
