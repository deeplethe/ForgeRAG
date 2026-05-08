"""Phase 0 — Library / Workspace split: project / artifact / agent-run tables.

Revision ID: 20260516_add_projects_artifacts_runs
Revises: 20260515_add_conversation_favorite
Create Date: 2026-05-16

Lays the relational shape behind the new "Workspace" surface (the
agent-driven artifact area, distinct from the renamed Library = file
manager). No behaviour change yet — the routes that consume these
tables land in 0.4; this migration just makes sure subsequent slices
have somewhere to write.

Tables introduced:

    projects
      One row per agent-workspace project. Owns a workdir under
      ``storage/projects/<project_id>/``, with ``shared_with`` reusing
      the Library's owner+rw grant shape (see Folder.shared_with).
      ``last_active_at`` is bumped by run / artifact writes so the UI
      can sort "recently touched" projects to the top.

    agent_runs
      One row per agent execution lifecycle (a single user's "do this
      task" request that the planner / executor walks). Holds the
      structured plan, current step pointer, status, and rolled-up
      token / cost / wall-time totals. Survives FastAPI worker
      restart — the orchestrator resumes from ``step_index``.

    agent_run_steps
      Granular per-step audit. One row per planner / executor /
      critic / sub-agent invocation inside a run. Tool calls + their
      results land here; this is what the per-project run-history UI
      renders as a timeline.

    artifacts
      One row per file the agent (or the user) drops into a project's
      workdir that's worth tracking as a deliverable. ``lineage_json``
      records sources (chunk_ids / urls / prior artifact_ids) so
      every claim a downstream document makes can be traced back.
      User-uploaded files also get artifact rows (run_id NULL); the
      Library bridge in 0.5 also creates them on import.

    execution_sessions
      One row per live ipykernel running inside a user's per-user
      Docker container. Tracks (container_id, kernel_id, last_active_at)
      so the SandboxManager can decide when to reap idle kernels and
      when to cold-start a fresh one for a project.

Schema deltas to existing tables:

    conversations
      + project_id  VARCHAR(32) NULL  FK projects.project_id  ON DELETE SET NULL

      Nullable on purpose — chats not tied to a Project keep working
      as today (just Q&A, no workspace artifacts). The 0.5 chat-to-
      project binding step populates it for newly-created chats.

Reversible: ``downgrade`` drops everything in reverse order. Note
that any artifacts / runs / projects existing at downgrade time are
permanently lost — the workdirs on disk under ``storage/projects/``
are NOT touched, so re-applying the migration would orphan them.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260516_add_projects_artifacts_runs"
down_revision = "20260515_add_conversation_favorite"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── projects ──────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # Relative path under storage root, e.g. "projects/<project_id>".
        # Resolved against ``storage_root`` at I/O time so deployments
        # that move the storage root don't have to rewrite this column.
        sa.Column("workdir_path", sa.String(1024), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Same shape as folders.shared_with: list[{user_id, role}] where
        # role ∈ {"r", "rw"}. Owner is implicit (always full access);
        # admins bypass via auth_users.role='admin'. Day 1 we ship with
        # only "rw" co-collaborators in mind — read-only viewers are a
        # post-Phase-0 polish item.
        sa.Column("shared_with", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        # Soft-delete marker: when non-NULL, the project is in trash.
        # Shape matches Folder.trashed_metadata: {original_path,
        # trashed_at, trashed_by}. Workdir on disk is moved to
        # ``storage/projects/__trash__/<ts>_<id>/`` in lockstep.
        sa.Column("trashed_metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        # Bumped by run / artifact writes; nullable for the brief
        # window between create and first activity.
        sa.Column("last_active_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"])
    op.create_index("ix_projects_last_active_at", "projects", ["last_active_at"])

    # ── agent_runs ────────────────────────────────────────────────
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Nullable because the chat ↔ project binding lands in 0.5;
        # also future scheduled / cron runs may have no conversation.
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversations.conversation_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Planner's structured output the executor walks. Free-form
        # JSON intentionally — schema lives in code, not the DB, so
        # iteration on plan shape doesn't need migrations.
        sa.Column("plan_json", sa.JSON(), nullable=False, server_default="{}"),
        # pending | running | paused | done | failed | cancelled
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        # 0-based pointer into plan.steps; resume index after restart.
        sa.Column(
            "step_index", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column("last_checkpoint_at", sa.DateTime, nullable=True),
        sa.Column(
            "total_input_tokens", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "total_output_tokens", sa.Integer, nullable=False, server_default="0"
        ),
        # Stored as Float; rounded for display in the UI.
        sa.Column(
            "total_cost_usd", sa.Float, nullable=False, server_default="0"
        ),
        sa.Column(
            "started_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_agent_runs_project_id", "agent_runs", ["project_id"])
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index(
        "ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"]
    )

    # ── agent_run_steps ───────────────────────────────────────────
    op.create_table(
        "agent_run_steps",
        sa.Column("step_id", sa.String(32), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(32),
            sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer, nullable=False),
        # planner | executor | critic | sub-agent | tool
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("tool_call_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column(
            "input_tokens", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "output_tokens", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column("wall_ms", sa.Integer, nullable=False, server_default="0"),
        # pending | running | done | failed | skipped
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending"
        ),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_agent_run_steps_run_id", "agent_run_steps", ["run_id"]
    )
    op.create_index(
        "ix_agent_run_steps_run_step",
        "agent_run_steps",
        ["run_id", "step_index"],
    )

    # ── artifacts ─────────────────────────────────────────────────
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(32), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NULL for user-uploaded artifacts; set when an agent run
        # produced this file.
        sa.Column(
            "run_id",
            sa.String(32),
            sa.ForeignKey("agent_runs.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "produced_by_step_id",
            sa.String(32),
            sa.ForeignKey("agent_run_steps.step_id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Path relative to the project workdir, e.g.
        # "outputs/contracts_summary.xlsx". The UI renders these
        # against the host filesystem directly via FastAPI; the
        # container sees the same file at /workdir/<project_id>/<path>.
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=True),
        # {sources: [{type: "chunk"|"url"|"artifact", id: "...",
        #             url: "...", artifact_id: "..."}]}
        # Chunk references survive Library re-ingest because chunk_ids
        # are stable; URLs carry a snapshot sha to detect drift.
        sa.Column("lineage_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])
    op.create_index("ix_artifacts_user_id", "artifacts", ["user_id"])

    # ── execution_sessions ────────────────────────────────────────
    op.create_table(
        "execution_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(32),
            sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Docker container id (long form). NULL while the container
        # is being cold-started, populated once the daemon returns it.
        sa.Column("container_id", sa.String(255), nullable=True),
        # Jupyter kernel uuid (from the kernel connection-info file).
        sa.Column("kernel_id", sa.String(255), nullable=True),
        # starting | ready | busy | dead | reaped
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="starting"
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_execution_sessions_project_id", "execution_sessions", ["project_id"]
    )
    op.create_index(
        "ix_execution_sessions_user_id", "execution_sessions", ["user_id"]
    )
    op.create_index(
        "ix_execution_sessions_status", "execution_sessions", ["status"]
    )

    # ── conversations.project_id ──────────────────────────────────
    # SQLite batch-alter rejects anonymous FK constraints — name the
    # constraint explicitly so ``ALTER TABLE … ADD CONSTRAINT`` (the
    # batch-mode rebuild that SQLite uses internally) round-trips
    # cleanly across both Postgres and SQLite. Postgres ignores the
    # name beyond display purposes; SQLite requires it.
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column(
                "project_id",
                sa.String(32),
                sa.ForeignKey(
                    "projects.project_id",
                    ondelete="SET NULL",
                    name="fk_conversations_project_id",
                ),
                nullable=True,
            )
        )
    op.create_index(
        "ix_conversations_project_id", "conversations", ["project_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_project_id", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("project_id")

    op.drop_index(
        "ix_execution_sessions_status", table_name="execution_sessions"
    )
    op.drop_index(
        "ix_execution_sessions_user_id", table_name="execution_sessions"
    )
    op.drop_index(
        "ix_execution_sessions_project_id", table_name="execution_sessions"
    )
    op.drop_table("execution_sessions")

    op.drop_index("ix_artifacts_user_id", table_name="artifacts")
    op.drop_index("ix_artifacts_run_id", table_name="artifacts")
    op.drop_index("ix_artifacts_project_id", table_name="artifacts")
    op.drop_table("artifacts")

    op.drop_index("ix_agent_run_steps_run_step", table_name="agent_run_steps")
    op.drop_index("ix_agent_run_steps_run_id", table_name="agent_run_steps")
    op.drop_table("agent_run_steps")

    op.drop_index(
        "ix_agent_runs_conversation_id", table_name="agent_runs"
    )
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_project_id", table_name="agent_runs")
    op.drop_table("agent_runs")

    op.drop_index("ix_projects_last_active_at", table_name="projects")
    op.drop_index("ix_projects_owner_user_id", table_name="projects")
    op.drop_table("projects")
