"""Add ``conversations.path_filters_json`` for chat-level pinned
knowledge scopes.

Revision ID: 20260522_add_conversation_path_filters
Revises: 20260521_add_attachments_table
Create Date: 2026-05-22

Multi-knowledge upgrade: the chat input now lets the user pin a list
of folder / file paths from the knowledge tree (chip-rail style),
sticky across turns until removed. Each ChatRequest forwards the
list, and the chat route injects it into the agent's prompt as a
"preferred search scope" hint — the agent fans out per path.

Stored as JSON list[str] rather than a join table because the list
is small (≤10 typically), order matters (for the prompt hint), and
we never query "which conversations contain path X" — the access
pattern is always "load this conversation's pinned scopes". JSON
default to ``[]`` so reads from legacy rows return an empty list.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260522_add_conversation_path_filters"
down_revision = "20260521_add_attachments_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column(
                "path_filters_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("path_filters_json")
