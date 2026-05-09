"""Add ``cwd_path`` to conversations / agent_runs / artifacts.

Revision ID: 20260518_add_conversation_cwd_path
Revises: 20260516_add_projects_artifacts_runs
Create Date: 2026-05-18

Folder-as-cwd refactor — stage 1 (schema only).

Each conversation / agent_run / artifact gains a folder path
(``cwd_path``) that names where in the user's accessible folder
tree the chat / run / artifact "lives". This replaces the
``Project`` mental model: instead of selecting a Project from a
list, the user opens a chat IN A FOLDER, and the agent's working
directory IS that folder.

Schema details:

  * ``cwd_path`` is ``String(1024)`` to match other path columns
    (``Folder.path``, ``Document.path``).
  * Nullable for backwards compatibility — legacy rows that
    pre-date the refactor stay project-bound via the existing
    ``project_id`` columns (kept untouched in this migration).
    The route-side selector treats NULL ``cwd_path`` as "fall
    back to project lookup".
  * Indexed because every agent-turn endpoint will filter rows by
    ``WHERE user_id = ? AND cwd_path = ?`` and we expect heavy
    fan-out per user.

Backfill is intentionally NOT done here. Project-bound legacy
rows have a ``project_id`` that points to a ``workdir_path`` of
the form ``projects/<project_id>/...`` — that's a STORAGE path,
not a user-visible folder path. Coercing it into ``cwd_path``
would create a misleading record. New chats opt into the new
field; legacy rows stay legacy until a follow-up clean-up
migration once the UX is settled.

The ``Project`` table itself + ``project_id`` FKs stay until a
later "drop Project" migration; this stage is additive only,
fully reversible.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260518_add_conversation_cwd_path"
down_revision = "20260516_add_projects_artifacts_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column("cwd_path", sa.String(1024), nullable=True)
        )
        batch.create_index(
            "ix_conversations_cwd_path",
            ["cwd_path"],
        )

    with op.batch_alter_table("agent_runs") as batch:
        # ``project_id`` was NOT NULL — new folder-as-cwd runs have
        # no project to point at. Relax the constraint; the FK +
        # CASCADE on existing rows is unchanged.
        batch.alter_column(
            "project_id",
            existing_type=sa.String(32),
            nullable=True,
        )
        batch.add_column(
            sa.Column("cwd_path", sa.String(1024), nullable=True)
        )
        batch.create_index(
            "ix_agent_runs_cwd_path",
            ["cwd_path"],
        )

    with op.batch_alter_table("artifacts") as batch:
        # Same relaxation — agent-produced artifacts under
        # folder-as-cwd are anchored by ``cwd_path`` + ``run_id``,
        # not by a Project.
        batch.alter_column(
            "project_id",
            existing_type=sa.String(32),
            nullable=True,
        )
        batch.add_column(
            sa.Column("cwd_path", sa.String(1024), nullable=True)
        )
        batch.create_index(
            "ix_artifacts_cwd_path",
            ["cwd_path"],
        )


def downgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_index("ix_artifacts_cwd_path")
        batch.drop_column("cwd_path")
        batch.alter_column(
            "project_id",
            existing_type=sa.String(32),
            nullable=False,
        )

    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_index("ix_agent_runs_cwd_path")
        batch.drop_column("cwd_path")
        batch.alter_column(
            "project_id",
            existing_type=sa.String(32),
            nullable=False,
        )

    with op.batch_alter_table("conversations") as batch:
        batch.drop_index("ix_conversations_cwd_path")
        batch.drop_column("cwd_path")
