"""
Per-request path remap for the per-user Spaces UI model.

Each grant a user holds becomes an independent **space**. The
user's UI surfaces (folder tree, breadcrumbs, search rows, chat
scope picker, citation paths) display paths relative to the
space root, never the absolute global path. The store still
holds absolute paths as the single source of truth for authz +
retrieval; this module only translates at the API boundary.

Phase 1 scope:
  * Build the user's space list from their grant set.
  * Compute basename collisions and assign owner-suffix
    disambiguators where needed.
  * Provide ``to_user`` / ``to_abs`` translators for the
    request lifetime.

Later phases extend the same translator across more API
surfaces (doc detail, search, chat scope, citations); the
shape doesn't need to change between phases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select

from persistence.models import Folder


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantRoot:
    """A topmost path the user has direct grant on. Nested grants
    (where a parent is also granted) are filtered out — the parent
    grant alone is enough to reach everything underneath."""

    folder_id: str
    abs_path: str
    role: str       # "rw" / "r"
    is_personal: bool   # True iff abs_path == /users/<this_user>'s_username


@dataclass(frozen=True)
class Space:
    space_id: str   # opaque, stable within a session — usually basename + disambiguator
    name: str       # display label (folder basename, or basename + " (owner)" on collision)
    abs_root: str   # absolute path of the grant root
    role: str
    is_personal: bool


# ---------------------------------------------------------------------------
# PathRemap
# ---------------------------------------------------------------------------


class PathRemap:
    """Per-request path translator. Built once per request from
    the authenticated principal's grant set. Stateless once
    constructed; the helper methods are plain dictionary lookups
    against the materialised space list.

    Construction is via ``PathRemap.build(state, principal)`` —
    the constructor itself takes the resolved grant set so the
    type is unit-testable without an AppState fixture.
    """

    def __init__(self, spaces: Iterable[Space]):
        self._spaces: list[Space] = sorted(
            spaces,
            # Personal first, then by display name. Stable order
            # so the UI presents the same layout across requests.
            key=lambda s: (not s.is_personal, s.name.lower()),
        )
        self._by_id: dict[str, Space] = {s.space_id: s for s in self._spaces}
        # Reverse index for ``to_user``: longest-prefix match.
        # We materialise the (abs_root, space) pairs sorted by
        # depth desc so a nested grant root wins over an
        # ancestor (defensive — ``user_grant_roots`` already
        # filters nested grants, but if that ever changes the
        # remap stays correct).
        self._by_abs_prefix: list[tuple[str, Space]] = sorted(
            ((s.abs_root.rstrip("/") or "/", s) for s in self._spaces),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, state, principal) -> "PathRemap":
        """Resolve the principal's grant roots, decide
        disambiguators, and build the spaces list. Empty list
        when the user has no grants — caller should render an
        empty-state.

        Auth-disabled mode (``state.cfg.auth.enabled == False``)
        is special-cased: the synthetic "local" principal sees
        every top-level folder as its own space. This preserves
        single-user dev workflows where there's no real
        ownership and admins want to see everything organised by
        natural folder hierarchy.
        """
        roots = list(_user_grant_roots(state, principal))
        return cls(_build_spaces(roots))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def spaces(self) -> list[Space]:
        return list(self._spaces)

    def get(self, space_id: str) -> Space | None:
        return self._by_id.get(space_id)

    def to_user(self, abs_path: str) -> tuple[str, str] | None:
        """Translate an absolute path to ``(space_id, rel_path)``.

        Returns None when ``abs_path`` is outside every grant the
        user holds — that's the signal to the caller that the row
        shouldn't be surfaced (treat as "not visible"). ``rel_path``
        is empty string when the abs_path equals the grant root
        itself.
        """
        if not abs_path:
            return None
        norm = abs_path if abs_path.startswith("/") else "/" + abs_path
        norm = norm.rstrip("/") or "/"
        for prefix, space in self._by_abs_prefix:
            if norm == prefix:
                return space.space_id, ""
            if norm.startswith(prefix + "/"):
                rel = norm[len(prefix) + 1:]
                return space.space_id, rel
        return None

    def to_abs(self, space_id: str, rel_path: str) -> str:
        """Inverse of ``to_user``. Raises KeyError when the
        space is unknown to this user — caller should treat as
        404, never fall through to the absolute path silently."""
        space = self._by_id.get(space_id)
        if space is None:
            raise KeyError(f"unknown space {space_id!r}")
        rel = (rel_path or "").strip("/")
        if not rel:
            return space.abs_root
        return f"{space.abs_root.rstrip('/')}/{rel}"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _user_grant_roots(state, principal) -> Iterable[GrantRoot]:
    """Topmost paths user has direct ``shared_with`` grant on.
    Excludes nested grants (if user has both /a and /a/b, only
    /a is returned). Excludes trashed folders.

    Two role-driven shortcuts:

      * Auth disabled — every non-system top-level folder is a
        Space. Single-user dev mode.
      * Admin — same shape (every non-system top-level folder is
        a Space) PLUS the admin's personal ``/users/<username>``
        marked ``is_personal=True``. Admins manage the global
        tree, so they see it as their workspace; the personal
        Space is just one entry alongside.

    Regular users follow the strict grant-walking path below:
    only folders whose ``shared_with`` lists them get yielded.
    """
    from persistence.models import AuthUser
    from persistence.models import Folder as _F

    cfg_auth = getattr(state.cfg, "auth", None)
    auth_disabled = cfg_auth is None or not getattr(cfg_auth, "enabled", False)
    user_id = principal.user_id

    with state.store.transaction() as sess:
        if auth_disabled:
            # Every direct child of root becomes a space. System
            # folders (__trash__, /users) excluded.
            rows = sess.execute(
                select(_F.folder_id, _F.path).where(
                    _F.parent_id == "__root__",
                    _F.is_system.is_(False),
                    _F.trashed_metadata.is_(None),
                )
            ).all()
            for folder_id, path in rows:
                yield GrantRoot(
                    folder_id=folder_id,
                    abs_path=path,
                    role="rw",
                    is_personal=False,  # auth-disabled has no concept of "personal"
                )
            return

        user = sess.get(AuthUser, user_id) if user_id else None

        # Admin role bypass: admins manage the workspace, so they
        # see every non-system top-level folder as a Space — same
        # behaviour as auth-disabled, plus the personal Space
        # tagged so the UI can land them there by default.
        if user is not None and user.role == "admin":
            personal_path = (
                f"/users/{user.username}" if user.username else None
            )
            rows = sess.execute(
                select(_F.folder_id, _F.path).where(
                    _F.parent_id == "__root__",
                    _F.is_system.is_(False),
                    _F.trashed_metadata.is_(None),
                )
            ).all()
            for folder_id, path in rows:
                yield GrantRoot(
                    folder_id=folder_id,
                    abs_path=path,
                    role="rw",
                    is_personal=False,
                )
            # Personal Space lives under /users/ (system folder, so
            # excluded above) — yield it separately if it exists.
            if personal_path:
                row = sess.execute(
                    select(_F.folder_id, _F.path).where(
                        _F.path == personal_path,
                        _F.trashed_metadata.is_(None),
                    )
                ).one_or_none()
                if row is not None:
                    yield GrantRoot(
                        folder_id=row[0],
                        abs_path=row[1],
                        role="rw",
                        is_personal=True,
                    )
            return

        # Regular user: walk every folder, find ones where the
        # user has a direct ``shared_with`` grant. Then dedup
        # nested grants — keep only ancestors when both ancestor
        # and descendant are granted.
        from .authz import _role_in_shared_with

        rows = sess.execute(
            select(Folder.folder_id, Folder.path, Folder.shared_with).where(
                Folder.trashed_metadata.is_(None),
            )
        ).all()
        granted: list[tuple[str, str, str]] = []
        for folder_id, path, shared_with in rows:
            role = _role_in_shared_with(shared_with or [], user_id)
            if role is not None:
                granted.append((folder_id, path, role))

        # Nested-grant filter: drop entries whose path has a
        # strict ancestor in ``granted``. Sort by depth ascending
        # so each entry sees its potential ancestors first.
        granted.sort(key=lambda r: r[1].count("/"))
        accepted_paths: list[str] = []
        accepted: list[tuple[str, str, str]] = []
        for entry in granted:
            path = entry[1]
            if any(_is_ancestor(p, path) for p in accepted_paths):
                continue
            accepted_paths.append(path)
            accepted.append(entry)

        # Compute the "is_personal" flag: this user's
        # /users/<username> folder. We resolve via the AuthUser
        # row instead of pattern-matching on path so the literal
        # /users/ prefix isn't load-bearing — admins could
        # rename the parent folder later without breaking this.
        # ``user`` was already fetched at the top of this block
        # for the role check.
        personal_path = (
            f"/users/{user.username}" if user and user.username else None
        )

        for folder_id, path, role in accepted:
            yield GrantRoot(
                folder_id=folder_id,
                abs_path=path,
                role=role,
                is_personal=(personal_path is not None and path == personal_path),
            )


def _is_ancestor(maybe_ancestor: str, descendant: str) -> bool:
    """``/a`` is an ancestor of ``/a/b`` (and ``/a/b/c``) but
    NOT of ``/ab``. Trailing slashes normalised so ``/a/`` and
    ``/a`` behave identically."""
    a = maybe_ancestor.rstrip("/") or "/"
    d = descendant.rstrip("/") or "/"
    if a == d:
        return False
    if a == "/":
        return True
    return d.startswith(a + "/")


def _build_spaces(roots: Iterable[GrantRoot]) -> list[Space]:
    """Resolve grant roots into displayable Space records,
    appending owner-suffix disambiguators where multiple roots
    collide on basename.

    The disambiguator is ``" (basename of parent path)"`` for
    Phase 1 — keeps the suffix semantically meaningful without
    leaking arbitrary segments. Example: two grants on
    ``/projects/q4`` and ``/eng/q4`` collide at basename ``q4``,
    so they render as ``q4 (projects)`` and ``q4 (eng)``.
    """
    roots = list(roots)
    if not roots:
        return []

    # Group by basename; any group with >1 entries needs
    # disambiguation.
    by_basename: dict[str, list[GrantRoot]] = {}
    for r in roots:
        bn = os.path.basename(r.abs_path.rstrip("/")) or r.abs_path
        by_basename.setdefault(bn, []).append(r)

    out: list[Space] = []
    for basename, group in by_basename.items():
        if len(group) == 1:
            r = group[0]
            out.append(Space(
                space_id=basename,
                name=basename,
                abs_root=r.abs_path,
                role=r.role,
                is_personal=r.is_personal,
            ))
            continue
        # Collision — append parent-folder hint to each.
        for r in group:
            parent = os.path.basename(
                os.path.dirname(r.abs_path.rstrip("/"))
            )
            disambiguator = parent or "/"
            out.append(Space(
                space_id=f"{basename}__{disambiguator}",
                name=f"{basename} ({disambiguator})",
                abs_root=r.abs_path,
                role=r.role,
                is_personal=r.is_personal,
            ))
    return out
