"""
Unified relational store built on SQLAlchemy 2.0.

Replaces the previous per-backend PostgresStore / MySQLStore /
SQLiteStore. The public method signatures are kept compatible so
that IngestionWriter / retrieval / embedder callers don't need to
change.

Transactions:
    Use `with store.transaction(): ...` to group multiple writes.
    Calls outside a transaction block open a short-lived session
    per call. Transactions are re-entrant (nested calls pass
    through).
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from config import RelationalConfig

from .engine import make_engine
from .models import (
    Base,
    ChunkRow,
    Conversation,
    DocTreeRow,
    Document,
    File,
    LLMProvider,
    Message,
    ParsedBlock,
    QueryTrace,
    Setting,
)

log = logging.getLogger(__name__)


class Store:
    def __init__(self, cfg: RelationalConfig):
        self.cfg = cfg
        self.backend = cfg.backend
        self._engine = None
        self._sessionmaker: sessionmaker[Session] | None = None
        self._ctx_session: contextvars.ContextVar = contextvars.ContextVar("session", default=None)

    # =======================================================================
    # Lifecycle
    # =======================================================================

    def connect(self) -> None:
        if self._engine is not None:
            return
        self._engine = make_engine(self.cfg)
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )
        # Mount OTel SQL instrumentation if observability is enabled.
        # Safe no-op if the tracer provider is the default (tests).
        try:
            from config.observability import instrument_sqlalchemy

            instrument_sqlalchemy(self._engine)
        except Exception as e:
            log.debug("SQLAlchemy OTel instrumentation skipped: %s", e)
        log.info("Store connected (backend=%s)", self.backend)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._sessionmaker = None

    def ensure_schema(self, *, with_vector: bool = False, embedding_dim: int = 1536) -> None:
        """
        Create all tables if missing, then migrate existing tables
        by adding any missing columns. Vector columns are NOT handled
        here -- pgvector's `chunks.embedding` column is added by
        PgvectorStore via a dedicated ALTER TABLE so the relational
        schema stays dialect-agnostic.
        """
        del with_vector, embedding_dim  # reserved for symmetry
        assert self._engine is not None
        Base.metadata.create_all(self._engine)
        self._migrate_add_columns()
        self._seed_system_folders()

    def _seed_system_folders(self) -> None:
        """
        Ensure the two system folders (__root__, __trash__) exist.
        Both the alembic migration and the test-mode Base.metadata.create_all()
        rely on this — the migration runs this SQL explicitly, but tests use
        create_all() which doesn't execute data seeds.
        """
        from sqlalchemy import text

        assert self._engine is not None
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO folders (folder_id, path, path_lower, parent_id,
                                         name, is_system, metadata_json)
                    SELECT '__root__', '/', '/', NULL, 'Root', :tt, '{}'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM folders WHERE folder_id = '__root__'
                    )
                    """
                ),
                {"tt": True},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO folders (folder_id, path, path_lower, parent_id,
                                         name, is_system, metadata_json)
                    SELECT '__trash__', '/__trash__', '/__trash__', '__root__',
                           'Trash', :tt, '{}'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM folders WHERE folder_id = '__trash__'
                    )
                    """
                ),
                {"tt": True},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO folder_grants (grant_id, folder_id, principal_id,
                                               principal_type, permission,
                                               inherit, granted_by)
                    SELECT '__bootstrap__', '__root__', 'local', 'user',
                           'admin', :inh, 'system'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM folder_grants WHERE grant_id = '__bootstrap__'
                    )
                    """
                ),
                {"inh": True},
            )

    # Column renames: (table_name, old_col, new_col)
    _COLUMN_RENAMES: list[tuple[str, str, str]] = [
        ("documents", "source_path", "filename"),
    ]

    def _migrate_add_columns(self) -> None:
        """Add missing columns and rename old columns in existing tables."""
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text

        inspector = sa_inspect(self._engine)

        # Phase 1: rename columns (SQLite 3.25+ supports ALTER TABLE RENAME COLUMN)
        for table_name, old_col, new_col in self._COLUMN_RENAMES:
            if not inspector.has_table(table_name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table_name)}
            if old_col in existing and new_col not in existing:
                ddl = f"ALTER TABLE {table_name} RENAME COLUMN {old_col} TO {new_col}"
                try:
                    with self._engine.begin() as conn:
                        conn.execute(text(ddl))
                    log.info("migrated rename: %s", ddl)
                except Exception:
                    log.warning("rename failed: %s → %s.%s", old_col, table_name, new_col)

        # Phase 2: add missing columns
        # Re-inspect after renames
        inspector = sa_inspect(self._engine)
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(self._engine.dialect)
                # Always add as nullable for safety with existing rows
                default = ""
                if col.server_default is not None:
                    default = f" DEFAULT {col.server_default.arg.text if hasattr(col.server_default.arg, 'text') else repr(col.server_default.arg)}"
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}{default}"
                try:
                    with self._engine.begin() as conn:
                        conn.execute(text(ddl))
                    log.info("migrated: %s", ddl)
                except OperationalError:
                    pass  # column may already exist in a race
                except Exception as e:
                    log.warning("migration failed for column %s: %s", col.name, e)

        # Phase 3: fix stale status combos from older code
        self._fix_stale_statuses()

    def _fix_stale_statuses(self) -> None:
        """Fix documents that reached 'ready' but left enrich/embed as 'pending'."""
        from sqlalchemy import text

        fixes = [
            # ready but enrich never ran → mark skipped
            ("UPDATE documents SET enrich_status = 'skipped' WHERE status = 'ready' AND enrich_status = 'pending'"),
            # ready but embed still says pending → already done
            ("UPDATE documents SET embed_status = 'done' WHERE status = 'ready' AND embed_status = 'pending'"),
        ]
        try:
            with self._engine.begin() as conn:
                for sql in fixes:
                    result = conn.execute(text(sql))
                    if result.rowcount:
                        log.info("status fix: %s  (%d rows)", sql[:60], result.rowcount)
        except Exception:
            log.warning("_fix_stale_statuses failed", exc_info=True)

    def recover_stuck_documents(self) -> list[dict]:
        """
        On startup, reset documents stuck in intermediate states back to
        ``pending`` so they can be re-queued.  A document is "stuck" if its
        status is one of the in-flight phases (processing, converting,
        parsing, parsed, structuring, embedding) — these cannot survive a
        process restart because worker threads are gone.

        Returns a list of ``{doc_id, file_id}`` dicts for the recovered rows
        so the caller can re-submit them to the ingestion queue.
        """
        from sqlalchemy import text

        stuck_statuses = (
            "processing",
            "converting",
            "parsing",
            "parsed",
            "structuring",
            "embedding",
        )
        placeholders = ", ".join(f"'{s}'" for s in stuck_statuses)
        sql = f"UPDATE documents SET status = 'pending' WHERE status IN ({placeholders})"
        try:
            with self._engine.begin() as conn:
                result = conn.execute(text(sql))
                if result.rowcount:
                    log.info("recovered %d stuck document(s) → pending", result.rowcount)
                else:
                    return []
            # Fetch the recovered rows to get doc_id + file_id
            with self._session() as s:
                rows = s.execute(
                    select(Document.doc_id, Document.file_id)
                    .where(Document.status == "pending")
                    .where(Document.file_id.isnot(None))
                ).all()
                return [{"doc_id": r.doc_id, "file_id": r.file_id} for r in rows]
        except Exception:
            log.warning("recover_stuck_documents failed", exc_info=True)
            return []

    # =======================================================================
    # Session / transaction management
    # =======================================================================

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """
        Multi-statement transaction. Re-entrant: nested calls join
        the outer session.
        """
        if self._sessionmaker is None:
            raise RuntimeError("store not connected")
        existing = self._ctx_session.get()
        if existing is not None:
            yield existing
            return

        session = self._sessionmaker()
        self._ctx_session.set(session)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            self._ctx_session.set(None)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Yield either the active transaction session or a short-lived one."""
        existing = self._ctx_session.get()
        if existing is not None:
            yield existing
            return
        if self._sessionmaker is None:
            raise RuntimeError("store not connected")
        session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # =======================================================================
    # Documents
    # =======================================================================

    def upsert_document(
        self,
        *,
        doc_id: str,
        filename: str,
        format: str,
        active_parse_version: int,
        profile_json: dict,
        trace_json: dict,
        metadata_json: dict | None = None,
        file_id: str | None = None,
        pages_json: list | None = None,
    ) -> None:
        with self._session() as s:
            row = s.get(Document, doc_id)
            if row is None:
                row = Document(
                    doc_id=doc_id,
                    file_id=file_id,
                    filename=filename,
                    format=format,
                    active_parse_version=active_parse_version,
                    metadata_json=metadata_json or {},
                    doc_profile_json=profile_json,
                    parse_trace_json=trace_json,
                    pages_json=pages_json,
                )
                s.add(row)
            else:
                row.file_id = file_id if file_id is not None else row.file_id
                row.filename = filename
                row.format = format
                row.active_parse_version = active_parse_version
                if metadata_json is not None:
                    row.metadata_json = metadata_json
                row.doc_profile_json = profile_json
                row.parse_trace_json = trace_json
                row.pages_json = pages_json

    def create_document_placeholder(
        self,
        *,
        doc_id: str,
        file_id: str,
        filename: str,
        format: str,
        status: str = "pending",
        folder_id: str = "__root__",
        path: str = "/",
    ) -> None:
        """
        Create a minimal document row so the frontend can see it
        immediately after upload, before ingestion starts.
        If the document already exists this is a no-op.

        ``folder_id`` + ``path`` place the new doc under a specific folder
        (caller should pre-compute a collision-free path via
        ``folder_service.unique_document_path``).
        """
        with self._session() as s:
            existing = s.get(Document, doc_id)
            if existing is not None:
                return
            row = Document(
                doc_id=doc_id,
                file_id=file_id,
                filename=filename,
                format=format,
                active_parse_version=1,
                metadata_json={},
                status=status,
                folder_id=folder_id,
                path=path,
            )
            s.add(row)

    def update_document_status(self, doc_id: str, **fields) -> None:
        """Update arbitrary status fields on a document."""
        with self._session() as s:
            row = s.get(Document, doc_id)
            if row is None:
                return
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)

    def get_document(self, doc_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(Document, doc_id)
            return _doc_to_dict(row) if row else None

    def delete_document(self, doc_id: str) -> None:
        with self._session() as s:
            row = s.get(Document, doc_id)
            if row is not None:
                s.delete(row)

    def list_document_ids(self) -> list[str]:
        with self._session() as s:
            rows = s.execute(select(Document.doc_id).order_by(Document.doc_id)).all()
            return [r[0] for r in rows]

    # =======================================================================
    # Versioning
    # =======================================================================

    def delete_parse_version(self, doc_id: str, parse_version: int) -> None:
        with self._session() as s:
            s.execute(
                delete(ChunkRow).where(
                    ChunkRow.doc_id == doc_id,
                    ChunkRow.parse_version == parse_version,
                )
            )
            s.execute(
                delete(DocTreeRow).where(
                    DocTreeRow.doc_id == doc_id,
                    DocTreeRow.parse_version == parse_version,
                )
            )
            s.execute(
                delete(ParsedBlock).where(
                    ParsedBlock.doc_id == doc_id,
                    ParsedBlock.parse_version == parse_version,
                )
            )

    # =======================================================================
    # Blocks
    # =======================================================================

    def insert_blocks(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self._session() as s:
            s.execute(insert(ParsedBlock), rows)

    def get_blocks(self, doc_id: str, parse_version: int) -> list[dict]:
        with self._session() as s:
            rows = (
                s.execute(
                    select(ParsedBlock)
                    .where(
                        ParsedBlock.doc_id == doc_id,
                        ParsedBlock.parse_version == parse_version,
                    )
                    .order_by(ParsedBlock.page_no, ParsedBlock.seq)
                )
                .scalars()
                .all()
            )
            return [_block_to_dict(r) for r in rows]

    def get_block(self, block_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(ParsedBlock, block_id)
            return _block_to_dict(row) if row else None

    # =======================================================================
    # Trees
    # =======================================================================

    def save_tree(
        self,
        *,
        doc_id: str,
        parse_version: int,
        root_id: str,
        quality_score: float,
        generation_method: str,
        tree_json: dict,
    ) -> None:
        with self._session() as s:
            row = s.execute(
                select(DocTreeRow).where(
                    DocTreeRow.doc_id == doc_id,
                    DocTreeRow.parse_version == parse_version,
                )
            ).scalar_one_or_none()
            if row is None:
                s.add(
                    DocTreeRow(
                        doc_id=doc_id,
                        parse_version=parse_version,
                        root_id=root_id,
                        quality_score=quality_score,
                        generation_method=generation_method,
                        tree_json=tree_json,
                    )
                )
            else:
                row.root_id = root_id
                row.quality_score = quality_score
                row.generation_method = generation_method
                row.tree_json = tree_json

    def load_tree(self, doc_id: str, parse_version: int) -> dict | None:
        with self._session() as s:
            row = s.execute(
                select(DocTreeRow.tree_json).where(
                    DocTreeRow.doc_id == doc_id,
                    DocTreeRow.parse_version == parse_version,
                )
            ).scalar_one_or_none()
            return row

    # =======================================================================
    # Chunks
    # =======================================================================

    def insert_chunks(self, rows: list[dict]) -> None:
        """
        Insert chunk rows. The ``path`` column is denormalized from
        the owning ``documents.path`` at write time — callers don't need
        to know or supply it. This keeps ingestion code path-agnostic
        while letting retrieval queries filter natively on chunks.path.
        """
        if not rows:
            return
        # Auto-fill chunks.path from documents.path for any row that
        # didn't supply one. Callers that already know the path (bulk
        # rename, restoration, etc.) can pass it explicitly to skip
        # the lookup.
        need_path = [r for r in rows if not r.get("path")]
        if need_path:
            from sqlalchemy import select as _sel

            doc_ids = list({r["doc_id"] for r in need_path if r.get("doc_id")})
            path_by_doc: dict[str, str] = {}
            if doc_ids:
                with self._session() as s:
                    for did, p in s.execute(
                        _sel(Document.doc_id, Document.path).where(
                            Document.doc_id.in_(doc_ids)
                        )
                    ):
                        path_by_doc[did] = p or "/"
            for r in need_path:
                r["path"] = path_by_doc.get(r.get("doc_id", ""), "/")
        with self._session() as s:
            s.execute(insert(ChunkRow), rows)

    def get_chunks(self, doc_id: str, parse_version: int) -> list[dict]:
        with self._session() as s:
            rows = (
                s.execute(
                    select(ChunkRow)
                    .where(
                        ChunkRow.doc_id == doc_id,
                        ChunkRow.parse_version == parse_version,
                    )
                    .order_by(ChunkRow.page_start, ChunkRow.y_sort)
                )
                .scalars()
                .all()
            )
            return [_chunk_to_dict(r) for r in rows]

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        if not chunk_ids:
            return []
        results = []
        with self._session() as s:
            for i in range(0, len(chunk_ids), 500):
                batch = chunk_ids[i : i + 500]
                rows = s.execute(select(ChunkRow).where(ChunkRow.chunk_id.in_(batch))).scalars().all()
                results.extend(_chunk_to_dict(r) for r in rows)
        return results

    def find_chunk_by_block_id(self, doc_id: str, parse_version: int, block_id: str) -> dict | None:
        """Find the chunk that contains a given block_id (JSONB @> query)."""
        from sqlalchemy import type_coerce
        from sqlalchemy.dialects.postgresql import JSONB

        with self._session() as s:
            row = (
                s.execute(
                    select(ChunkRow)
                    .where(
                        ChunkRow.doc_id == doc_id,
                        ChunkRow.parse_version == parse_version,
                        type_coerce(ChunkRow.block_ids, JSONB).contains([block_id]),
                    )
                    .limit(1)
                )
                .scalars()
                .first()
            )
            return _chunk_to_dict(row) if row else None

    def get_chunks_by_node_ids(self, node_ids: list[str]) -> list[dict]:
        if not node_ids:
            return []
        results = []
        with self._session() as s:
            for i in range(0, len(node_ids), 500):
                batch = node_ids[i : i + 500]
                rows = (
                    s.execute(
                        select(ChunkRow)
                        .where(ChunkRow.node_id.in_(batch))
                        .order_by(ChunkRow.page_start, ChunkRow.y_sort)
                    )
                    .scalars()
                    .all()
                )
                results.extend(_chunk_to_dict(r) for r in rows)
        return results

    # =======================================================================
    # Files
    # =======================================================================

    def insert_file(self, record: dict) -> None:
        with self._session() as s:
            s.add(File(**record))

    def get_file(self, file_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(File, file_id)
            return _file_to_dict(row) if row else None

    def get_file_by_hash(self, content_hash: str) -> dict | None:
        """Return the most recent file row with the given hash, if any."""
        with self._session() as s:
            row = (
                s.execute(select(File).where(File.content_hash == content_hash).order_by(File.uploaded_at.desc()))
                .scalars()
                .first()
            )
            return _file_to_dict(row) if row else None

    # =======================================================================
    # Query Traces
    # =======================================================================

    def insert_trace(self, record: dict) -> None:
        with self._session() as s:
            s.add(QueryTrace(**record))

    def get_trace(self, trace_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(QueryTrace, trace_id)
            return _trace_to_dict(row) if row else None

    def list_traces(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        with self._session() as s:
            rows = (
                s.execute(select(QueryTrace).order_by(QueryTrace.timestamp.desc()).limit(limit).offset(offset))
                .scalars()
                .all()
            )
            return [_trace_to_dict(r) for r in rows]

    # =======================================================================
    # Conversations
    # =======================================================================

    def create_conversation(self, record: dict) -> None:
        with self._session() as s:
            s.add(Conversation(**record))

    def get_conversation(self, conversation_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(Conversation, conversation_id)
            return _conversation_to_dict(row) if row else None

    def update_conversation(self, conversation_id: str, **updates) -> None:
        with self._session() as s:
            row = s.get(Conversation, conversation_id)
            if row:
                for k, v in updates.items():
                    if hasattr(row, k):
                        setattr(row, k, v)

    def list_conversations(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._session() as s:
            rows = (
                s.execute(select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit).offset(offset))
                .scalars()
                .all()
            )
            return [_conversation_to_dict(r) for r in rows]

    def count_conversations(self) -> int:
        with self._session() as s:
            from sqlalchemy import func as sa_func

            return s.execute(select(sa_func.count()).select_from(Conversation)).scalar() or 0

    def delete_conversation(self, conversation_id: str) -> None:
        with self._session() as s:
            row = s.get(Conversation, conversation_id)
            if row:
                s.delete(row)

    # =======================================================================
    # Messages
    # =======================================================================

    def add_message(self, record: dict) -> None:
        with self._session() as s:
            s.add(Message(**record))

    def get_messages(self, conversation_id: str, *, limit: int = 100) -> list[dict]:
        with self._session() as s:
            rows = (
                s.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.created_at.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [_message_to_dict(r) for r in rows]

    def count_messages(self, conversation_id: str) -> int:
        with self._session() as s:
            from sqlalchemy import func as sa_func

            return (
                s.execute(
                    select(sa_func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
                ).scalar()
                or 0
            )

    # =======================================================================
    # Settings
    # =======================================================================

    def upsert_setting(self, record: dict) -> None:
        with self._session() as s:
            row = s.get(Setting, record["key"])
            if row is None:
                s.add(Setting(**record))
            else:
                for k, v in record.items():
                    if k != "key":
                        setattr(row, k, v)

    def get_setting(self, key: str) -> dict | None:
        with self._session() as s:
            row = s.get(Setting, key)
            return _setting_to_dict(row) if row else None

    def get_settings_by_group(self, group_name: str) -> list[dict]:
        with self._session() as s:
            rows = (
                s.execute(select(Setting).where(Setting.group_name == group_name).order_by(Setting.key)).scalars().all()
            )
            return [_setting_to_dict(r) for r in rows]

    def get_all_settings(self) -> list[dict]:
        with self._session() as s:
            rows = s.execute(select(Setting).order_by(Setting.group_name, Setting.key)).scalars().all()
            return [_setting_to_dict(r) for r in rows]

    def delete_setting(self, key: str) -> None:
        with self._session() as s:
            row = s.get(Setting, key)
            if row is not None:
                s.delete(row)

    def bulk_upsert_settings(self, records: list[dict]) -> None:
        with self._session() as s:
            for record in records:
                row = s.get(Setting, record["key"])
                if row is None:
                    s.add(Setting(**record))
                else:
                    for k, v in record.items():
                        if k != "key":
                            setattr(row, k, v)

    def delete_trace(self, trace_id: str) -> None:
        with self._session() as s:
            row = s.get(QueryTrace, trace_id)
            if row is not None:
                s.delete(row)

    def list_files(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._session() as s:
            rows = s.execute(select(File).order_by(File.uploaded_at.desc()).limit(limit).offset(offset)).scalars().all()
            return [_file_to_dict(r) for r in rows]

    def count_files(self) -> int:
        with self._session() as s:
            from sqlalchemy import func as sa_func

            return s.execute(select(sa_func.count()).select_from(File)).scalar() or 0

    def delete_file(self, file_id: str) -> None:
        with self._session() as s:
            row = s.get(File, file_id)
            if row is not None:
                s.delete(row)

    # =======================================================================
    # Documents - extended
    # =======================================================================

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        status: str | None = None,
        folder_id: str | None = None,
        path_prefix: str | None = None,
    ) -> list[dict]:
        """
        ``folder_id`` and ``path_prefix`` are mutually exclusive: pass the
        former for exact-folder (direct-children) match using the folder_id
        index, or the latter for subtree prefix match on ``Document.path``.
        The route layer picks one based on the caller's ``recursive`` flag.
        """
        with self._session() as s:
            from sqlalchemy import or_

            q = select(Document).order_by(Document.updated_at.desc())
            if search:
                q = q.where(
                    or_(
                        Document.doc_id.contains(search),
                        Document.filename.contains(search),
                    )
                )
            if status:
                if "," in status:
                    q = q.where(Document.status.in_([st.strip() for st in status.split(",")]))
                else:
                    q = q.where(Document.status == status)
            if folder_id is not None:
                q = q.where(Document.folder_id == folder_id)
            if path_prefix is not None:
                p = path_prefix.rstrip("/") or "/"
                if p == "/":
                    # Root subtree = everything (no filter needed); skip
                    pass
                else:
                    q = q.where(or_(Document.path == p, Document.path.like(p + "/%")))
            rows = s.execute(q.limit(limit).offset(offset)).scalars().all()
            return [_doc_to_dict(r) for r in rows]

    def count_documents(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        folder_id: str | None = None,
        path_prefix: str | None = None,
    ) -> int:
        with self._session() as s:
            from sqlalchemy import func as sa_func
            from sqlalchemy import or_

            q = select(sa_func.count()).select_from(Document)
            if search:
                q = q.where(
                    or_(
                        Document.doc_id.contains(search),
                        Document.filename.contains(search),
                    )
                )
            if status:
                if "," in status:
                    q = q.where(Document.status.in_([st.strip() for st in status.split(",")]))
                else:
                    q = q.where(Document.status == status)
            if folder_id is not None:
                q = q.where(Document.folder_id == folder_id)
            if path_prefix is not None:
                p = path_prefix.rstrip("/") or "/"
                if p != "/":
                    q = q.where(or_(Document.path == p, Document.path.like(p + "/%")))
            return s.execute(q).scalar() or 0

    def get_blocks_paginated(self, doc_id: str, parse_version: int, *, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._session() as s:
            rows = (
                s.execute(
                    select(ParsedBlock)
                    .where(ParsedBlock.doc_id == doc_id, ParsedBlock.parse_version == parse_version)
                    .order_by(ParsedBlock.page_no, ParsedBlock.seq)
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
            return [_block_to_dict(r) for r in rows]

    def count_blocks(self, doc_id: str, parse_version: int) -> int:
        with self._session() as s:
            from sqlalchemy import func as sa_func

            return (
                s.execute(
                    select(sa_func.count())
                    .select_from(ParsedBlock)
                    .where(ParsedBlock.doc_id == doc_id, ParsedBlock.parse_version == parse_version)
                ).scalar()
                or 0
            )

    def get_chunks_paginated(self, doc_id: str, parse_version: int, *, limit: int = 100, offset: int = 0) -> list[dict]:
        with self._session() as s:
            rows = (
                s.execute(
                    select(ChunkRow)
                    .where(ChunkRow.doc_id == doc_id, ChunkRow.parse_version == parse_version)
                    .order_by(ChunkRow.page_start, ChunkRow.y_sort)
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
            return [_chunk_to_dict(r) for r in rows]

    def chunk_position(self, doc_id: str, parse_version: int, chunk_id: str) -> int:
        """Return 0-based position of chunk_id in the sorted chunk list, or -1."""
        with self._session() as s:
            target = s.get(ChunkRow, chunk_id)
            if not target or target.doc_id != doc_id:
                return -1
            from sqlalchemy import and_, or_
            from sqlalchemy import func as sa_func

            # Count chunks that sort before this one
            # order_by(page_start, chunk_id)
            cnt = (
                s.execute(
                    select(sa_func.count())
                    .select_from(ChunkRow)
                    .where(
                        ChunkRow.doc_id == doc_id,
                        ChunkRow.parse_version == parse_version,
                        or_(
                            ChunkRow.page_start < target.page_start,
                            and_(
                                ChunkRow.page_start == target.page_start,
                                ChunkRow.chunk_id < target.chunk_id,
                            ),
                        ),
                    )
                ).scalar()
                or 0
            )
            return cnt

    def count_chunks(self, doc_id: str, parse_version: int) -> int:
        with self._session() as s:
            from sqlalchemy import func as sa_func

            return (
                s.execute(
                    select(sa_func.count())
                    .select_from(ChunkRow)
                    .where(ChunkRow.doc_id == doc_id, ChunkRow.parse_version == parse_version)
                ).scalar()
                or 0
            )

    def get_chunk(self, chunk_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(ChunkRow, chunk_id)
            return _chunk_to_dict(row) if row else None

    def count_traces(self) -> int:
        with self._session() as s:
            from sqlalchemy import func as sa_func

            return s.execute(select(sa_func.count()).select_from(QueryTrace)).scalar() or 0

    # =======================================================================
    # LLM Providers
    # =======================================================================

    def upsert_llm_provider(self, record: dict) -> None:
        with self._session() as s:
            row = s.get(LLMProvider, record["id"])
            if row is None:
                s.add(LLMProvider(**record))
            else:
                for k, v in record.items():
                    if k != "id":
                        setattr(row, k, v)

    def get_llm_provider(self, provider_id: str) -> dict | None:
        with self._session() as s:
            row = s.get(LLMProvider, provider_id)
            return _llm_provider_to_dict(row) if row else None

    def get_llm_provider_by_name(self, name: str) -> dict | None:
        with self._session() as s:
            row = s.execute(select(LLMProvider).where(LLMProvider.name == name)).scalars().first()
            return _llm_provider_to_dict(row) if row else None

    def list_llm_providers(self, provider_type: str | None = None) -> list[dict]:
        with self._session() as s:
            q = select(LLMProvider).order_by(LLMProvider.provider_type, LLMProvider.name)
            if provider_type:
                q = q.where(LLMProvider.provider_type == provider_type)
            rows = s.execute(q).scalars().all()
            return [_llm_provider_to_dict(r) for r in rows]

    def delete_llm_provider(self, provider_id: str) -> None:
        with self._session() as s:
            row = s.get(LLMProvider, provider_id)
            if row is not None:
                s.delete(row)

    def count_llm_providers(self) -> int:
        with self._session() as s:
            from sqlalchemy import func as sa_func

            return s.execute(select(sa_func.count()).select_from(LLMProvider)).scalar() or 0


# ---------------------------------------------------------------------------
# Row -> dict helpers
# ---------------------------------------------------------------------------


def _doc_to_dict(row: Document) -> dict:
    return {
        "doc_id": row.doc_id,
        "file_id": row.file_id,
        "pdf_file_id": getattr(row, "pdf_file_id", None),
        "filename": row.filename,
        "format": row.format,
        "active_parse_version": row.active_parse_version,
        "metadata_json": row.metadata_json,
        "doc_profile_json": row.doc_profile_json,
        "parse_trace_json": row.parse_trace_json,
        "status": row.status,
        "embed_status": row.embed_status,
        "embed_provider_id": row.embed_provider_id,
        "embed_model": row.embed_model,
        "embed_at": row.embed_at,
        "enrich_status": row.enrich_status,
        "enrich_provider_id": row.enrich_provider_id,
        "enrich_model": row.enrich_model,
        "enrich_summary_count": row.enrich_summary_count,
        "enrich_image_count": row.enrich_image_count,
        "enrich_at": row.enrich_at,
        "parse_started_at": row.parse_started_at,
        "parse_completed_at": row.parse_completed_at,
        "structure_started_at": row.structure_started_at,
        "structure_completed_at": row.structure_completed_at,
        "enrich_started_at": row.enrich_started_at,
        "embed_started_at": row.embed_started_at,
        "kg_status": row.kg_status,
        "kg_entity_count": row.kg_entity_count,
        "kg_relation_count": row.kg_relation_count,
        "kg_started_at": row.kg_started_at,
        "kg_completed_at": row.kg_completed_at,
        "kg_provider_id": row.kg_provider_id,
        "kg_model": row.kg_model,
        "tree_navigable": getattr(row, "tree_navigable", None),
        "tree_quality": getattr(row, "tree_quality", None),
        "tree_method": getattr(row, "tree_method", None),
        "error_message": getattr(row, "error_message", None),
        "pages_json": row.pages_json,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        # Folder membership — needed by workspace UI + retrieval path_filter
        "folder_id": getattr(row, "folder_id", None),
        "path": getattr(row, "path", None),
    }


def _block_to_dict(row: ParsedBlock) -> dict:
    return {
        "block_id": row.block_id,
        "doc_id": row.doc_id,
        "parse_version": row.parse_version,
        "page_no": row.page_no,
        "seq": row.seq,
        "bbox_x0": row.bbox_x0,
        "bbox_y0": row.bbox_y0,
        "bbox_x1": row.bbox_x1,
        "bbox_y1": row.bbox_y1,
        "type": row.type,
        "level": row.level,
        "text": row.text,
        "confidence": row.confidence,
        "table_html": row.table_html,
        "table_markdown": row.table_markdown,
        "figure_storage_key": row.figure_storage_key,
        "figure_mime": row.figure_mime,
        "figure_caption": row.figure_caption,
        "formula_latex": row.formula_latex,
        "excluded": row.excluded,
        "excluded_reason": row.excluded_reason,
        "caption_of": row.caption_of,
        "cross_ref_targets": list(row.cross_ref_targets or []),
    }


def _chunk_to_dict(row: ChunkRow) -> dict:
    return {
        "chunk_id": row.chunk_id,
        "doc_id": row.doc_id,
        "parse_version": row.parse_version,
        "node_id": row.node_id,
        "content": row.content,
        "content_type": row.content_type,
        "block_ids": list(row.block_ids or []),
        "page_start": row.page_start,
        "page_end": row.page_end,
        "token_count": row.token_count,
        "y_sort": row.y_sort or 0.0,
        "section_path": list(row.section_path or []),
        "ancestor_node_ids": list(row.ancestor_node_ids or []),
        "cross_ref_chunk_ids": list(row.cross_ref_chunk_ids or []),
    }


def _conversation_to_dict(row: Conversation) -> dict:
    return {
        "conversation_id": row.conversation_id,
        "title": row.title,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "metadata_json": row.metadata_json or {},
    }


def _message_to_dict(row: Message) -> dict:
    return {
        "message_id": row.message_id,
        "conversation_id": row.conversation_id,
        "role": row.role,
        "content": row.content,
        "trace_id": row.trace_id,
        "citations_json": row.citations_json,
        "created_at": row.created_at,
    }


def _setting_to_dict(row: Setting) -> dict:
    return {
        "key": row.key,
        "value_json": row.value_json,
        "group_name": row.group_name,
        "label": row.label,
        "description": row.description,
        "value_type": row.value_type,
        "enum_options": row.enum_options,
        "updated_at": row.updated_at,
    }


def _trace_to_dict(row: QueryTrace) -> dict:
    return {
        "trace_id": row.trace_id,
        "query": row.query,
        "timestamp": row.timestamp,
        "total_ms": row.total_ms,
        "total_llm_ms": row.total_llm_ms,
        "total_llm_calls": row.total_llm_calls,
        "answer_text": row.answer_text,
        "answer_model": row.answer_model,
        "finish_reason": row.finish_reason,
        "citations_used": list(row.citations_used or []),
        "trace_json": row.trace_json or {},
        "metadata_json": row.metadata_json or {},
    }


def _file_to_dict(row: File) -> dict:
    return {
        "file_id": row.file_id,
        "content_hash": row.content_hash,
        "storage_key": row.storage_key,
        "original_name": row.original_name,
        "display_name": row.display_name,
        "size_bytes": row.size_bytes,
        "mime_type": row.mime_type,
        "uploaded_at": row.uploaded_at,
        "metadata_json": row.metadata_json or {},
    }


def _llm_provider_to_dict(row: LLMProvider) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "api_base": row.api_base,
        "model_name": row.model_name,
        "api_key": row.api_key,
        "is_default": row.is_default,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
