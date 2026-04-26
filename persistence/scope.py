"""
ScopeService — the sole entry point for folder-scope checks.

**This is not authZ.** ForgeRAG is single-tenant; there is no multi-user
access-control plan. What this service expresses is *retrieval / mutation
scope*: given an operation (read, write, manage) targeted at a folder, is
that folder currently in-scope for the caller? Right now every check
returns True — the hooks are here so we can later wire in read-only mode,
per-folder mute flags, archive-locking, etc. without touching every route.

Modes:

    READ    — list / retrieve / query under a folder
    WRITE   — upload / rename / move within a folder
    MANAGE  — destructive: empty trash, hard-delete, purge

Usage:

    scope = ScopeService(store)
    scope.require_folder(folder_id, ScopeMode.WRITE)
    if scope.can_document(doc_id, ScopeMode.READ):
        ...

``folder_id`` (not path) is the anchor on purpose: a rename / move keeps
the id stable, so any future scope flag attached to a folder rides through
path changes unaffected.
"""

from __future__ import annotations

from enum import Enum


class ScopeMode(str, Enum):
    READ = "read"  # list / retrieve / query
    WRITE = "write"  # upload / rename / move
    MANAGE = "manage"  # destructive: empty trash, hard-delete


class OutOfScope(RuntimeError):
    """Raised when a folder is not in scope for the requested mode."""


class ScopeService:
    """Phase-1 always-allow implementation. Swap in a real gate later
    without touching callsites."""

    def __init__(self, store, *, actor_id: str = "local"):
        self.store = store
        self.actor_id = actor_id
        self._cache: dict[tuple[str, str], bool] = {}

    # ── Folder-level ────────────────────────────────────────────────

    def can_folder(self, folder_id: str, mode: ScopeMode) -> bool:
        # Always True today. A future gate might consult a per-folder
        # ``archived`` / ``readonly`` flag and gate WRITE/MANAGE on it.
        return True

    def require_folder(self, folder_id: str, mode: ScopeMode) -> None:
        if not self.can_folder(folder_id, mode):
            raise OutOfScope(f"actor {self.actor_id!r} cannot {mode.value} folder {folder_id!r}")

    # ── Document-level ──────────────────────────────────────────────
    # A document's scope IS its folder's scope. One rule, no surprises.

    def can_document(self, doc_id: str, mode: ScopeMode) -> bool:
        from .models import Document

        with self.store.transaction() as sess:
            doc = sess.get(Document, doc_id)
            if doc is None:
                return False
            return self.can_folder(doc.folder_id, mode)

    def require_document(self, doc_id: str, mode: ScopeMode) -> None:
        if not self.can_document(doc_id, mode):
            raise OutOfScope(f"actor {self.actor_id!r} cannot {mode.value} document {doc_id!r}")
