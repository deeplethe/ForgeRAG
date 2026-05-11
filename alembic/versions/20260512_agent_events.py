"""Add ``agent_events`` + long-task columns on ``agent_runs``.

Revision ID: 20260512_agent_events
Revises: 20260522_add_conversation_path_filters
Create Date: 2026-05-12

Long-task / HITL architecture v1 — persistence layer (Inc 1 of 7).

Two changes:

1. New ``agent_events`` table. Every event the agent emits during a
   run (phase / thought / tool / citation / approval_request /
   ask_human / sub_agent_start / usage / done / ...) lands here, keyed
   by a per-run monotonic ``seq``. Backs the disconnect-survival +
   reconnect replay protocol: ``GET /conversations/{id}/stream?since=N``
   replays events with seq>N before tailing live ones. Without this
   table the agent can't survive client disconnect.

2. New columns on ``agent_runs`` for the long-task extension:
   - ``parent_run_id`` + ``depth`` — sub-agent tree (Task tool spawns)
   - ``last_event_seq``        — fast reconnect short-circuit
   - ``token_budget_total``    — single ceiling (input+output combined)
   - ``escalation_reason``     — set when agent calls ask_human

Forward-only design notes:
  - No archival in MVP; ``agent_events`` grows linearly with usage.
    Single-user self-host shouldn't hit any ceiling for years.
  - ``parent_run_id`` is FK→agent_runs.run_id (self-ref) with CASCADE
    so deleting a parent run nukes the sub-tree. Future "preserve
    finished sub-results" can detach by setting to NULL first.
  - The unique index ``(run_id, seq)`` is the read key for replay.
    seq is assigned in-memory by AgentTaskHandle (monotonic per run);
    DB inserts may interleave runs which is why we don't rely on the
    autoinc id for ordering.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260512_agent_events"
down_revision = "20260522_add_conversation_path_filters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── agent_runs: new columns ───────────────────────────────────
    # SQLite batch-alter for cross-dialect compatibility. Named FK
    # constraint required by SQLite's batch rebuild path.
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(
            sa.Column(
                "parent_run_id",
                sa.String(32),
                sa.ForeignKey(
                    "agent_runs.run_id",
                    ondelete="CASCADE",
                    name="fk_agent_runs_parent_run_id",
                ),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column("depth", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "last_event_seq", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(
            sa.Column("token_budget_total", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column("escalation_reason", sa.String(255), nullable=True)
        )
    op.create_index(
        "ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"]
    )

    # ── agent_events ──────────────────────────────────────────────
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(32),
            sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column(
            "payload_json", sa.JSON(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])
    op.create_index(
        "ix_agent_events_event_type", "agent_events", ["event_type"]
    )
    # Reconnect replay key: ORDER BY seq ASC WHERE run_id=? AND seq>?
    op.create_index(
        "ix_agent_events_run_seq",
        "agent_events",
        ["run_id", "seq"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_events_run_seq", table_name="agent_events")
    op.drop_index("ix_agent_events_event_type", table_name="agent_events")
    op.drop_index("ix_agent_events_run_id", table_name="agent_events")
    op.drop_table("agent_events")

    op.drop_index("ix_agent_runs_parent_run_id", table_name="agent_runs")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("escalation_reason")
        batch.drop_column("token_budget_total")
        batch.drop_column("last_event_seq")
        batch.drop_column("depth")
        batch.drop_column("parent_run_id")
