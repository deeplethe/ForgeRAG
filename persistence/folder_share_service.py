"""
FolderShareService — folder membership management with cascading
superset rules.

Membership lives in ``folders.shared_with`` (JSON list of
``{"user_id", "role"}`` entries). The invariant the service
maintains is:

    For every user U and every folder F, F.shared_with[U] >= F.parent.shared_with[U]
    (where "no grant" < "r" < "rw").

This is what makes path-prefix retrieval correct: a query scoped to
``/legal`` can include every chunk under that prefix without having
to walk the subtree at query time and double-check each subfolder.
The invariant is maintained at write time, so reads stay cheap.

Operations:

    set_member_role(folder_id, user_id, role)
        Add a member to F at ``role``, OR upgrade their existing role.
        Cascades to descendants: any descendant where the user has a
        weaker (or no) grant is upgraded to ``role`` to preserve
        the superset invariant. Descendants that already have a
        stronger grant (e.g. an explicit subfolder upgrade to rw
        while parent grants only r) are left alone.

    remove_member(folder_id, user_id)
        Remove a member from F (and recursively from descendants).
        Rejected if the user is in some ancestor's shared_with — the
        service raises ``MembershipConstraintError`` with a hint
        about which ancestor still grants access. The caller has to
        either remove the ancestor grant first, or move the subtree
        out from under the granting ancestor.

    list_members(folder_id) -> [{user_id, role, source}]
        Materialised view of effective membership. ``source`` is
        ``"direct"`` / ``"inherited:<ancestor folder_id>"`` so the UI
        can show where each row came from.

All operations write an entry to ``audit_log`` with the actor /
target / before-after grants for forensics.

Roles are just ``r`` (read-only) and ``rw`` (everything: read +
write + manage shared_with + delete folder + purge trash). There is
no separate "owner" tier — the design originally had one but it
added complexity without buying anything ``rw`` doesn't already
cover. ``role=admin`` on ``auth_users`` bypasses every per-folder
check globally, which is the only escape hatch.

The AuthorizationService's ``can()`` is the read-side counterpart;
this module is the write side. Routes in ``api/routes/folders.py``
(S3.b) are the only callers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLogRow, AuthUser, Folder

log = logging.getLogger(__name__)

Role = Literal["r", "rw"]

# Numeric weights for monotonic comparisons. Higher = more permissive.
_ROLE_WEIGHT: dict[str, int] = {"r": 1, "rw": 2}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FolderShareError(Exception):
    """Base class for share-management errors."""


class MembershipConstraintError(FolderShareError):
    """Raised when a removal would violate the subfolder-superset
    rule (i.e. the user is still in an ancestor's shared_with)."""


class FolderNotFound(FolderShareError):
    pass


class UserNotFound(FolderShareError):
    pass


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class MemberRow:
    """One row in the materialised membership view."""

    user_id: str
    username: str
    email: str | None
    display_name: str | None
    role: Role
    # ``"direct"`` for a grant on this folder; ``"inherited:<folder_id>"``
    # for grants cascaded down from an ancestor.
    source: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _grant_for(shared_with: list, user_id: str) -> str | None:
    """Return the user's role in a shared_with list, or None."""
    if not shared_with:
        return None
    for entry in shared_with:
        if isinstance(entry, dict) and entry.get("user_id") == user_id:
            r = entry.get("role")
            if r in ("r", "rw"):
                return r
    return None


def _set_grant(shared_with: list, user_id: str, role: Role) -> list:
    """Return a new list with ``user_id`` set to ``role`` (replacing
    any existing entry)."""
    out = [
        e for e in (shared_with or [])
        if not (isinstance(e, dict) and e.get("user_id") == user_id)
    ]
    out.append({"user_id": user_id, "role": role})
    return out


def _drop_grant(shared_with: list, user_id: str) -> list:
    return [
        e for e in (shared_with or [])
        if not (isinstance(e, dict) and e.get("user_id") == user_id)
    ]


class FolderShareService:
    """Stateless once constructed; the SQLAlchemy session is the only
    state. Instantiate per-request inside the route's transaction."""

    def __init__(self, sess: Session):
        self.sess = sess

    # ------------------------------------------------------------------
    # set_member_role
    # ------------------------------------------------------------------

    def set_member_role(
        self,
        *,
        folder_id: str,
        user_id: str,
        role: Role,
        actor_user_id: str,
    ) -> None:
        """Add or upgrade a user's role on a folder; cascade to
        descendants so the superset invariant holds.

        Adding the existing owner of a folder is a no-op (their
        owner role is already strictly stronger). Adding a user
        who already has the same or higher role is a no-op too —
        we don't write a duplicate audit row in that case.
        """
        if role not in ("r", "rw"):
            raise FolderShareError(f"invalid role: {role!r}")
        folder = self._require_folder(folder_id)
        self._require_user(user_id)

        before = _grant_for(folder.shared_with or [], user_id)
        if before == role:
            return  # idempotent
        # Downgrade attempt would violate ancestor superset; reject.
        ancestor_role = self._strongest_ancestor_role(folder, user_id)
        if ancestor_role and _ROLE_WEIGHT[role] < _ROLE_WEIGHT[ancestor_role]:
            raise MembershipConstraintError(
                f"role {role!r} weaker than ancestor grant {ancestor_role!r}; "
                f"either pick a stronger role or remove the ancestor grant first"
            )

        # Apply on the folder itself.
        folder.shared_with = _set_grant(folder.shared_with or [], user_id, role)

        # Cascade: every descendant needs at least ``role`` for this
        # user. Stronger-existing-grants stay (more permissive is
        # always allowed).
        for desc in self._descendants(folder):
            existing = _grant_for(desc.shared_with or [], user_id)
            if existing and _ROLE_WEIGHT[existing] >= _ROLE_WEIGHT[role]:
                continue
            desc.shared_with = _set_grant(desc.shared_with or [], user_id, role)

        self._audit(
            actor_user_id,
            action="folder.member.set",
            target_id=folder_id,
            details={
                "user_id": user_id,
                "role_before": before,
                "role_after": role,
            },
        )

    # ------------------------------------------------------------------
    # remove_member
    # ------------------------------------------------------------------

    def remove_member(
        self,
        *,
        folder_id: str,
        user_id: str,
        actor_user_id: str,
    ) -> None:
        """Drop a user's grant from F and all descendants.

        Rejected when the user still has access via an ancestor's
        ``shared_with`` — removing here would leave F with weaker
        rights than its parent, breaking the superset rule.
        """
        folder = self._require_folder(folder_id)

        ancestor_id = self._ancestor_granting(folder, user_id)
        if ancestor_id is not None:
            raise MembershipConstraintError(
                f"user {user_id!r} still has access via ancestor folder "
                f"{ancestor_id!r}; remove from the ancestor first or move "
                f"this folder out from under it"
            )

        before = _grant_for(folder.shared_with or [], user_id)
        if before is None and not any(
            _grant_for(d.shared_with or [], user_id) is not None
            for d in self._descendants(folder)
        ):
            return  # idempotent — nothing to remove anywhere

        # Apply on F and every descendant.
        folder.shared_with = _drop_grant(folder.shared_with or [], user_id)
        for desc in self._descendants(folder):
            desc.shared_with = _drop_grant(desc.shared_with or [], user_id)

        self._audit(
            actor_user_id,
            action="folder.member.remove",
            target_id=folder_id,
            details={
                "user_id": user_id,
                "role_before": before,
                "cascade": True,
            },
        )

    # ------------------------------------------------------------------
    # list_members
    # ------------------------------------------------------------------

    def list_members(self, folder_id: str) -> list[MemberRow]:
        """Return every effective member of a folder — every grant in
        shared_with, labelled by where it originated.

        Cascading writes the same role onto every descendant, so a
        "direct" grant on a subfolder might in fact be a copy of a
        parent grant. We detect that by walking ancestors: when the
        nearest ancestor with a grant for the user has the SAME role,
        the grant on this folder was cascaded in (label
        ``inherited:<ancestor_id>``); when the ancestor has a weaker
        role (or none), this folder's grant was added or upgraded
        directly here (label ``direct``).

        The label drives the Members panel UI: only "direct" rows
        are editable on this folder; "inherited" rows must be
        edited upstream.
        """
        folder = self._require_folder(folder_id)
        rows: dict[str, MemberRow] = {}

        for entry in folder.shared_with or []:
            if not isinstance(entry, dict):
                continue
            uid = entry.get("user_id")
            role = entry.get("role")
            if uid is None or role not in ("r", "rw") or uid in rows:
                continue
            user = self.sess.get(AuthUser, uid)
            if user is None:
                continue

            ancestor_id, ancestor_role = self._nearest_ancestor_grant(folder, uid)
            if ancestor_role == role:
                source = f"inherited:{ancestor_id}"
            else:
                source = "direct"

            rows[uid] = MemberRow(
                user_id=uid,
                username=user.username,
                email=user.email,
                display_name=user.display_name,
                role=role,
                source=source,
            )

        # Stable order: by username.
        return sorted(rows.values(), key=lambda r: r.username)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_folder(self, folder_id: str) -> Folder:
        folder = self.sess.get(Folder, folder_id)
        if folder is None:
            raise FolderNotFound(f"folder not found: {folder_id!r}")
        return folder

    def _require_user(self, user_id: str) -> AuthUser:
        user = self.sess.get(AuthUser, user_id)
        if user is None:
            raise UserNotFound(f"user not found: {user_id!r}")
        return user

    def _descendants(self, folder: Folder) -> list[Folder]:
        """Every folder whose path starts with ``folder.path/``.

        Excludes ``folder`` itself. Single SQL query against the
        denormalised path column, indexed by
        ``ix_folders_path``.
        """
        prefix = folder.path.rstrip("/")
        if prefix == "":
            # Root: descendants are everything else.
            return list(
                self.sess.execute(
                    select(Folder).where(Folder.folder_id != folder.folder_id)
                ).scalars()
            )
        return list(
            self.sess.execute(
                select(Folder).where(Folder.path.like(prefix + "/%"))
            ).scalars()
        )

    def _strongest_ancestor_role(
        self, folder: Folder, user_id: str
    ) -> str | None:
        """Walk up to root and return the strongest grant the user
        already has via an ancestor's ``shared_with``. None when
        the user has no ancestor access."""
        cur = self.sess.get(Folder, folder.parent_id) if folder.parent_id else None
        best: str | None = None
        while cur is not None:
            r = _grant_for(cur.shared_with or [], user_id)
            if r is not None and (
                best is None or _ROLE_WEIGHT[r] > _ROLE_WEIGHT[best]
            ):
                best = r
                if best == "rw":
                    break  # strictly strongest; short-circuit
            cur = (
                self.sess.get(Folder, cur.parent_id) if cur.parent_id else None
            )
        return best

    def _nearest_ancestor_grant(
        self, folder: Folder, user_id: str
    ) -> tuple[str | None, str | None]:
        """Walk up to root and return the (folder_id, role) of the
        nearest ancestor where ``user_id`` has a ``shared_with``
        grant. Returns ``(None, None)`` when no ancestor grants
        access.

        Used by ``list_members`` to label rows as "direct" vs
        "inherited from ancestor X" — the UI points at the named
        ancestor as the place to edit upstream.
        """
        cur = self.sess.get(Folder, folder.parent_id) if folder.parent_id else None
        while cur is not None:
            r = _grant_for(cur.shared_with or [], user_id)
            if r is not None:
                return cur.folder_id, r
            cur = (
                self.sess.get(Folder, cur.parent_id) if cur.parent_id else None
            )
        return None, None

    def _ancestor_granting(
        self, folder: Folder, user_id: str
    ) -> str | None:
        """Return the folder_id of the nearest ancestor whose
        ``shared_with`` includes ``user_id``, or None. Used by
        remove_member to decide whether removal would violate the
        superset rule."""
        cur = self.sess.get(Folder, folder.parent_id) if folder.parent_id else None
        while cur is not None:
            if _grant_for(cur.shared_with or [], user_id) is not None:
                return cur.folder_id
            cur = (
                self.sess.get(Folder, cur.parent_id) if cur.parent_id else None
            )
        return None

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
