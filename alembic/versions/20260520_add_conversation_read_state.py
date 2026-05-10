"""Add ``conversations.last_read_at`` + ``last_assistant_at`` for the
sidebar's per-conversation unread indicator.

Revision ID: 20260520_add_conversation_read_state
Revises: 20260518_add_conversation_cwd_path
Create Date: 2026-05-20

The chat sidebar shows a blue dot on conversations that have a fresh
agent reply the user hasn't looked at. We compute "unread" server-side
as ``last_assistant_at > last_read_at`` so the indicator syncs across
devices (user reads on phone → desktop sidebar dot clears).

Two columns rather than one because comparing against ``updated_at``
would false-positive on the user's own messages — typing a message
mutates ``updated_at`` but the user has clearly seen what they just
typed. ``last_assistant_at`` only bumps when the AGENT writes back.

Both nullable + default NULL — for legacy rows the unread state
collapses to "false" (NULL > NULL is false in SQL), which is the
right default: don't badge old conversations.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260520_add_conversation_read_state"
down_revision = "20260518_add_conversation_cwd_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column("last_assistant_at", sa.DateTime(), nullable=True)
        )
        batch.add_column(
            sa.Column("last_read_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("last_read_at")
        batch.drop_column("last_assistant_at")
