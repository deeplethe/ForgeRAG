"""
REST API request/response schemas.

All responses follow a consistent shape:
    - Single resource: the resource object directly
    - List resource:   { items: [...], total: N, limit: N, offset: N }
    - Mutation result: { id: "...", status: "..." } or the updated resource
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Pagination wrapper
# ---------------------------------------------------------------------------


class PaginatedResponse(BaseModel):
    items: list[Any]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class FileOut(BaseModel):
    file_id: str
    content_hash: str
    original_name: str
    display_name: str
    size_bytes: int
    mime_type: str
    uploaded_at: Any


class UploadUrlRequest(BaseModel):
    url: str
    original_name: str | None = None
    mime_type: str | None = None


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    file_id: str
    doc_id: str | None = None
    parse_version: int = 1
    enrich_summary: bool | None = None
    force_reparse: bool = False
    folder_path: str | None = Field(
        None,
        description=(
            "Destination folder, e.g. '/legal/2024'. Default = '/'. Ignored "
            "on force_reparse (the existing doc's folder is preserved)."
        ),
    )


class DocumentOut(BaseModel):
    doc_id: str
    file_id: str | None = None
    pdf_file_id: str | None = None
    filename: str = ""
    format: str
    active_parse_version: int
    doc_profile_json: dict | None = None
    parse_trace_json: dict | None = None
    metadata_json: dict | None = None
    created_at: Any = None
    updated_at: Any = None
    # Folder membership (denormalised from folder tree; kept in sync by
    # FolderService on rename/move. ``path`` is the carrier used by
    # retrieval path_filter + workspace UI navigation.)
    folder_id: str | None = None
    path: str | None = None
    # Processing status
    status: str = "pending"
    error_message: str | None = None
    embed_status: str = "pending"
    embed_model: str | None = None
    embed_at: Any = None
    enrich_status: str = "pending"
    enrich_model: str | None = None
    enrich_summary_count: int = 0
    enrich_image_count: int = 0
    enrich_at: Any = None
    # Per-phase timing
    parse_started_at: Any = None
    parse_completed_at: Any = None
    structure_started_at: Any = None
    structure_completed_at: Any = None
    enrich_started_at: Any = None
    embed_started_at: Any = None
    # Knowledge Graph
    kg_status: Any = None
    kg_model: str | None = None
    kg_entity_count: Any = None
    kg_relation_count: Any = None
    kg_started_at: Any = None
    kg_completed_at: Any = None
    # Enriched fields (filled by the route, not the store)
    num_blocks: int | None = None
    num_chunks: int | None = None
    file_name: str | None = None
    file_size_bytes: int | None = None
    # Per-page metadata. Populated from ``pages_json`` on the
    # document row. Spreadsheet docs use this to expose sheet names
    # for the frontend tab strip; PDFs return the bare list with
    # ``name=None``.
    pages: list[dict] | None = None


class IngestResponse(BaseModel):
    file_id: str
    doc_id: str
    parse_version: int
    num_blocks: int
    num_chunks: int
    tree_quality: float


class IngestAcceptedResponse(BaseModel):
    """Returned when ingestion is queued for background processing."""

    file_id: str
    doc_id: str
    status: str = "pending"
    message: str = "queued for processing"


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


class BlockOut(BaseModel):
    block_id: str
    doc_id: str
    parse_version: int
    page_no: int
    seq: int
    bbox: dict = Field(description="x0, y0, x1, y1")
    type: str
    level: int | None = None
    text: str
    confidence: float
    table_html: str | None = None
    table_markdown: str | None = None
    image_storage_key: str | None = None
    image_caption: str | None = None
    formula_latex: str | None = None
    code_text: str | None = None
    code_language: str | None = None
    excluded: bool
    excluded_reason: str | None = None
    caption_of: str | None = None
    cross_ref_targets: list[str] = []


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


class ChunkOut(BaseModel):
    chunk_id: str
    doc_id: str
    parse_version: int
    node_id: str
    content: str
    content_type: str
    block_ids: list[str]
    page_start: int
    page_end: int
    token_count: int
    section_path: list[str]
    ancestor_node_ids: list[str]
    cross_ref_chunk_ids: list[str]
    role: str = "main"


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


class TreeNodeOut(BaseModel):
    node_id: str
    parent_id: str | None = None
    level: int
    title: str
    page_start: int
    page_end: int
    children: list[str]
    block_ids: list[str]
    element_types: list[str]
    table_count: int
    image_count: int
    summary: str | None = None
    key_entities: list[str] = []
    role: str = "main"


class TreeOut(BaseModel):
    doc_id: str
    parse_version: int
    root_id: str
    quality_score: float
    generation_method: str
    nodes: dict[str, TreeNodeOut]


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class QueryOverrides(BaseModel):
    """
    Per-request overrides of retrieval-pipeline yaml defaults. Every field is
    ``None`` by default — meaning "use the cfg value". Set a field to take
    effect only for this single query; the global yaml is untouched.

    Notes:
      * Turning a path OFF always works. Turning a path ON that yaml has OFF
        works for lazily-initialised collaborators (query_understanding,
        rerank, kg). ``tree_llm_nav`` can only be overridden ON if yaml's
        ``retrieval.tree_path.llm_nav_enabled`` is also true — the navigator
        is constructed at startup.
    """

    # Path switches (None = yaml default)
    query_understanding: bool | None = Field(
        None,
        description="Skip query understanding / expansion when false. Saves the QU LLM call.",
    )
    kg_path: bool | None = None
    tree_path: bool | None = None
    tree_llm_nav: bool | None = Field(
        None,
        description="LLM tree navigation (vs heuristic). Can only be enabled if yaml has tree_path.llm_nav_enabled = true.",
    )
    rerank: bool | None = None

    # Top-k overrides (None = yaml)
    bm25_top_k: int | None = Field(None, ge=1, le=500)
    vector_top_k: int | None = Field(None, ge=1, le=500)
    tree_top_k: int | None = Field(None, ge=1, le=500)
    kg_top_k: int | None = Field(None, ge=1, le=500)
    rerank_top_k: int | None = Field(None, ge=1, le=500)

    # Fusion / expansion
    candidate_limit: int | None = Field(
        None,
        ge=1,
        le=500,
        description="Cap on merged candidates passed to rerank / downstream.",
    )
    descendant_expansion: bool | None = None
    sibling_expansion: bool | None = None
    crossref_expansion: bool | None = None

    # Failure-handling escape hatch
    allow_partial_failure: bool | None = Field(
        None,
        description=(
            "Default false = a single path (BM25/vector/tree/KG/rerank/QU) "
            "that raises an exception aborts the whole query with a "
            "RetrievalError (HTTP 502). Set true to fall back to the "
            "legacy 'log + zero hits, continue' behaviour — useful for "
            "batch jobs where one bad query shouldn't kill the rest."
        ),
    )


class GenerationOverrides(BaseModel):
    """
    Per-request overrides of generation yaml defaults. Every field is
    ``None`` by default — meaning "use cfg value". Set a field to take
    effect only for this single query.

    These travel through the API to the LLM via LiteLLM's unified
    cross-provider params. ``reasoning_effort`` in particular is
    routed automatically: for OpenAI o-series / xAI it passes through;
    for Anthropic it maps to ``thinking={type, budget_tokens}``; for
    Gemini it maps to ``thinking_budget``; for DeepSeek any non-none
    value enables thinking (DeepSeek's LiteLLM route doesn't surface
    "disable" — for that, set ``extra_kwargs.extra_body.thinking`` in
    yaml; this UI override doesn't yet expose it).
    """

    # Boolean thinking-mode switch. Works across providers because we
    # route both via ``extra_body.thinking.type`` (DeepSeek's quirk) and
    # via ``reasoning_effort=disable`` (Anthropic / Gemini). OpenAI
    # o-series / xAI ignore (thinking is model-bound for them, can't be
    # toggled per request — picking ``gpt-4.1`` vs ``o3-mini`` is the
    # toggle there).
    thinking: bool | None = Field(
        None,
        description=(
            "True = force thinking ON, False = force OFF, None = use "
            "provider default. Resolved both via ``extra_body.thinking`` "
            "(DeepSeek) and ``reasoning_effort`` (Anthropic / Gemini)."
        ),
    )
    # Intensity dial — orthogonal to ``thinking``. ``low``/``medium``/
    # ``high`` are universally meaningful; ``disable``/``none`` are
    # better expressed via ``thinking=False`` above.
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(None)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, ge=1, le=128000)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=8192)
    filter: dict[str, Any] | None = None
    # Path scoping: when set, retrieval is limited to documents whose path
    # starts with this prefix (e.g. "/legal/2024"). Trashed documents are
    # always excluded regardless of this filter.
    path_filter: str | None = Field(
        None,
        description=(
            "Limit retrieval to documents under this folder path. "
            "Matches by path prefix (e.g. '/legal' matches '/legal/2024/x.pdf'). "
            "Trashed documents are always excluded."
        ),
    )
    overrides: QueryOverrides | None = Field(
        None,
        description="Per-request overrides of retrieval yaml defaults. Unset fields fall through to cfg.",
    )
    generation_overrides: GenerationOverrides | None = Field(
        None,
        description=(
            "Per-request overrides of generation yaml defaults — typically "
            "wired to a UI 'Tools' panel for reasoning_effort / temperature."
        ),
    )
    conversation_id: str | None = Field(
        None,
        description=(
            "Pass a conversation_id to continue a multi-turn chat. "
            "Omit or null to start a new standalone query. "
            "If the id doesn't exist yet, a new conversation is auto-created."
        ),
    )
    stream: bool = Field(
        False,
        description="If true, return a text/event-stream SSE response.",
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


class ConversationOut(BaseModel):
    conversation_id: str
    title: str | None = None
    created_at: Any = None
    updated_at: Any = None
    message_count: int | None = None


class MessageOut(BaseModel):
    message_id: str
    conversation_id: str
    role: str
    content: str
    trace_id: str | None = None
    citations_json: list | None = None
    thinking: str | None = None
    created_at: Any = None


class HighlightOut(BaseModel):
    page_no: int
    bbox: tuple[float, float, float, float]


class CitationOut(BaseModel):
    citation_id: str
    doc_id: str
    file_id: str | None = None
    source_file_id: str | None = None
    source_format: str = ""
    parse_version: int
    page_no: int
    highlights: list[HighlightOut]
    snippet: str
    score: float
    open_url: str | None = None


class QueryResponse(BaseModel):
    query: str
    text: str
    citations_used: list[CitationOut]
    citations_all: list[CitationOut]
    model: str
    finish_reason: str
    stats: dict[str, Any]
    trace: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Search — the retrieval primitive (no LLM answer).
# See docs/roadmaps/retrieval-evolution.md for the design.
# ---------------------------------------------------------------------------


class SearchLimit(BaseModel):
    """Per-view caps. Either field may be omitted to use the module
    defaults (30 chunks, 10 files)."""

    chunks: int | None = Field(None, ge=1, le=200)
    files: int | None = Field(None, ge=1, le=100)


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=8192)
    # Default: chunks-only. Workspace search bars pass ["files"] (or
    # both) explicitly when they want the file rollup.
    include: list[str] = Field(
        default_factory=lambda: ["chunks"],
        description='Subset of {"chunks", "files"}. Empty / unrecognised values fall back to ["chunks"].',
    )
    limit: SearchLimit | None = None
    filter: dict[str, Any] | None = None
    path_filter: str | None = Field(
        None,
        description=(
            "Limit results to documents under this folder path. "
            "Matches by path prefix (e.g. '/legal' matches '/legal/2024/x.pdf'). "
            "Trashed documents are always excluded."
        ),
    )
    overrides: QueryOverrides | None = Field(
        None,
        description="Per-request retrieval overrides. /search defaults rerank=False to stay cheap.",
    )


class ScoredChunkOut(BaseModel):
    chunk_id: str
    doc_id: str
    filename: str
    path: str
    page_no: int
    snippet: str
    score: float
    boosted_by_filename: bool = False


class ChunkMatchOut(BaseModel):
    chunk_id: str
    snippet: str
    page_no: int
    score: float


class FileHitOut(BaseModel):
    doc_id: str
    filename: str
    path: str
    format: str
    score: float
    matched_in: list[str]
    best_chunk: ChunkMatchOut | None = None
    filename_tokens: list[str] | None = None


class SearchResponse(BaseModel):
    query: str
    chunks: list[ScoredChunkOut] = Field(default_factory=list)
    files: list[FileHitOut] | None = None
    stats: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


class TraceSummaryOut(BaseModel):
    trace_id: str
    query: str
    timestamp: Any
    total_ms: int
    total_llm_ms: int
    total_llm_calls: int
    answer_model: str | None = None
    finish_reason: str | None = None
    citations_used: list[str]


class TraceDetailOut(TraceSummaryOut):
    answer_text: str | None = None
    trace_json: dict[str, Any]
    metadata_json: dict[str, Any]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    version: str = "1"
    components: dict[str, str]
    counts: dict[str, int] | None = None
    # Per-feature capability flags + lists. Populated from cfg at the
    # /health route so the frontend can pre-flight uploads etc. without
    # a separate capabilities endpoint. Keys defined so far:
    #
    #   image_upload          (bool)  — image-as-document uploads work
    #                                   (image_enrichment.enabled + a
    #                                    VLM model + reachable creds)
    #   image_upload_extensions (list) — extensions to accept when the
    #                                    above is True. Empty when off.
    features: dict[str, object] | None = None
