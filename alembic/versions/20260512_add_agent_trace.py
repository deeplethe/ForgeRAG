"""Add ``messages.agent_trace_json`` — persists the agent's
reasoning chain so refreshes keep the "Thought for Xs · N tools"
panel visible.

Revision ID: 20260512_add_agent_trace
Revises: 20260508_drop_message_thinking
Create Date: 2026-05-12

Pre-this-migration the agent's reasoning chain (the sequence of
phase / thought / tool entries the frontend renders as
"Understanding the question → Semantic search '…' → Reviewing
results → Read 8 passages …") was session-only state on the
client. Live streaming built it from SSE events; once the message
landed in DB it had only the final answer + citations. On reload
the chain disappeared entirely — direct-answer turns simply lost
the panel, while tool turns showed the answer with no context for
WHY those particular sources were used.

This migration adds a JSON column to the messages table to
persist the trace. The route's ``_accumulate_trace`` helper
mirrors the frontend's reducer: each SSE event extends the trace
the same way ``streamTrace`` does. The full structured array is
written alongside the assistant row's content + citations.

Reversible: ``downgrade`` drops the column. Persisted traces are
lost forever on downgrade; users who roll back see the same
"empty panel on reload" UX as before.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260512_add_agent_trace"
down_revision = "20260508_drop_message_thinking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite supports adding nullable columns directly; batch mode
    # is unnecessary but harmless and matches sibling migrations.
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("agent_trace_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("agent_trace_json")
