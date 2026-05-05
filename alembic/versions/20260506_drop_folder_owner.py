"""Drop folders.owner_user_id — shared_with is now the sole authz field.

Revision ID: 20260506_drop_folder_owner
Revises: 20260505_multi_user
Create Date: 2026-05-06

The multi-user feature originally introduced a per-folder
``owner_user_id`` alongside ``shared_with`` (one-row "owner" plus a
list of "members"). Production usage made it clear the extra tier
wasn't paying for itself: anything an owner could do, a user with
``rw`` in ``shared_with`` should also be allowed to do, and
``role=admin`` on ``auth_users`` covers the global escape hatch.

Dropping the column lets ``shared_with`` be the single source of
truth and removes a bunch of edge cases:

  * No "creator becomes owner of subfolder under another user's
    folder" surprise — the new folder just inherits the parent's
    shared_with.
  * No transfer-ownership operation; admin re-edits shared_with.
  * No "ownerless" state to clean up after user deletion.

The ``documents.owner_user_id``, ``files.owner_user_id``, and
``conversations.user_id`` columns are KEPT — the first two as
audit-only attribution ("who uploaded this") and the third as the
privacy boundary ("conversations are user-private, not folder-
scoped"). Only ``folders.owner_user_id`` is going away.
"""

from __future__ import annotations

from alembic import op

revision = "20260506_drop_folder_owner"
down_revision = "20260505_multi_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_folders_owner_user_id", table_name="folders")
    with op.batch_alter_table("folders") as batch:
        batch.drop_column("owner_user_id")


def downgrade() -> None:
    import sqlalchemy as sa

    with op.batch_alter_table("folders") as batch:
        batch.add_column(
            sa.Column(
                "owner_user_id",
                sa.String(32),
                sa.ForeignKey("auth_users.user_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
    op.create_index("ix_folders_owner_user_id", "folders", ["owner_user_id"])
