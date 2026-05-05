"""Rename documents/files owner_user_id → user_id (audit-only attribution).

Revision ID: 20260507_rename_owner_to_user
Revises: 20260506_drop_folder_owner
Create Date: 2026-05-07

The ``owner_user_id`` columns on ``documents`` and ``files`` were a
holdover from the early multi-user design where folders also had an
explicit owner tier. Folder ownership is gone (S0); these two
columns now serve a narrower purpose — attribution / audit, "who
created this row" — and the name no longer fits. Rename to
``user_id`` to match ``conversations.user_id`` (also a creator
attribution).

The columns do NOT participate in authz. Read access is gated
solely by ``shared_with`` on the containing folder; admin role
bypass on the auth layer covers the global escape hatch.

Indices are dropped + recreated under the new name; FKs follow the
column rename automatically.
"""

from __future__ import annotations

from alembic import op

revision = "20260507_rename_owner_to_user"
down_revision = "20260506_drop_folder_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # documents
    op.drop_index("ix_documents_owner_user_id", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        batch.alter_column("owner_user_id", new_column_name="user_id")
    op.create_index("ix_documents_user_id", "documents", ["user_id"])

    # files
    op.drop_index("ix_files_owner_user_id", table_name="files")
    with op.batch_alter_table("files") as batch:
        batch.alter_column("owner_user_id", new_column_name="user_id")
    op.create_index("ix_files_user_id", "files", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_files_user_id", table_name="files")
    with op.batch_alter_table("files") as batch:
        batch.alter_column("user_id", new_column_name="owner_user_id")
    op.create_index("ix_files_owner_user_id", "files", ["owner_user_id"])

    op.drop_index("ix_documents_user_id", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        batch.alter_column("user_id", new_column_name="owner_user_id")
    op.create_index("ix_documents_owner_user_id", "documents", ["owner_user_id"])
