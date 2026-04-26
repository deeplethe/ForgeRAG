"""Denormalize path onto chunks + pending_folder_ops queue.

Revision ID: 20260418_chunks_path
Revises: 20260418_folder_tree
Create Date: 2026-04-18

Phase-1 refinement: we previously kept path only on documents and used
a Python snapshot set (allowed_doc_ids) to scope retrieval. That scales
poorly when a single folder holds tens of thousands of documents and
the set has to be pushed to Chroma / Neo4j.

This migration adopts the **denormalized path** architecture:

  1. Every `chunks` row carries its own `path` column, kept in sync with
     its document's path by FolderService on rename/move. pgvector can
     then filter natively with `WHERE path LIKE '/legal/%'`.

  2. A new `pending_folder_ops` queue stores rename/move operations
     whose chunk-count exceeds the async threshold (default 2000). These
     are consumed by the nightly maintenance script and propagated to
     Chroma / Neo4j.

Chroma and Neo4j denormalization (adding path to their metadata) is not
a relational DDL concern and lives in their respective store modules.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# ─── Alembic identifiers ───────────────────────────────────────────────
revision = "20260418_chunks_path"
down_revision = "20260418_folder_tree"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _add_chunks_path_column()
    _create_pending_folder_ops_table()
    _backfill_chunks_path()


def downgrade() -> None:
    op.drop_table("pending_folder_ops")
    with op.batch_alter_table("chunks") as batch:
        batch.drop_index("ix_chunks_path")
        batch.drop_index("ix_chunks_path_prefix")
        batch.drop_column("path")


# ───────────────────────────────────────────────────────────────────────
# Step 1: chunks.path
# ───────────────────────────────────────────────────────────────────────


def _add_chunks_path_column() -> None:
    with op.batch_alter_table("chunks") as batch:
        batch.add_column(sa.Column("path", sa.String(1024), nullable=False, server_default="/"))
    op.create_index("ix_chunks_path", "chunks", ["path"])
    # Postgres-only: prefix index for `WHERE path LIKE '/x/%'` queries.
    # SQLite (test fixtures) ignores this — create it conditionally.
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_path_prefix ON chunks (path varchar_pattern_ops)")


# ───────────────────────────────────────────────────────────────────────
# Step 2: pending_folder_ops queue
# ───────────────────────────────────────────────────────────────────────


def _create_pending_folder_ops_table() -> None:
    op.create_table(
        "pending_folder_ops",
        sa.Column("op_id", sa.String(32), primary_key=True),
        # "rename" | "move" | "delete"
        sa.Column("op_type", sa.String(16), nullable=False),
        # Old/new path. For deletes, new_path is NULL.
        sa.Column("old_path", sa.String(1024), nullable=False),
        sa.Column("new_path", sa.String(1024), nullable=True),
        # Number of chunks affected at enqueue time; informs the async
        # threshold router and is shown to the user in the UI.
        sa.Column("affected_chunks", sa.Integer, nullable=False),
        # "pending" | "running" | "done" | "failed"
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("queued_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("queued_by", sa.String(128), nullable=False, server_default="local"),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("error_msg", sa.Text, nullable=True),
    )
    op.create_index("ix_pending_ops_status", "pending_folder_ops", ["status", "queued_at"])
    op.create_index(
        "ix_pending_ops_old_path",
        "pending_folder_ops",
        ["old_path"],
    )
    op.create_index(
        "ix_pending_ops_new_path",
        "pending_folder_ops",
        ["new_path"],
    )


# ───────────────────────────────────────────────────────────────────────
# Step 3: backfill chunks.path from documents.path
# ───────────────────────────────────────────────────────────────────────


def _backfill_chunks_path() -> None:
    """Populate chunks.path = the owning document.path. Runs as a single
    correlated UPDATE; for 1M chunks this completes in tens of seconds."""
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE chunks AS c
            SET path = d.path
            FROM documents AS d
            WHERE c.doc_id = d.doc_id
              AND c.path = '/'
            """
        )
        if conn.dialect.name == "postgresql"
        else sa.text(
            # SQLite path: correlated subquery (no UPDATE ... FROM)
            """
            UPDATE chunks SET path = (
                SELECT path FROM documents WHERE documents.doc_id = chunks.doc_id
            )
            WHERE path = '/'
              AND EXISTS (
                SELECT 1 FROM documents WHERE documents.doc_id = chunks.doc_id
              )
            """
        )
    )
