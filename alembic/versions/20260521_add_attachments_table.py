"""Add ``attachments`` table for chat-message file attachments.

Revision ID: 20260521_add_attachments_table
Revises: 20260520_add_conversation_read_state
Create Date: 2026-05-11

Stores user-uploaded files attached to chat messages — images, PDFs,
plain-text-ish files (md / html / json / csv / log / ...). The blob
itself lives on disk under
``<storage_root>/user-uploads/<user_id>/<conv_id>/<id>__<filename>``;
this row carries the metadata + path.

Two-phase lifecycle:

  1. **Draft**: user uploads via ``POST /conversations/<cid>/attachments``
     before sending the message. Row is created with ``message_id =
     NULL`` and visible in the input box as a chip the user can
     remove. Drafts older than N hours can be GC'd.

  2. **Bound**: when the user sends the message, the chat route binds
     the draft attachments by setting ``message_id``. Once bound, the
     attachment lives as long as the message does (``ON DELETE
     CASCADE`` from ``messages``).

Conversation-level cascade is also wired so deleting a conversation
takes its attachments with it (covers both bound and still-draft
rows for that conversation).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260521_add_attachments_table"
down_revision = "20260520_add_conversation_read_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("attachment_id", sa.String(64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "message_id",
            sa.String(64),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.String(32),
            sa.ForeignKey("auth_users.user_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        # Canonical MIME (browser-supplied or sniffed). Used by the
        # frontend to pick an icon and by the agent to decide which
        # content-block format to wrap the bytes in.
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        # SHA-256 of the blob content. Lets a future de-dup pass
        # collapse identical uploads to one blob (not implemented
        # for v0). Hex-encoded, 64 chars.
        sa.Column("sha256", sa.String(64), nullable=True),
        # Coarse classification — "text" / "image" / "pdf" / "other".
        # Computed once at upload from the MIME so the agent runtime
        # doesn't have to re-classify on every send.
        sa.Column("kind", sa.String(16), nullable=False),
        # Filesystem path to the blob, relative to the storage root.
        # Resolved against ``cfg.storage.root`` at read time so the
        # storage tree can move without rewriting rows.
        sa.Column("blob_path", sa.String(1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("attachments")
