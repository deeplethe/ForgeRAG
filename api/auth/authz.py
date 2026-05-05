"""
Authorization service — folder-level grants + admin bypass.

Authentication (who you are) lives in ``api/auth/middleware.py``.
This module is *authorization* (what you're allowed to do) and is the
single source of truth for every search-bearing API.

Three public methods:

    can(user_id, folder_id, action) -> bool
        The atomic permission check. Walks the folder + every
        ancestor and returns the strongest ``shared_with`` grant on
        the chain; ``rw`` covers every action, ``r`` covers reads
        only.

    resolve_paths(user_id, requested) -> list[str]
        Turns an optional ``path_filters: list[str] | None`` from the
        request body into the concrete list of path prefixes the
        retrieval pipeline should scope to. Validates every requested
        path against the user's accessible set; raises
        ``UnauthorizedPath`` on the first violation. ``None`` falls
        back to the user's minimal accessible spanning set.

    list_accessible_folders(user_id) -> list[Folder]
        Used by the sidebar / scope picker to render the user's
        folder tree. Excludes folders inside trash.

Action vocabulary (frozensets exported alongside the service):

    READ_ACTIONS    — search, list documents, view chunks
    WRITE_ACTIONS   — upload, edit, soft-delete, rename
    MANAGE_ACTIONS  — edit shared_with, purge trash, delete folder

Permission matrix (non-admin):

    Action  | rw  | r  | other
    --------|-----|----|------
    READ    |  ✓  | ✓  |
    WRITE   |  ✓  |    |
    MANAGE  |  ✓  |    |

``rw`` covers every action — there is no separate "owner" tier; the
single design knob is "is this user in shared_with at rw level on
this folder or some ancestor." Admin role bypasses all of the above
on every folder. Privacy-sensitive surfaces (conversations,
research sessions) check user_id directly and do NOT use admin
bypass — they're the user's, not the org's.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select

from persistence.models import AuthUser, Folder

# ---------------------------------------------------------------------------
# Action vocabulary
# ---------------------------------------------------------------------------

# Reading content / listing — anyone with at least 'r'.
READ_ACTIONS = frozenset({"read", "search", "list"})
# Writing content — owner or 'rw'.
WRITE_ACTIONS = frozenset({"upload", "edit", "soft_delete", "rename"})
# Folder management — owner only (admin bypass still applies).
MANAGE_ACTIONS = frozenset(
    {"share", "transfer", "purge", "delete_folder", "set_grant"}
)

Action = Literal[
    # read
    "read", "search", "list",
    # write
    "upload", "edit", "soft_delete", "rename",
    # manage
    "share", "transfer", "purge", "delete_folder", "set_grant",
]

# Path under which trashed content lives. Mirrors
# ``persistence.folder_service.TRASH_PATH`` without importing — keep
# this module dependency-light.
_TRASH_PREFIX = "/__trash__"


def _action_allowed_for(role: str, action: Action) -> bool:
    """Map (role, action) to allow / deny under the new role matrix.

    rw covers everything, r covers reads only. Anything else is
    rejected. Used by ``can()`` once the strongest grant on the
    ancestor chain has been determined.
    """
    if role == "rw":
        return action in READ_ACTIONS or action in WRITE_ACTIONS or action in MANAGE_ACTIONS
    if role == "r":
        return action in READ_ACTIONS
    return False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthzError(Exception):
    """Base for authorization failures. Caught by the route layer
    and turned into 403."""


class UnauthorizedPath(AuthzError):
    """Raised by ``resolve_paths`` when a requested path is outside
    the user's accessible set. The offending path is named so the
    client can show a precise error."""

    def __init__(self, path: str):
        self.path = path
        super().__init__(f"no access to path: {path}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _role_in_shared_with(shared_with: list, user_id: str) -> str | None:
    """Look up a user's role in a folder's shared_with list. Returns
    the role string ('r' or 'rw') or None if the user isn't listed."""
    if not shared_with:
        return None
    for entry in shared_with:
        if not isinstance(entry, dict):
            continue
        if entry.get("user_id") == user_id:
            r = entry.get("role")
            if r in ("r", "rw"):
                return r
    return None


def _path_starts_with(child: str, parent: str) -> bool:
    """True iff ``parent`` is an ancestor (inclusive) of ``child`` in
    the folder tree. ``parent='/'`` matches anything."""
    if parent in ("/", child):
        return True
    parent = parent.rstrip("/")
    return child == parent or child.startswith(parent + "/")


def minimize_paths(paths: list[str]) -> list[str]:
    """Drop redundant subpaths. ``['/a', '/a/b', '/x']`` → ``['/a', '/x']``.

    The retrieval-layer OR-clause shrinks accordingly; '/' subsumes
    everything (output collapses to ``['/']``). Output is sorted
    lexically so callers and tests can rely on a stable order.
    """
    if not paths:
        return []
    # Dedup, sort by length so shorter (potential ancestors) come first.
    uniq = sorted(set(paths), key=lambda p: (len(p), p))
    out: list[str] = []
    for p in uniq:
        if any(_path_starts_with(p, existing) for existing in out):
            continue
        out.append(p)
    return sorted(out)


# ---------------------------------------------------------------------------
# AuthorizationService
# ---------------------------------------------------------------------------


class AuthorizationService:
    """Stateless orchestrator over the relational store.

    Constructed once per process (in AppState) and shared across
    requests. Holds no per-call state — every method opens its own
    short transaction.
    """

    def __init__(self, store):
        self._store = store

    # ------------------------------------------------------------------
    # can()
    # ------------------------------------------------------------------

    def can(self, user_id: str, folder_id: str, action: Action) -> bool:
        """Return True iff ``user_id`` may perform ``action`` on
        ``folder_id``. False on disabled / suspended users, missing
        folders, or insufficient grant. Admin role bypasses all
        non-privacy checks.

        Resolution walks the folder + every ancestor and picks the
        strongest ``shared_with`` grant on the chain. ``rw`` covers
        every action (READ / WRITE / MANAGE); ``r`` covers READ
        only. The walk early-terminates on a first ``rw`` hit since
        no stronger grant exists.

        The ancestor walk is a defensive read-side fallback —
        cascade keeps each folder's ``shared_with`` materialised so
        a single-folder lookup is usually sufficient. The walk
        ensures correctness when cascade hasn't run (legacy data,
        manual SQL fixes, etc.).
        """
        with self._store.transaction() as sess:
            user = sess.get(AuthUser, user_id)
            if user is None:
                return False
            if user.status != "active" or not user.is_active:
                return False
            if user.role == "admin":
                return True

            cur = sess.get(Folder, folder_id)
            if cur is None:
                return False

            best_role: str | None = None  # 'r' / 'rw' / None
            while cur is not None:
                role = _role_in_shared_with(cur.shared_with or [], user_id)
                if role == "rw":
                    return _action_allowed_for("rw", action)
                if role == "r" and best_role is None:
                    best_role = "r"
                cur = (
                    sess.get(Folder, cur.parent_id)
                    if cur.parent_id
                    else None
                )

            if best_role is None:
                return False
            return _action_allowed_for(best_role, action)

    # ------------------------------------------------------------------
    # resolve_paths()
    # ------------------------------------------------------------------

    def resolve_paths(
        self, user_id: str, requested: list[str] | None
    ) -> list[str]:
        """Turn an optional request-side ``path_filters`` list into the
        concrete prefixes retrieval should run against.

        ``requested=None`` (or empty) → user's minimal accessible
        spanning set; e.g. a user with grants on ``/legal/2024`` and
        ``/research`` gets ``['/legal/2024', '/research']``.

        ``requested=[...]`` → each entry is validated against the
        user's accessible set. Admins bypass validation (they may
        scope to any path including ``/``). On the first
        unauthorised path we raise ``UnauthorizedPath`` so the
        caller can return a 403 naming the path.

        NOTE: there is no hard cap on list length. Each entry expands
        to one OR-clause downstream:

          * Postgres: ``OR documents.path LIKE :p``  — comfortable up
            to a couple hundred clauses.
          * Chroma: ``$or`` list — slows past ~100 entries.
          * Neo4j: Cypher OR — scales but the explain plan widens.

        The minimal-spanning compression already drops redundant
        subfolders, so a user with 100 sibling grants probably has
        a layout problem rather than a missing cap. Telemetry should
        track ``auth.path_filters_count`` so we can spot pathological
        cases without blocking legit ones.
        """
        with self._store.transaction() as sess:
            user = sess.get(AuthUser, user_id)
            if user is None or user.status != "active" or not user.is_active:
                # Unknown / suspended user has no scope.
                raise UnauthorizedPath(
                    requested[0] if requested else "/"
                )

            # Admin: no validation; pass requested through untouched.
            # Default scope (no explicit list) is the whole tree —
            # admins can search everything by virtue of role bypass,
            # and their own accessible_paths set is typically empty
            # (no shared_with grants needed) so falling back to that
            # would yield an empty search by default.
            if user.role == "admin":
                if requested:
                    return list(requested)
                return ["/"]

            accessible = self._accessible_paths(sess, user_id)

        if not requested:
            return minimize_paths(accessible)

        for r in requested:
            if not any(_path_starts_with(r, a) for a in accessible):
                raise UnauthorizedPath(r)
        return list(requested)

    # ------------------------------------------------------------------
    # list_accessible_folders()
    # ------------------------------------------------------------------

    def list_accessible_folders(self, user_id: str) -> list[Folder]:
        """Folders where the user has at least 'r' access. Trashed
        folders excluded. Admins see every non-trashed folder."""
        with self._store.transaction() as sess:
            user = sess.get(AuthUser, user_id)
            if user is None:
                return []
            if user.status != "active" or not user.is_active:
                return []

            stmt = select(Folder).where(
                Folder.trashed_metadata.is_(None),
                ~Folder.path.like(_TRASH_PREFIX + "%"),
            )
            if user.role == "admin":
                return list(sess.execute(stmt).scalars())

            # Non-admin: filter in Python — cross-dialect JSON ops
            # are awkward, total folder count is small (< 10K typical
            # before this becomes a hot path), and a GIN-index
            # optimisation on ``(shared_with->'user_id')`` is
            # reserved for when telemetry shows it paying off.
            #
            # The cascade keeps shared_with materialised on every
            # descendant, so a direct membership check is sufficient
            # — we don't need the ancestor walk here.
            rows = sess.execute(stmt).scalars().all()
            return [
                f for f in rows
                if _role_in_shared_with(f.shared_with or [], user_id) is not None
            ]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _accessible_paths(self, sess, user_id: str) -> list[str]:
        """The list of folder paths where ``user_id`` has any
        ``shared_with`` grant, excluding trashed content. Used by
        ``resolve_paths``."""
        rows = sess.execute(
            select(Folder.path, Folder.shared_with).where(
                Folder.trashed_metadata.is_(None),
                ~Folder.path.like(_TRASH_PREFIX + "%"),
            )
        ).all()
        return [
            path
            for path, shared in rows
            if _role_in_shared_with(shared or [], user_id) is not None
        ]
