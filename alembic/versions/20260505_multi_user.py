"""Multi-user: folder ownership + shared_with + per-resource owners.

Revision ID: 20260505_multi_user
Revises: 20260424_auth_tables
Create Date: 2026-05-05

This migration lays the schema for Feature 3 (multi-user) from the
retrieval-evolution roadmap. There is no behaviour change yet — the
new columns sit nullable and back-filled to the bootstrap admin so
existing single-user deploys continue working. Routes start using
the new fields in the slices that follow (S2: AuthorizationService +
path_filters list; S3: folder sharing API; S4: self-registration).

Schema deltas:

    auth_users
      + email           VARCHAR(255) NULL  (UNIQUE when set)
      + display_name    VARCHAR(64)  NULL
      + status          VARCHAR(20)  NOT NULL DEFAULT 'active'
                        ('pending_approval' | 'active' | 'suspended' | 'deleted')

    auth_tokens
      + scope_path      VARCHAR(1024) NULL
      + scope_role      VARCHAR(16)   NULL

    folders
      + owner_user_id   VARCHAR(32)  NULL  FK auth_users.user_id
      + shared_with     JSON         NOT NULL DEFAULT '[]'

    documents
      + owner_user_id   VARCHAR(32)  NULL  FK auth_users.user_id

    files
      + owner_user_id   VARCHAR(32)  NULL  FK auth_users.user_id

    conversations
      + user_id         VARCHAR(32)  NULL  FK auth_users.user_id

    folder_invitations  (new)
      One row per outstanding share invite. Inviter copies the URL
      manually (no SMTP in v1).

The legacy ``folder_grants`` table from the original folder-tree
migration is left in place but no longer read by anything; dropping
it is deferred to a follow-up so this migration stays additive.

Backfill: rows currently belong to the implicit single admin. We
look up the FIRST ``auth_users`` row (the bootstrap admin) and set
all owner / user_id columns to that user_id. If ``auth_users`` is
empty (auth has never been bootstrapped), we leave NULLs — the
bootstrap path will populate ownership when it creates the admin.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260505_multi_user"
down_revision = "20260424_auth_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── auth_users: email / display_name / status ───────────────────
    with op.batch_alter_table("auth_users") as batch:
        batch.add_column(sa.Column("email", sa.String(255), nullable=True))
        batch.add_column(sa.Column("display_name", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="active",
            )
        )
    op.create_index("ix_auth_users_email", "auth_users", ["email"])
    op.create_index("ix_auth_users_status", "auth_users", ["status"])

    # ── auth_tokens: scope_path / scope_role ────────────────────────
    with op.batch_alter_table("auth_tokens") as batch:
        batch.add_column(sa.Column("scope_path", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("scope_role", sa.String(16), nullable=True))

    # ── folders: owner_user_id / shared_with ────────────────────────
    with op.batch_alter_table("folders") as batch:
        batch.add_column(
            sa.Column(
                "owner_user_id",
                sa.String(32),
                sa.ForeignKey("auth_users.user_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "shared_with",
                sa.JSON,
                nullable=False,
                server_default="[]",
            )
        )
    op.create_index("ix_folders_owner_user_id", "folders", ["owner_user_id"])

    # ── documents.owner_user_id ─────────────────────────────────────
    with op.batch_alter_table("documents") as batch:
        batch.add_column(
            sa.Column(
                "owner_user_id",
                sa.String(32),
                sa.ForeignKey("auth_users.user_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
    op.create_index("ix_documents_owner_user_id", "documents", ["owner_user_id"])

    # ── files.owner_user_id ─────────────────────────────────────────
    with op.batch_alter_table("files") as batch:
        batch.add_column(
            sa.Column(
                "owner_user_id",
                sa.String(32),
                sa.ForeignKey("auth_users.user_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
    op.create_index("ix_files_owner_user_id", "files", ["owner_user_id"])

    # ── conversations.user_id ───────────────────────────────────────
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column(
                "user_id",
                sa.String(32),
                sa.ForeignKey("auth_users.user_id", ondelete="CASCADE"),
                nullable=True,
            )
        )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    # ── folder_invitations (new table) ──────────────────────────────
    op.create_table(
        "folder_invitations",
        sa.Column("invitation_id", sa.String(32), primary_key=True),
        sa.Column(
            "folder_id",
            sa.String(32),
            sa.ForeignKey("folders.folder_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "inviter_user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("consumed_at", sa.DateTime, nullable=True),
        sa.Column(
            "consumed_by_user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_folder_invitations_folder_id", "folder_invitations", ["folder_id"]
    )
    op.create_index(
        "ix_folder_invitations_inviter_user_id",
        "folder_invitations",
        ["inviter_user_id"],
    )
    op.create_index(
        "ix_folder_invitations_target_email",
        "folder_invitations",
        ["target_email"],
    )
    op.create_index(
        "ix_folder_invitations_token_hash",
        "folder_invitations",
        ["token_hash"],
        unique=True,
    )

    # ── Backfill ───────────────────────────────────────────────────
    # Existing single-admin deploys: bootstrap admin (the first row in
    # auth_users) becomes owner of every folder and the user_id on
    # every conversation. New self-registered users in S4 will create
    # rows with their own user_id from the start.
    bind = op.get_bind()
    admin_id_row = bind.execute(
        sa.text(
            "SELECT user_id FROM auth_users "
            "WHERE role = 'admin' "
            "ORDER BY created_at ASC LIMIT 1"
        )
    ).fetchone()
    if admin_id_row is not None:
        admin_id = admin_id_row[0]
        # NB: the ``folders.owner_user_id`` column added by this
        # migration is dropped by ``20260506_drop_folder_owner`` —
        # the simplification PR found it didn't pay for itself
        # alongside ``shared_with``. Backfill kept here for forward
        # / down-migration consistency: applying THIS migration in
        # isolation populates the column, and the next migration
        # drops it cleanly.
        bind.execute(
            sa.text("UPDATE folders SET owner_user_id = :uid WHERE owner_user_id IS NULL"),
            {"uid": admin_id},
        )
        bind.execute(
            sa.text("UPDATE documents SET owner_user_id = :uid WHERE owner_user_id IS NULL"),
            {"uid": admin_id},
        )
        bind.execute(
            sa.text("UPDATE files SET owner_user_id = :uid WHERE owner_user_id IS NULL"),
            {"uid": admin_id},
        )
        bind.execute(
            sa.text("UPDATE conversations SET user_id = :uid WHERE user_id IS NULL"),
            {"uid": admin_id},
        )


def downgrade() -> None:
    op.drop_index(
        "ix_folder_invitations_token_hash", table_name="folder_invitations"
    )
    op.drop_index(
        "ix_folder_invitations_target_email", table_name="folder_invitations"
    )
    op.drop_index(
        "ix_folder_invitations_inviter_user_id",
        table_name="folder_invitations",
    )
    op.drop_index(
        "ix_folder_invitations_folder_id", table_name="folder_invitations"
    )
    op.drop_table("folder_invitations")

    op.drop_index("ix_conversations_user_id", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("user_id")

    op.drop_index("ix_files_owner_user_id", table_name="files")
    with op.batch_alter_table("files") as batch:
        batch.drop_column("owner_user_id")

    op.drop_index("ix_documents_owner_user_id", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("owner_user_id")

    op.drop_index("ix_folders_owner_user_id", table_name="folders")
    with op.batch_alter_table("folders") as batch:
        batch.drop_column("shared_with")
        batch.drop_column("owner_user_id")

    with op.batch_alter_table("auth_tokens") as batch:
        batch.drop_column("scope_role")
        batch.drop_column("scope_path")

    op.drop_index("ix_auth_users_status", table_name="auth_users")
    op.drop_index("ix_auth_users_email", table_name="auth_users")
    with op.batch_alter_table("auth_users") as batch:
        batch.drop_column("status")
        batch.drop_column("display_name")
        batch.drop_column("email")
