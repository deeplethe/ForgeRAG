"""Add ``auth_users.avatar_path`` — relative path to the user's
uploaded profile image, NULL when the user hasn't set one (UI
falls back to initials).

Revision ID: 20260514_add_user_avatar
Revises: 20260513_add_message_tokens
Create Date: 2026-05-14

The path is stored relative to the storage root and includes the
file extension (e.g. ``avatars/u_alice.png``) so the GET endpoint
can derive content-type without a separate column. ``avatar_path``
is rewritten on every upload so a one-row UPDATE is enough to
swap the image — there's no version history. The user-id portion
of the filename plus a no-cache header on the GET handler keeps
browsers honest when an upload replaces the file in place.

Reversible: ``downgrade`` drops the column. The actual files on
disk are NOT removed by the migration — that's a separate
operational concern (the upload route writes ``./storage/avatars/``;
delete that directory by hand if you want a clean revert).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260514_add_user_avatar"
down_revision = "20260513_add_message_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("auth_users") as batch:
        batch.add_column(sa.Column("avatar_path", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("auth_users") as batch:
        batch.drop_column("avatar_path")
