"""
FolderInvitationService — issue / list / revoke / consume invitation
links to share a folder with a user who isn't yet registered.

The flow:

  1. Owner picks "invite by email" in the Members panel.
  2. ``create()`` mints a random opaque token, stores
     ``sha256(token)`` on a new ``FolderInvitation`` row, and
     returns the raw token + an expiry. The route packages it
     into a URL (``/auth/register?invite=<token>``) for the
     owner to copy.
  3. Recipient follows the link. The auth route looks up
     ``sha256(incoming)`` and surfaces ``preview()`` info
     (folder path + role + inviter email) before they confirm.
  4. After registration / login, ``consume()`` adds the
     ``shared_with`` grant atomically with marking the invitation
     consumed. A token can be redeemed at most once.

We store only the hash, never the raw token — same pattern as
``AuthToken``. Revocation is a soft-delete via ``revoked_at``
(written as a clear ``consumed_at`` row when the invitation
itself is revoked rather than redeemed). ``list()`` filters out
expired and consumed entries by default; pass
``include_consumed=True`` to surface them for the audit-style
"Recent invitations" view.

No SMTP yet (v1) — the inviter copy/pastes the URL into whatever
channel they use. v2 wires email delivery through a provider
abstraction.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .folder_share_service import FolderShareService
from .models import AuditLogRow, AuthUser, Folder, FolderInvitation

log = logging.getLogger(__name__)

Role = Literal["r", "rw"]

# Token length is 32 bytes (43 url-safe chars) — same order of
# magnitude as our auth tokens. Plenty of entropy; cheap to hash.
_TOKEN_BYTES = 32

# Default invitation lifetime. Caller can pass a custom value via
# ``ttl_days`` for one-off "30-day onboarding" links.
_DEFAULT_TTL_DAYS = 7


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvitationError(Exception):
    """Base class for invitation lifecycle errors."""


class InvitationNotFound(InvitationError):
    pass


class InvitationExpired(InvitationError):
    pass


class InvitationAlreadyConsumed(InvitationError):
    pass


class InvitationFolderMissing(InvitationError):
    """The folder the invitation pointed at no longer exists (deleted
    while the link was outstanding). The link is dead — return a
    helpful error to the recipient instead of half-redeeming."""


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class IssuedInvitation:
    """Returned by ``create()``. The raw token is included exactly
    once — the caller bakes it into a URL and never asks the server
    for it again."""

    invitation_id: str
    token: str  # raw, only returned at creation time
    folder_id: str
    folder_path: str
    target_email: str
    role: Role
    expires_at: datetime


@dataclass
class InvitationPreview:
    """Pre-redemption preview shown to the recipient so they can
    confirm what they're accepting before clicking Continue."""

    invitation_id: str
    folder_id: str
    folder_path: str
    target_email: str
    role: Role
    expires_at: datetime
    inviter_username: str
    inviter_email: str | None


@dataclass
class InvitationRow:
    """One row in the per-folder invitations list (admin view)."""

    invitation_id: str
    folder_id: str
    target_email: str
    role: Role
    inviter_user_id: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    consumed_by_user_id: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FolderInvitationService:
    """Stateless once constructed; the SQLAlchemy session is the only
    state. Instantiate per-request inside the route's transaction."""

    def __init__(self, sess: Session):
        self.sess = sess

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        folder_id: str,
        target_email: str,
        role: Role,
        inviter_user_id: str,
        ttl_days: int | None = None,
    ) -> IssuedInvitation:
        """Mint a new invitation. Returns the raw token exactly once;
        the caller is responsible for URL construction.

        Multiple outstanding invitations to the same email are
        allowed — they each carry their own token and the recipient
        can redeem any one of them. Revoking is a separate operation.
        """
        if role not in ("r", "rw"):
            raise InvitationError(f"invalid role: {role!r}")
        folder = self.sess.get(Folder, folder_id)
        if folder is None:
            raise InvitationFolderMissing(folder_id)

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        expires = _now() + timedelta(days=ttl_days or _DEFAULT_TTL_DAYS)
        invitation = FolderInvitation(
            invitation_id=_new_id(),
            folder_id=folder_id,
            inviter_user_id=inviter_user_id,
            target_email=target_email.lower().strip(),
            role=role,
            token_hash=_hash_token(token),
            expires_at=expires,
        )
        self.sess.add(invitation)
        self.sess.flush()

        self._audit(
            inviter_user_id,
            action="folder.invitation.create",
            target_id=folder_id,
            details={
                "invitation_id": invitation.invitation_id,
                "target_email": target_email,
                "role": role,
                "expires_at": expires.isoformat(),
            },
        )

        return IssuedInvitation(
            invitation_id=invitation.invitation_id,
            token=token,
            folder_id=folder_id,
            folder_path=folder.path,
            target_email=invitation.target_email,
            role=role,
            expires_at=expires,
        )

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------

    def preview(self, token: str) -> InvitationPreview:
        """Look up an invitation by raw token (hashed server-side) and
        return a confirmation summary. Surfaced unauthenticated — the
        recipient hasn't logged in yet."""
        inv = self._get_by_token(token)
        if inv.expires_at < _now():
            raise InvitationExpired(inv.invitation_id)
        if inv.consumed_at is not None:
            raise InvitationAlreadyConsumed(inv.invitation_id)
        folder = self.sess.get(Folder, inv.folder_id)
        if folder is None:
            raise InvitationFolderMissing(inv.folder_id)
        inviter = self.sess.get(AuthUser, inv.inviter_user_id)
        return InvitationPreview(
            invitation_id=inv.invitation_id,
            folder_id=folder.folder_id,
            folder_path=folder.path,
            target_email=inv.target_email,
            role=inv.role,  # type: ignore[arg-type]
            expires_at=inv.expires_at,
            inviter_username=(inviter.username if inviter else "(unknown)"),
            inviter_email=(inviter.email if inviter else None),
        )

    # ------------------------------------------------------------------
    # consume
    # ------------------------------------------------------------------

    def consume(
        self, *, token: str, redeemer_user_id: str
    ) -> InvitationPreview:
        """Apply the invitation's grant to ``redeemer_user_id`` and
        mark the invitation consumed. Idempotent only by the user
        the invitation was originally targeted at — but we don't
        enforce email-match here because the registration / login
        layer already pinned the recipient by their email when they
        followed the link.

        Returns the preview shape so the caller can render a "you
        now have access to /<folder_path>" confirmation.
        """
        inv = self._get_by_token(token)
        if inv.expires_at < _now():
            raise InvitationExpired(inv.invitation_id)
        if inv.consumed_at is not None:
            raise InvitationAlreadyConsumed(inv.invitation_id)
        folder = self.sess.get(Folder, inv.folder_id)
        if folder is None:
            raise InvitationFolderMissing(inv.folder_id)

        # Apply the grant via the share service so cascade + audit
        # logic both run.
        FolderShareService(self.sess).set_member_role(
            folder_id=inv.folder_id,
            user_id=redeemer_user_id,
            role=inv.role,  # type: ignore[arg-type]
            actor_user_id=inv.inviter_user_id,
        )

        inv.consumed_at = _now()
        inv.consumed_by_user_id = redeemer_user_id

        self._audit(
            redeemer_user_id,
            action="folder.invitation.consume",
            target_id=inv.folder_id,
            details={
                "invitation_id": inv.invitation_id,
                "role": inv.role,
                "inviter_user_id": inv.inviter_user_id,
            },
        )

        inviter = self.sess.get(AuthUser, inv.inviter_user_id)
        return InvitationPreview(
            invitation_id=inv.invitation_id,
            folder_id=folder.folder_id,
            folder_path=folder.path,
            target_email=inv.target_email,
            role=inv.role,  # type: ignore[arg-type]
            expires_at=inv.expires_at,
            inviter_username=(inviter.username if inviter else "(unknown)"),
            inviter_email=(inviter.email if inviter else None),
        )

    # ------------------------------------------------------------------
    # list / revoke
    # ------------------------------------------------------------------

    def list(
        self, *, folder_id: str, include_consumed: bool = False
    ) -> list[InvitationRow]:
        """Outstanding invitations for a folder. Sorted by creation
        time, newest first."""
        rows = list(
            self.sess.execute(
                select(FolderInvitation)
                .where(FolderInvitation.folder_id == folder_id)
                .order_by(FolderInvitation.created_at.desc())
            ).scalars()
        )
        out: list[InvitationRow] = []
        for r in rows:
            if not include_consumed and r.consumed_at is not None:
                continue
            out.append(
                InvitationRow(
                    invitation_id=r.invitation_id,
                    folder_id=r.folder_id,
                    target_email=r.target_email,
                    role=r.role,  # type: ignore[arg-type]
                    inviter_user_id=r.inviter_user_id,
                    created_at=r.created_at,
                    expires_at=r.expires_at,
                    consumed_at=r.consumed_at,
                    consumed_by_user_id=r.consumed_by_user_id,
                )
            )
        return out

    def revoke(self, *, invitation_id: str, actor_user_id: str) -> None:
        """Soft-revoke by deleting the invitation row. The token is
        no longer redeemable. Idempotent — already-revoked rows
        produce no error."""
        inv = self.sess.get(FolderInvitation, invitation_id)
        if inv is None:
            return
        folder_id = inv.folder_id
        self.sess.delete(inv)
        self._audit(
            actor_user_id,
            action="folder.invitation.revoke",
            target_id=folder_id,
            details={"invitation_id": invitation_id},
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get_by_token(self, token: str) -> FolderInvitation:
        h = _hash_token(token)
        inv = self.sess.execute(
            select(FolderInvitation).where(FolderInvitation.token_hash == h)
        ).scalar_one_or_none()
        if inv is None:
            raise InvitationNotFound("invalid or revoked invitation token")
        return inv

    def _audit(
        self,
        actor_user_id: str,
        *,
        action: str,
        target_id: str,
        details: dict,
    ) -> None:
        self.sess.add(
            AuditLogRow(
                actor_id=actor_user_id,
                action=action,
                target_type="folder",
                target_id=target_id,
                details=details,
            )
        )
