"""Add folder tree, folder_grants, audit_log.

Revision ID: 20260418_folder_tree
Revises:
Create Date: 2026-04-18

This migration introduces the "folder as permission basis" model:

    folders            — tree of folders, stable folder_id + cached path
    folder_grants      — future permission grants (Phase 2)
    audit_log          — append-only mutation log

plus extends `documents`:
    folder_id          — stable link to folder (default __root__)
    path               — cached denormalization (default '/')
    trashed_metadata   — JSON for restore info when soft-deleted

System folders __root__ and __trash__ are seeded here.
Existing documents are backfilled into __root__ with path='/<filename>'.
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op


# ─── Alembic identifiers ───────────────────────────────────────────────
revision = "20260418_folder_tree"
down_revision = None   # stand-alone — this repo has no prior migration
branch_labels = None
depends_on = None


# ─── Schema constants ──────────────────────────────────────────────────
ROOT_FOLDER_ID = "__root__"
TRASH_FOLDER_ID = "__trash__"
ROOT_PATH = "/"
TRASH_PATH = "/__trash__"


def upgrade() -> None:
    _create_folders_table()
    _create_folder_grants_table()
    _create_audit_log_table()
    _extend_documents_table()
    _seed_system_folders()
    _backfill_existing_documents()
    _bootstrap_root_grant()


def downgrade() -> None:
    # Documents: drop the three new columns
    with op.batch_alter_table("documents") as batch:
        batch.drop_index("ix_documents_path")
        batch.drop_column("trashed_metadata")
        batch.drop_column("path")
        batch.drop_column("folder_id")

    op.drop_table("audit_log")
    op.drop_table("folder_grants")
    op.drop_table("folders")


# ───────────────────────────────────────────────────────────────────────
# Step 1: folders
# ───────────────────────────────────────────────────────────────────────


def _create_folders_table() -> None:
    op.create_table(
        "folders",
        sa.Column("folder_id", sa.String(32), primary_key=True),
        sa.Column("path", sa.String(1024), nullable=False, unique=True),
        sa.Column("path_lower", sa.String(1024), nullable=False, server_default=""),
        sa.Column(
            "parent_id",
            sa.String(32),
            sa.ForeignKey("folders.folder_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("trashed_metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("metadata_json", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_index("ix_folders_path", "folders", ["path"], unique=True)
    op.create_index("ix_folders_path_lower", "folders", ["path_lower"])
    op.create_index("ix_folders_parent_id", "folders", ["parent_id"])
    # Postgres-only: prefix index for "all descendants of path X" queries
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_folders_path_prefix "
        "ON folders (path varchar_pattern_ops)"
    )


# ───────────────────────────────────────────────────────────────────────
# Step 2: folder_grants
# ───────────────────────────────────────────────────────────────────────


def _create_folder_grants_table() -> None:
    op.create_table(
        "folder_grants",
        sa.Column("grant_id", sa.String(32), primary_key=True),
        sa.Column(
            "folder_id",
            sa.String(32),
            sa.ForeignKey("folders.folder_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("principal_id", sa.String(128), nullable=False),
        sa.Column("principal_type", sa.String(16), nullable=False),
        sa.Column("permission", sa.String(16), nullable=False),
        sa.Column("inherit", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("granted_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "granted_by", sa.String(128), nullable=False, server_default="system"
        ),
    )
    op.create_index("ix_folder_grants_folder_id", "folder_grants", ["folder_id"])
    op.create_index(
        "ix_folder_grants_principal",
        "folder_grants",
        ["principal_id", "principal_type"],
    )


# ───────────────────────────────────────────────────────────────────────
# Step 3: audit_log
# ───────────────────────────────────────────────────────────────────────


def _create_audit_log_table() -> None:
    op.create_table(
        "audit_log",
        sa.Column("audit_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(255), nullable=True),
        sa.Column("details", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_id", "created_at"])
    op.create_index(
        "ix_audit_log_target", "audit_log", ["target_type", "target_id", "created_at"]
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action", "created_at"])


# ───────────────────────────────────────────────────────────────────────
# Step 4: extend documents
# ───────────────────────────────────────────────────────────────────────


def _extend_documents_table() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.add_column(
            sa.Column(
                "folder_id",
                sa.String(32),
                sa.ForeignKey("folders.folder_id", ondelete="RESTRICT"),
                nullable=False,
                server_default=ROOT_FOLDER_ID,
            )
        )
        batch.add_column(
            sa.Column("path", sa.String(1024), nullable=False, server_default=ROOT_PATH)
        )
        batch.add_column(sa.Column("trashed_metadata", sa.JSON, nullable=True))
    op.create_index("ix_documents_folder_id", "documents", ["folder_id"])
    op.create_index("ix_documents_path", "documents", ["path"])
    # Postgres prefix index for subtree queries (`WHERE path LIKE '/legal/%'`)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_path_prefix "
        "ON documents (path varchar_pattern_ops)"
    )


# ───────────────────────────────────────────────────────────────────────
# Step 5: seed system folders (__root__, __trash__)
# ───────────────────────────────────────────────────────────────────────


def _seed_system_folders() -> None:
    conn = op.get_bind()
    now = sa.func.now()
    conn.execute(
        sa.text(
            """
            INSERT INTO folders (
                folder_id, path, path_lower, parent_id,
                name, is_system, created_at, updated_at, metadata_json
            )
            VALUES
              (:root_id,  :root_path,  :root_path_l,  NULL,
               'Root',  TRUE,  now(), now(), '{}'::json),
              (:trash_id, :trash_path, :trash_path_l, :root_id,
               'Trash', TRUE,  now(), now(), '{}'::json)
            ON CONFLICT (folder_id) DO NOTHING
            """
        ),
        {
            "root_id": ROOT_FOLDER_ID,
            "root_path": ROOT_PATH,
            "root_path_l": ROOT_PATH.lower(),
            "trash_id": TRASH_FOLDER_ID,
            "trash_path": TRASH_PATH,
            "trash_path_l": TRASH_PATH.lower(),
        },
    )


# ───────────────────────────────────────────────────────────────────────
# Step 6: backfill existing documents into __root__
# ───────────────────────────────────────────────────────────────────────


def _backfill_existing_documents() -> None:
    """
    All pre-existing docs now live directly under __root__. Path is
    assigned as '/<filename>' (or '/<doc_id>' as fallback). Collisions
    from duplicate filenames get a '(N)' suffix.
    """
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT doc_id, filename FROM documents ORDER BY created_at, doc_id")
    ).fetchall()

    seen_paths: set[str] = set()
    for doc_id, filename in rows:
        base = (filename or doc_id or "document").strip() or doc_id
        base = base.replace("/", "_").strip()
        candidate = f"/{base}"
        if candidate.lower() in seen_paths:
            i = 1
            stem, dot, ext = base.rpartition(".")
            while True:
                if dot:
                    candidate = f"/{stem} ({i}).{ext}"
                else:
                    candidate = f"/{base} ({i})"
                if candidate.lower() not in seen_paths:
                    break
                i += 1
        seen_paths.add(candidate.lower())
        conn.execute(
            sa.text(
                "UPDATE documents SET folder_id = :fid, path = :path WHERE doc_id = :doc_id"
            ),
            {"fid": ROOT_FOLDER_ID, "path": candidate, "doc_id": doc_id},
        )


# ───────────────────────────────────────────────────────────────────────
# Step 7: bootstrap root-level grant so the local user can see everything
# ───────────────────────────────────────────────────────────────────────


def _bootstrap_root_grant() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO folder_grants (
                grant_id, folder_id, principal_id, principal_type,
                permission, inherit, granted_at, granted_by
            )
            VALUES (
                '__bootstrap__', :root_id, 'local', 'user',
                'admin', TRUE, now(), 'system'
            )
            ON CONFLICT (grant_id) DO NOTHING
            """
        ),
        {"root_id": ROOT_FOLDER_ID},
    )
