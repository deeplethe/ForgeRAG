"""Add ``messages.input_tokens`` + ``output_tokens`` — per-turn LLM
token accounting so usage can be aggregated per user.

Revision ID: 20260513_add_message_tokens
Revises: 20260512_add_agent_trace
Create Date: 2026-05-13

Background. ``query_traces`` (the legacy /query route's audit
table) carries token counts in its ``trace_json`` blob, but the
agent path that replaced /query post-cutover writes to ``messages``
instead — and ``messages`` had no token columns. Result: every
admin metric / per-user usage view came back empty even though
the agent loop already exposes ``tokens_in`` / ``tokens_out`` on
its final ``done`` event.

This migration adds the two integer columns directly on the
assistant message row. Cheaper than a per-call usage_log table
(per-turn granularity is what every UI needs anyway) and the
join path for "this user's total tokens" is just messages →
conversations → user_id, which is already indexed.

Columns are NOT NULL with default 0 so existing rows (and any
tool turn that writes the user side first) read back as zero
instead of NULL. The /usage endpoints sum across these directly.

Reversible: ``downgrade`` drops the columns. Historical token
counts are lost on downgrade; future per-user/total queries
return 0 for any pre-rollback turn.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260513_add_message_tokens"
down_revision = "20260512_add_agent_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(
            sa.Column(
                "input_tokens",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "output_tokens",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("output_tokens")
        batch.drop_column("input_tokens")
