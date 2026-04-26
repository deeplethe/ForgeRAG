"""Auth: single-user-now, multi-user-ready admin + tokens + sessions.

Revision ID: 20260424_auth_tables
Revises: 20260418_chunks_path
Create Date: 2026-04-24

Adds three tables to support password + API-token + web-session auth:

    auth_users      — one row per user; bootstrapped on first server
                      start when ``auth.enabled=true`` and this table is
                      empty (default password ``forgerag`` + random SK,
                      ``must_change_password=True``).
    auth_tokens     — bearer tokens (CLI/SDK). sha256 hash only — raw
                      plaintext is returned exactly once at creation.
    auth_sessions   — web-login sessions. Opaque ``session_id`` in cookie.
                      No TTL; revoked on logout, or when the owner changes
                      their password (revokes all OTHER sessions).

Multi-user extension later = INSERT more rows into ``auth_users`` + the
frontend grows a username field. No schema migration needed.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260424_auth_tables"
down_revision = "20260418_chunks_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_users",
        sa.Column("user_id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("password_changed_at", sa.DateTime, nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="admin"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_auth_users_username", "auth_users", ["username"], unique=True)

    op.create_table(
        "auth_tokens",
        sa.Column("token_id", sa.String(32), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("hash_prefix", sa.String(8), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="admin"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"])
    op.create_index("ix_auth_tokens_token_hash", "auth_tokens", ["token_hash"], unique=True)

    op.create_table(
        "auth_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_auth_tokens_token_hash", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens_user_id", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_index("ix_auth_users_username", table_name="auth_users")
    op.drop_table("auth_users")
