"""
PermissionService — the sole entry point for permission checks.

Phase 1 (current): single-user mode. Every check returns True. The
`folder_grants` table is seeded with a wildcard admin grant on the
root folder at migration time so future auth switches on cleanly.

Phase 2+: real ACL evaluation against folder_grants, with inheritance
up the folder tree. Switching modes is a single flag change in the
service constructor — no call-site modifications required.

Usage:

    perm = PermissionService(store, user_id="local")
    perm.require_folder(folder_id, Permission.EDIT)
    if perm.can_document(doc_id, Permission.VIEW):
        ...

All HTTP routes should route through this service. Phase 1 never
raises, Phase 2 will raise PermissionDenied when the user lacks the
required role.
"""

from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    VIEW = "view"     # list / retrieve / read
    EDIT = "edit"     # upload / rename / move within folder
    ADMIN = "admin"   # manage grants, delete folder, empty trash


class PermissionDenied(RuntimeError):
    """Raised when the current user lacks the required permission."""


class PermissionService:
    """Phase-1 always-allow implementation."""

    def __init__(self, store, *, user_id: str = "local"):
        self.store = store
        self.user_id = user_id
        # The real implementation will cache resolved grants per-request.
        self._cache: dict[tuple[str, str], bool] = {}

    # ── Folder-level ────────────────────────────────────────────────

    def can_folder(self, folder_id: str, permission: Permission) -> bool:
        # Phase 1: always allow. Phase 2 will query folder_grants with
        # inheritance up the ancestor chain.
        return True

    def require_folder(self, folder_id: str, permission: Permission) -> None:
        if not self.can_folder(folder_id, permission):
            raise PermissionDenied(
                f"user {self.user_id!r} lacks {permission.value} on folder {folder_id!r}"
            )

    # ── Document-level ──────────────────────────────────────────────
    # A document's permission IS its folder's permission. One rule,
    # no surprises.

    def can_document(self, doc_id: str, permission: Permission) -> bool:
        from .models import Document
        with self.store.transaction() as sess:
            doc = sess.get(Document, doc_id)
            if doc is None:
                return False
            return self.can_folder(doc.folder_id, permission)

    def require_document(self, doc_id: str, permission: Permission) -> None:
        if not self.can_document(doc_id, permission):
            raise PermissionDenied(
                f"user {self.user_id!r} lacks {permission.value} on document {doc_id!r}"
            )
