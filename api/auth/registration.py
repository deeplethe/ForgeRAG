"""
Self-registration logic for multi-user mode.

The route layer (``api/routes/auth.py``) hands raw request bodies
to ``register_user``; this module handles validation, the
registration-mode policy, the "first-user-becomes-admin" bootstrap,
and atomic invitation consumption.

Three registration modes (config: ``auth.registration_mode``):

    open         — anyone with a valid email can register and use
                   the system immediately.
    approval     — registration creates ``status=pending_approval``;
                   admin approves before login works. Default.
    invite_only  — registration is rejected unless the request
                   carries a valid invitation token, in which case
                   the user lands ``active`` and the invitation is
                   consumed atomically.

First-user override (always-on):

    When ``auth_users`` has zero rows where ``role='admin' AND
    status='active'``, the FIRST successful registration is
    promoted to admin + active regardless of registration_mode.
    This covers the empty-deploy bootstrap path (``initial_password``
    skipped, no admin pre-provisioned). Concurrent first-registrations
    are gated by the username UNIQUE index — only one INSERT wins.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select

from persistence.invitation_service import (
    FolderInvitationService,
    InvitationAlreadyConsumed,
    InvitationError,
    InvitationExpired,
    InvitationFolderMissing,
    InvitationNotFound,
)
from persistence.models import AuthUser, Folder

from .primitives import hash_password

log = logging.getLogger(__name__)

# Permissive email check: present + has '@' + at least one '.' on the
# right-hand side. We don't try to validate full RFC 5322 — the only
# downstream consumer is the typeahead-on-the-Members-panel matcher,
# which compares verbatim. Real address verification belongs to a
# future SMTP layer (v2).
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# Username constraints: 3–32 chars, alnum + underscore + hyphen.
# Stricter than email so URLs that include the username stay clean.
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RegistrationError(Exception):
    """Base for typed registration failures the route layer maps to
    HTTP status codes (mostly 400 / 409)."""


class InvalidEmail(RegistrationError):
    pass


class InvalidUsername(RegistrationError):
    pass


class WeakPassword(RegistrationError):
    pass


class EmailTaken(RegistrationError):
    pass


class UsernameTaken(RegistrationError):
    pass


class RegistrationClosed(RegistrationError):
    """Raised when ``registration_mode`` blocks the request — open
    mode never raises this; approval / invite_only do under their
    respective conditions."""


class InvitationProblem(RegistrationError):
    """Wraps invitation-service errors so the route can return 400
    with a precise message. The underlying error class is preserved
    on ``.cause``."""

    def __init__(self, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class RegistrationResult:
    user_id: str
    username: str
    email: str
    display_name: str | None
    role: Literal["admin", "user"]
    status: Literal["active", "pending_approval"]
    # When the user redeemed an invitation, this is the folder they
    # got access to. The route surfaces it so the client can route
    # straight into that folder after login.
    redeemed_folder_path: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


# Parent path under which every user's personal space lives. The
# Spaces UI translates this prefix away (Phase 2) — users never see
# the literal ``/users/`` segment, just their space root rendered as
# ``/``. See docs/roadmaps/per-user-spaces.md.
_USERS_PARENT_PATH = "/users"


def ensure_personal_folder(sess, *, user_id: str, username: str) -> Folder:
    """Idempotently create ``/users/<username>`` with a ``rw`` grant
    for the given user. Used by registration to give every new
    account a personal workspace; also safe to call from a backfill
    helper for accounts that pre-date this guarantee.

    Two folders may need creating:

      * ``/users/`` — the parent. Marked ``is_system=True`` so it
        never surfaces as a top-level Space (no one has a grant on
        it; ``PathRemap`` would skip it anyway, but the system flag
        also keeps it out of any future "list all top-level
        folders" endpoint).
      * ``/users/<username>`` — the personal Space. ``shared_with``
        contains exactly one entry granting ``rw`` to ``user_id``.

    On collision (folder already exists from a prior run, or two
    workers raced) we just ensure the grant is present and return
    the existing row.
    """
    from sqlalchemy import select as _select

    # 1) Ensure /users/ parent.
    parent = sess.execute(
        _select(Folder).where(Folder.path == _USERS_PARENT_PATH)
    ).scalar_one_or_none()
    if parent is None:
        parent = Folder(
            folder_id=_new_id(),
            path=_USERS_PARENT_PATH,
            path_lower=_USERS_PARENT_PATH.lower(),
            parent_id="__root__",
            name="users",
            is_system=True,
            metadata_json={},
            shared_with=[],
        )
        sess.add(parent)
        sess.flush()

    # 2) Ensure /users/<username>.
    personal_path = f"{_USERS_PARENT_PATH}/{username}"
    personal = sess.execute(
        _select(Folder).where(Folder.path == personal_path)
    ).scalar_one_or_none()

    grant = {"user_id": user_id, "role": "rw"}
    if personal is None:
        personal = Folder(
            folder_id=_new_id(),
            path=personal_path,
            path_lower=personal_path.lower(),
            parent_id=parent.folder_id,
            name=username,
            is_system=False,
            metadata_json={},
            shared_with=[grant],
        )
        sess.add(personal)
        sess.flush()
        log.info(
            "auth: created personal folder %s for user_id=%s",
            personal_path, user_id,
        )
    else:
        # Folder pre-existed (admin pre-created it, or backfill
        # racing). Ensure the user's grant is present without
        # clobbering any other grants the folder may already carry.
        existing = list(personal.shared_with or [])
        if not any(g.get("user_id") == user_id for g in existing):
            existing.append(grant)
            personal.shared_with = existing
            sess.flush()
            log.info(
                "auth: added rw grant for user_id=%s to existing %s",
                user_id, personal_path,
            )
    return personal


def _has_active_admin(sess) -> bool:
    """Whether at least one row in ``auth_users`` is an active
    admin. Used by the first-registration auto-promotion check."""
    n = sess.execute(
        select(func.count())
        .select_from(AuthUser)
        .where(
            AuthUser.role == "admin",
            AuthUser.status == "active",
            AuthUser.is_active.is_(True),
        )
    ).scalar() or 0
    return n > 0


def _normalise_email(email: str) -> str:
    return email.strip().lower()


def _validate_inputs(*, email: str, username: str, password: str) -> None:
    if not _EMAIL_RE.match(email):
        raise InvalidEmail(f"invalid email: {email!r}")
    if not _USERNAME_RE.match(username):
        raise InvalidUsername(
            "username must be 3–32 chars, letters / digits / underscore / hyphen"
        )
    if len(password) < 8:
        raise WeakPassword("password must be at least 8 characters")


# ---------------------------------------------------------------------------
# register_user
# ---------------------------------------------------------------------------


def register_user(
    *,
    cfg,
    sess,
    email: str,
    username: str,
    password: str,
    display_name: str | None = None,
    invitation_token: str | None = None,
) -> RegistrationResult:
    """The single entry point for self-registration.

    ``cfg`` is the full ``AppConfig`` (we read ``cfg.auth``).
    ``sess`` is an open SQLAlchemy session — caller owns the
    transaction so registration + invitation consumption commit
    atomically.

    Returns a ``RegistrationResult``; on any failure raises a
    typed ``RegistrationError`` subclass for the route layer to
    map.
    """
    email = _normalise_email(email)
    _validate_inputs(email=email, username=username, password=password)

    # Uniqueness checks before mode policy so we always give the
    # caller a precise error rather than "registration closed" when
    # the real reason is "email taken".
    if sess.execute(
        select(AuthUser).where(AuthUser.email == email)
    ).scalar_one_or_none() is not None:
        raise EmailTaken(f"email already registered: {email!r}")
    # Username is explicit + immutable, so a collision is the
    # caller's problem to fix — surface a precise error rather
    # than mutating their requested name behind their back.
    if sess.execute(
        select(AuthUser).where(AuthUser.username == username)
    ).scalar_one_or_none() is not None:
        raise UsernameTaken(f"username already taken: {username!r}")

    # Resolve registration policy.
    has_admin = _has_active_admin(sess)
    invitation_preview = None
    if invitation_token:
        # Validate the invitation here so we can short-circuit policy
        # for valid invites (auto-active, regardless of mode). We
        # don't consume yet — that happens after the user row exists.
        try:
            invitation_preview = FolderInvitationService(sess).preview(invitation_token)
        except InvitationNotFound as e:
            raise InvitationProblem("invalid or revoked invitation", cause=e)
        except InvitationExpired as e:
            raise InvitationProblem("invitation expired", cause=e)
        except InvitationAlreadyConsumed as e:
            raise InvitationProblem("invitation already used", cause=e)
        except InvitationFolderMissing as e:
            raise InvitationProblem(
                "invitation's folder no longer exists", cause=e
            )

    # First-user-becomes-admin: bypass mode + invitation requirements.
    promote_to_admin = not has_admin

    if not promote_to_admin and invitation_preview is None:
        # No invitation, not the first user — apply registration_mode.
        mode = cfg.auth.registration_mode
        if mode == "invite_only":
            raise RegistrationClosed(
                "registration is invite-only — get an invitation link "
                "from a folder owner first"
            )
        # 'open' lets it through; 'approval' lets it through but
        # marks pending_approval below.

    # Build the user row.
    if promote_to_admin:
        role: Literal["admin", "user"] = "admin"
        status: Literal["active", "pending_approval"] = "active"
    else:
        role = "user"
        # invitation present → auto-active (the invitation IS the trust signal);
        # otherwise mode decides.
        if invitation_preview is not None or cfg.auth.registration_mode == "open":
            status = "active"
        else:  # approval
            status = "pending_approval"

    user_id = _new_id()
    user = AuthUser(
        user_id=user_id,
        username=username,
        email=email,
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
        status=status,
        is_active=(status == "active"),
        must_change_password=False,
    )
    sess.add(user)
    sess.flush()  # surface the row before invitation consume reads it

    # Auto-create the user's personal Space (``/users/<username>``)
    # with an ``rw`` grant. Every account gets a personal home —
    # users never start out with zero spaces. See per-user-spaces
    # roadmap. Idempotent: if the folder already exists (admin
    # pre-created, or backfill ran), we just ensure the grant is
    # present.
    ensure_personal_folder(sess, user_id=user_id, username=username)

    # Consume the invitation atomically with the new user row.
    redeemed_folder_path: str | None = None
    if invitation_preview is not None:
        try:
            consumed = FolderInvitationService(sess).consume(
                token=invitation_token,  # type: ignore[arg-type]
                redeemer_user_id=user_id,
            )
            redeemed_folder_path = consumed.folder_path
        except InvitationError as e:
            # Should be impossible given we previewed successfully a
            # few lines ago, but if a race lost it (someone else
            # consumed first) we surface it cleanly.
            raise InvitationProblem(
                "invitation could not be redeemed", cause=e
            )

    if promote_to_admin:
        log.info(
            "auth: first-registration → user %r (id=%s) auto-promoted to admin",
            username,
            user_id,
        )

    return RegistrationResult(
        user_id=user_id,
        username=username,
        email=email,
        display_name=display_name,
        role=role,
        status=status,
        redeemed_folder_path=redeemed_folder_path,
    )
