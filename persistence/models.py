"""
SQLAlchemy 2.0 declarative models.

Portable across Postgres / SQLite via the dialect URL.
Array-like fields use the JSON column type (JSONB on Postgres,
TEXT on SQLite) because our access pattern is "store a list, read
it back whole" rather than indexed lookups.

Hard-overwrite versioning:
    (doc_id, parse_version) is treated as the unit of truth.
    ingestion_writer deletes old rows by parse_version inside a
    single transaction, then inserts the new ones.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Files (new: content-addressed user uploads)
# ---------------------------------------------------------------------------


class File(Base):
    __tablename__ = "files"

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(128), index=True)
    storage_key: Mapped[str] = mapped_column(String(512))
    original_name: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(128))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    file_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("files.file_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # If the source was converted to PDF (e.g. DOCX→PDF), this points
    # to the converted PDF file for viewing/highlighting.
    pdf_file_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("files.file_id", ondelete="SET NULL"),
        nullable=True,
    )
    # ── Folder tree membership ──────────────────────────────────────
    # folder_id is the stable anchor (survives rename/move unchanged).
    # path is a cached denormalization for fast prefix queries — kept in
    # sync by FolderService transactions. `__root__` is the default folder.
    folder_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("folders.folder_id", ondelete="RESTRICT"),
        default="__root__",
        server_default="__root__",
        index=True,
    )
    path: Mapped[str] = mapped_column(
        String(1024),
        default="/",
        server_default="/",
        index=True,
    )
    # Populated only for trashed documents: original folder + path + metadata
    # Stored as JSON so we can restore accurately even if the original
    # folder was also trashed. Shape: {original_folder_id, original_path,
    # trashed_at (iso), trashed_by}
    trashed_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    filename: Mapped[str] = mapped_column(String(512), default="")
    format: Mapped[str] = mapped_column(String(32))
    active_parse_version: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    doc_profile_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parse_trace_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Processing status ---
    status: Mapped[str | None] = mapped_column(String(32), nullable=True, server_default="pending")
    # Embedding
    embed_status: Mapped[str | None] = mapped_column(String(32), nullable=True, server_default="pending")
    embed_provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # LLM enrichment
    enrich_status: Mapped[str | None] = mapped_column(String(32), nullable=True, server_default="pending")
    enrich_provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enrich_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enrich_summary_count: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    enrich_image_count: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    enrich_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Per-phase timing
    parse_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parse_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    structure_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    structure_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enrich_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    embed_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Knowledge Graph extraction
    kg_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kg_entity_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kg_relation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kg_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    kg_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    kg_provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kg_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Tree navigation eligibility
    tree_navigable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, server_default="1")
    tree_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    tree_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Error message (human-readable reason for status="error")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Page dimensions: [{page_no, width, height}, ...]
    pages_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Parsed blocks
# ---------------------------------------------------------------------------


class ParsedBlock(Base):
    __tablename__ = "parsed_blocks"

    block_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        index=True,
    )
    parse_version: Mapped[int] = mapped_column(Integer, index=True)
    page_no: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)

    bbox_x0: Mapped[float] = mapped_column(Float)
    bbox_y0: Mapped[float] = mapped_column(Float)
    bbox_x1: Mapped[float] = mapped_column(Float)
    bbox_y1: Mapped[float] = mapped_column(Float)

    type: Mapped[str] = mapped_column(String(32))
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    table_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    table_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_mime: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    formula_latex: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_language: Mapped[str | None] = mapped_column(String(64), nullable=True)

    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    excluded_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    caption_of: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cross_ref_targets: Mapped[list] = mapped_column(JSON, default=list)

    __table_args__ = (Index("ix_parsed_blocks_doc_version", "doc_id", "parse_version"),)


# ---------------------------------------------------------------------------
# Doc tree (pure JSONB)
# ---------------------------------------------------------------------------


class DocTreeRow(Base):
    __tablename__ = "doc_trees"

    doc_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
    )
    parse_version: Mapped[int] = mapped_column(Integer)
    root_id: Mapped[str] = mapped_column(String(255))
    quality_score: Mapped[float] = mapped_column(Float)
    generation_method: Mapped[str] = mapped_column(String(32))
    tree_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (PrimaryKeyConstraint("doc_id", "parse_version"),)


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Query traces (retrieval pipeline audit log)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Runtime settings (frontend-editable config overrides)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Conversations (multi-turn chat)
# ---------------------------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text)
    # For assistant messages: link back to the trace + citations
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    citations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Reasoning content from thinking-mode LLMs (DeepSeek V4-Pro / o1 /
    # deepseek-reasoner). Persisted on the message row so the Thinking
    # pane survives conversation switches; without this the reasoning
    # text only lives on the in-flight stream and is lost on reload.
    thinking: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# Runtime settings (frontend-editable config overrides)
# ---------------------------------------------------------------------------


class Setting(Base):
    """
    Key-value store for runtime config overrides.

    The yaml file is the base config. Settings in this table are
    OVERLAID on top — a key like "retrieval.rerank.backend" with
    value_json="passthrough" means "override that one field at runtime".

    Groups allow the frontend to organize settings into tabs:
        llm, embedding, retrieval, parsing, system

    Schema is intentionally flat (not nested JSONB) so the frontend
    can render a simple form: one row per toggle/input.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON)
    group_name: Mapped[str] = mapped_column(String(64), index=True, default="system")
    label: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_type: Mapped[str] = mapped_column(String(32), default="string")  # string/int/float/bool/enum/secret
    enum_options: Mapped[list | None] = mapped_column(JSON, nullable=True)  # for value_type=enum
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# LLM Providers (pluggable model registry)
# ---------------------------------------------------------------------------


class LLMProvider(Base):
    """
    Registry of LLM / embedding / reranker endpoints.

    Allows the system to reference models by a short user-defined name
    instead of hardcoding model strings and API keys in settings.
    The `provider_type` column distinguishes chat models from embedding
    models from rerankers so the UI can filter appropriately.
    """

    __tablename__ = "llm_providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    provider_type: Mapped[str] = mapped_column(String(32), index=True)  # chat / embedding / reranker
    api_base: Mapped[str | None] = mapped_column(String(512), nullable=True)
    model_name: Mapped[str] = mapped_column(String(255))  # litellm model string
    api_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# ---------------------------------------------------------------------------
# Query traces (retrieval pipeline audit log)
# ---------------------------------------------------------------------------


class QueryTrace(Base):
    __tablename__ = "query_traces"

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    total_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_llm_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    citations_used: Mapped[list] = mapped_column(JSON, default=list)
    trace_json: Mapped[dict] = mapped_column(JSON)  # full trace phases
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Folder tree (retrieval scope)
# ---------------------------------------------------------------------------
#
# Design contract:
#   - folder_id is the stable anchor. Rename / move never changes it.
#   - path is a cached denormalization — kept in sync by FolderService.
#     Path is the retrieval / mutation scope carrier (Chroma, Neo4j, PG all
#     filter by path prefix). folder_id is only the relational anchor.
#   - Two built-in system folders seeded by the migration:
#       __root__   (path='/')       — everything's ancestor
#       __trash__  (path='/__trash__') — trash bin
#   - A document always belongs to exactly one folder. "No folder" = __root__.
#   - Single-tenant: there is no access-control layer planned. See
#     persistence/scope.py for why the FolderGrant model below is dormant.


class Folder(Base):
    __tablename__ = "folders"

    folder_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    path_lower: Mapped[str] = mapped_column(
        String(1024),
        index=True,
        default="",
        server_default="",
    )  # case-insensitive dedup lookup (keeps original case in `path` for display)
    parent_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("folders.folder_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # When trashed_at is NOT NULL, this folder is inside trash (a descendant of
    # __trash__). Documents inside it inherit the trashed view automatically.
    trashed_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class FolderGrant(Base):
    """
    DORMANT — kept only to avoid a schema migration. Originally designed for
    a Phase-2 ACL that we've since decided against: ForgeRAG is single-tenant
    and folder path is retrieval *scope*, not authZ (see persistence/scope.py).

    The table has a single bootstrap row inserted by Store._seed and is never
    read. Safe to drop in a future migration once we're ready to touch the
    schema.
    """

    __tablename__ = "folder_grants"

    grant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    folder_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("folders.folder_id", ondelete="CASCADE"),
        index=True,
    )
    principal_id: Mapped[str] = mapped_column(String(128), index=True)  # user/group id
    principal_type: Mapped[str] = mapped_column(String(16))  # 'user' | 'group' | 'public'
    permission: Mapped[str] = mapped_column(String(16))  # 'view' | 'edit' | 'admin'
    inherit: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    granted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    granted_by: Mapped[str] = mapped_column(String(128), default="system", server_default="system")


class AuditLogRow(Base):
    """Append-only audit trail for all folder/document mutations.

    Phase 1: actor_id is always 'local'. Phase 2+: populated from the
    authenticated request context.
    """

    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class PendingFolderOp(Base):
    """
    Queue of folder rename/move/delete operations that exceed the async
    threshold (default 2000 affected chunks). The PG side is always
    updated synchronously when the op is enqueued so `documents.path`
    and `chunks.path` are immediately consistent. Chroma and Neo4j
    lag — their path metadata is updated by ``scripts/nightly_maintenance.py``
    during the maintenance window.

    Between enqueue and maintenance, retrieval on affected scopes uses
    an OR-fallback filter ("match new_path OR old_path") for Chroma and
    Neo4j so queries remain complete.
    """

    __tablename__ = "pending_folder_ops"

    op_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    op_type: Mapped[str] = mapped_column(String(16))  # rename | move | delete
    old_path: Mapped[str] = mapped_column(String(1024), index=True)
    new_path: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    affected_chunks: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )  # pending | running | done | failed
    queued_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    queued_by: Mapped[str] = mapped_column(String(128), default="local", server_default="local")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Auth (users / API tokens / web sessions)
# ---------------------------------------------------------------------------
# Design contract:
#   - Single-tenant today (one row in auth_users), but the schema is
#     multi-user-ready — adding more rows later needs no migration.
#   - Password hashes use argon2id; NEVER store plaintext.
#   - API tokens: we store sha256(raw_bearer); the plaintext is shown
#     exactly once at creation time and never retrievable.
#   - Sessions: opaque random ID, DB-backed, no TTL. Logout revokes the
#     current session; password change revokes all OTHER sessions.
# ---------------------------------------------------------------------------


class AuthUser(Base):
    """Single or multi tenant user record. Admin-bootstrapped on first
    boot when ``auth.enabled=true`` and this table is empty."""

    __tablename__ = "auth_users"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))  # argon2id
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="admin", server_default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthToken(Base):
    """Bearer token for CLI / SDK auth.

    ``token_hash`` = sha256(raw_bearer). We generate ``Forge_<32B base58>``
    tokens (~44 chars printable) and store only the hash. Revocation is
    a soft-delete via ``revoked_at``.
    """

    __tablename__ = "auth_tokens"

    token_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("auth_users.user_id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))  # human label
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hash_prefix: Mapped[str] = mapped_column(String(8))  # first 8 hex of hash, for UI fingerprint
    role: Mapped[str] = mapped_column(String(16), default="admin", server_default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthSession(Base):
    """Web-login session. Opaque ``session_id`` lives in the cookie.

    No TTL — session stays valid until either (a) the user logs out, or
    (b) their password is changed (all *other* sessions get revoked) or
    (c) an admin explicitly revokes via /sessions/{id}.
    """

    __tablename__ = "auth_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("auth_users.user_id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


class ChunkRow(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    doc_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        index=True,
    )
    parse_version: Mapped[int] = mapped_column(Integer, index=True)
    node_id: Mapped[str] = mapped_column(String(255), index=True)
    # Denormalized from documents.path for fast path-prefix retrieval
    # queries (`WHERE chunks.path LIKE '/legal/%'`). Kept in sync by
    # FolderService on rename / move — the relational update runs
    # synchronously inside the same transaction that updates
    # documents.path, so PG is always coherent.  Chroma / Neo4j get the
    # same path via their own store-level denormalization.
    path: Mapped[str] = mapped_column(String(1024), default="/", server_default="/", index=True)

    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(32))
    block_ids: Mapped[list] = mapped_column(JSON, default=list)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)
    y_sort: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")

    section_path: Mapped[list] = mapped_column(JSON, default=list)
    ancestor_node_ids: Mapped[list] = mapped_column(JSON, default=list)
    cross_ref_chunk_ids: Mapped[list] = mapped_column(JSON, default=list)

    # Inherited from owning ``TreeNode.role``. Drives KG-extraction
    # filtering (skip ``toc``/``index``/``bibliography``/``front_matter``)
    # and lets retrieval downweight supplementary content. ``main`` is
    # the body-content default.
    role: Mapped[str] = mapped_column(String(32), default="main", server_default="main")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_chunks_doc_version", "doc_id", "parse_version"),)
