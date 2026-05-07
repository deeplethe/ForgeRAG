"""Add ``conversations.is_favorite`` — user-toggled per-conversation
flag for the sidebar's "star" gesture.

Revision ID: 20260515_add_conversation_favorite
Revises: 20260514_add_user_avatar
Create Date: 2026-05-15

The sidebar's three-dot row menu has a Star item; toggling sets
this column. The frontend can choose to surface favourites
specially (sort to top, render with a star glyph, etc.) — the
schema just stores the boolean.

NOT NULL with a server default of ``0`` so existing rows read as
"not favourited" without a backfill. Reversible: ``downgrade``
drops the column.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260515_add_conversation_favorite"
down_revision = "20260514_add_user_avatar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column(
                "is_favorite",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("is_favorite")
