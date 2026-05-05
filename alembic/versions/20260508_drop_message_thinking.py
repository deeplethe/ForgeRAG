"""Drop ``messages.thinking`` — provider CoT no longer captured.

Revision ID: 20260508_drop_message_thinking
Revises: 20260507_rename_owner_to_user
Create Date: 2026-05-08

The ``thinking`` column held provider-side CoT text (DeepSeek
V4-Pro / o1 / Anthropic extended thinking) so the chat UI could
render the model's reasoning trace alongside the answer. Post
agent-cutover that no longer happens:

  * Provider thinking is hard-disabled at every litellm callsite
    (commit ``d07f673``). New assistant messages would always
    write ``thinking=None``.
  * The frontend's "🧠 思考中…" indicator now reflects the
    AGENT's between-turn reasoning (commit ``3b36acb``) — not
    persisted text but a derived state from the SSE event stream.
  * The chat UI's live + persisted CoT panes were dropped
    (commits ``4afe145`` + this commit's frontend pair).

Future deep-research mode (roadmap Feature 5) will introduce its
own tables (plan / sections / drafts / human-decisions) — not
piggy-back on this column.

Existing rows lose their stored CoT content on this migration.
That's acceptable: the content was display-only; users who
re-open old conversations see the answer + citations exactly as
before, just without the (now-orphaned) reasoning collapsible.

Reversible: ``downgrade`` re-adds the column as nullable text.
Stored content is gone forever — users who downgrade will see
empty thinking panes.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260508_drop_message_thinking"
down_revision = "20260507_rename_owner_to_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite doesn't support DROP COLUMN before 3.35; use batch
    # mode so alembic emits a table-rebuild on older SQLite, no-op
    # on Postgres.
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("thinking")


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("thinking", sa.Text(), nullable=True))
