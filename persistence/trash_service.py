"""
TrashService — restore / permanent delete / auto-purge.

Works on the /__trash__ folder subtree populated by FolderService.move_to_trash
and the soft-delete path for documents. Permanent deletion cascades through
the relational DB, vector store, BM25 index, and KG graph store.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select

from .folder_service import (
    ROOT_FOLDER_ID,
    TRASH_FOLDER_ID,
    TRASH_PATH,
    FolderAlreadyExists,
    FolderError,
    FolderNotFound,
    FolderService,
    join_path,
    unique_document_path,
)
from .models import AuditLogRow, Document, Folder

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


def _folder_in_trash(f: Folder) -> bool:
    return f.path == TRASH_PATH or f.path.startswith(TRASH_PATH + "/")


def _doc_in_trash(d: Document) -> bool:
    return d.path.startswith(TRASH_PATH + "/") or d.path == TRASH_PATH


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TrashService:
    """
    High-level trash operations. Uses state.store for the relational
    session, state.vector / state.graph_store for downstream cleanup
    on permanent delete, state.refresh_bm25 for BM25 refresh.
    """

    def __init__(self, state, *, actor_id: str = "local"):
        self.state = state
        self.store = state.store
        self.actor_id = actor_id

    # ── Listing ────────────────────────────────────────────────────

    def list(self) -> dict:
        """Return all items (docs + top-level trashed folders) currently in trash."""
        with self.store.transaction() as sess:
            trashed_folders = list(
                sess.execute(
                    select(Folder).where(
                        (Folder.path.like(TRASH_PATH + "/%"))
                        # Only include "top-level trashed" (direct children of __trash__)
                        & (Folder.parent_id == TRASH_FOLDER_ID)
                    )
                ).scalars()
            )
            # Documents directly under __trash__ (or any descendant — we'll show
            # only those NOT inside a trashed folder, to avoid duplicate display)
            trashed_folder_prefixes = [f.path + "/" for f in trashed_folders]
            raw_docs = list(
                sess.execute(
                    select(Document).where(Document.path.like(TRASH_PATH + "/%"))
                ).scalars()
            )
            top_level_docs = [
                d for d in raw_docs
                if not any(d.path.startswith(p) for p in trashed_folder_prefixes)
            ]

            items: list[dict] = []
            for f in trashed_folders:
                items.append(_folder_to_trash_item(f))
            for d in top_level_docs:
                items.append(_doc_to_trash_item(d))
        return {"items": items, "count": len(items)}

    # ── Restore ────────────────────────────────────────────────────

    def restore(
        self,
        *,
        doc_ids: list[str] | None = None,
        folder_paths: list[str] | None = None,
    ) -> dict:
        """
        Move items out of trash back to their original path.
        Destination conflicts resolved by appending "(restored)" or "(N)".
        """
        restored: list[dict] = []
        errors: list[dict] = []

        with self.store.transaction() as sess:
            svc = FolderService(sess, actor_id=self.actor_id)

            for doc_id in (doc_ids or []):
                doc = sess.get(Document, doc_id)
                if doc is None or not _doc_in_trash(doc):
                    errors.append({"doc_id": doc_id, "error": "not in trash"})
                    continue
                meta = doc.trashed_metadata or {}
                original_folder_id = meta.get("original_folder_id") or ROOT_FOLDER_ID
                target_folder = sess.get(Folder, original_folder_id)
                if target_folder is None or _folder_in_trash(target_folder):
                    target_folder = sess.get(Folder, ROOT_FOLDER_ID)

                filename = doc.filename or doc.path.rsplit("/", 1)[-1]
                new_path = unique_document_path(sess, target_folder, filename)
                doc.folder_id = target_folder.folder_id
                doc.path = new_path
                doc.trashed_metadata = None
                restored.append({"doc_id": doc_id, "path": new_path})
                sess.add(AuditLogRow(
                    actor_id=self.actor_id,
                    action="document.restore",
                    target_type="document",
                    target_id=doc_id,
                    details={"to_path": new_path},
                ))

            for folder_path in (folder_paths or []):
                folder = svc.get_by_path(folder_path)
                if folder is None or not _folder_in_trash(folder):
                    errors.append({"folder_path": folder_path, "error": "not in trash"})
                    continue
                meta = folder.trashed_metadata or {}
                original_parent_id = meta.get("original_folder_id") or ROOT_FOLDER_ID
                original_path = meta.get("original_path") or ""
                target_parent = sess.get(Folder, original_parent_id)
                if target_parent is None or _folder_in_trash(target_parent):
                    target_parent = sess.get(Folder, ROOT_FOLDER_ID)

                # Recover the name: original_path basename, or strip timestamp prefix
                if original_path:
                    original_name = original_path.rsplit("/", 1)[-1]
                else:
                    # Name looks like "20260418T123456_foo" — strip timestamp
                    parts = folder.name.split("_", 1)
                    original_name = parts[1] if len(parts) == 2 else folder.name

                # Handle name collision in destination
                candidate = original_name
                i = 1
                while svc.get_by_path(join_path(target_parent.path, candidate)) is not None:
                    candidate = f"{original_name} (restored {i})" if i > 1 else f"{original_name} (restored)"
                    i += 1

                new_path = join_path(target_parent.path, candidate)
                old_path = folder.path
                svc._cascade_path_rewrite(old_path, new_path)
                folder.parent_id = target_parent.folder_id
                folder.name = candidate
                folder.trashed_metadata = None
                restored.append({"folder_path": folder_path, "path": new_path})
                sess.add(AuditLogRow(
                    actor_id=self.actor_id,
                    action="folder.restore",
                    target_type="folder",
                    target_id=folder.folder_id,
                    details={"old_path": old_path, "new_path": new_path},
                ))

        return {"restored": restored, "errors": errors}

    # ── Permanent delete ───────────────────────────────────────────

    def purge(
        self,
        *,
        doc_ids: list[str] | None = None,
        folder_paths: list[str] | None = None,
    ) -> dict:
        """Permanently delete items (must already be in trash)."""
        # Collect ids to purge in a read-only pass
        with self.store.transaction() as sess:
            purge_doc_ids: list[str] = []
            purge_folder_ids: list[str] = []

            for doc_id in (doc_ids or []):
                doc = sess.get(Document, doc_id)
                if doc is not None and _doc_in_trash(doc):
                    purge_doc_ids.append(doc_id)

            for fp in (folder_paths or []):
                f = FolderService(sess).get_by_path(fp)
                if f is not None and _folder_in_trash(f):
                    # Collect folder + all descendants + their docs
                    purge_folder_ids.append(f.folder_id)
                    subtree = FolderService(sess).list_descendants(f.folder_id)
                    purge_folder_ids.extend(x.folder_id for x in subtree)
                    subtree_paths = [f.path] + [x.path for x in subtree]
                    for p in subtree_paths:
                        docs = list(sess.execute(
                            select(Document.doc_id).where(
                                (Document.path == p) | (Document.path.like(p + "/%"))
                            )
                        ).scalars())
                        purge_doc_ids.extend(docs)

        # De-dup
        purge_doc_ids = sorted(set(purge_doc_ids))
        purge_folder_ids = sorted(set(purge_folder_ids))

        # Perform per-doc cleanup (outside the relational transaction so
        # failures don't block each other). Order: vector → KG → relational.
        for did in purge_doc_ids:
            try:
                chunk_ids = [c["chunk_id"] for c in self.store.get_chunks(did, 1)]
                if chunk_ids and hasattr(self.state, "vector"):
                    self.state.vector.delete_chunks(chunk_ids)
            except Exception as e:
                log.warning("vector cleanup failed for %s: %s", did, e)
            try:
                if getattr(self.state, "graph_store", None) is not None:
                    self.state.graph_store.delete_by_doc(did)
            except Exception as e:
                log.warning("KG cleanup failed for %s: %s", did, e)
            try:
                self.store.delete_document(did)
            except Exception as e:
                log.warning("relational delete failed for %s: %s", did, e)

        # Drop trashed folders (only after their contents are gone)
        with self.store.transaction() as sess:
            for fid in purge_folder_ids:
                f = sess.get(Folder, fid)
                if f is None:
                    continue
                sess.delete(f)
                sess.add(AuditLogRow(
                    actor_id=self.actor_id,
                    action="folder.purge",
                    target_type="folder",
                    target_id=fid,
                    details={"path": f.path},
                ))
            for did in purge_doc_ids:
                sess.add(AuditLogRow(
                    actor_id=self.actor_id,
                    action="document.purge",
                    target_type="document",
                    target_id=did,
                    details={},
                ))

        # BM25 refresh — once, at the end
        try:
            if hasattr(self.state, "refresh_bm25"):
                self.state.refresh_bm25()
        except Exception:
            log.warning("post-purge bm25 refresh failed")

        return {
            "purged_documents": len(purge_doc_ids),
            "purged_folders": len(purge_folder_ids),
        }

    # ── Empty trash ────────────────────────────────────────────────

    def empty(self) -> dict:
        """Permanently delete everything in /__trash__."""
        with self.store.transaction() as sess:
            top_folders = list(
                sess.execute(
                    select(Folder.path).where(Folder.parent_id == TRASH_FOLDER_ID)
                ).scalars()
            )
            top_docs = list(
                sess.execute(
                    select(Document.doc_id).where(Document.folder_id == TRASH_FOLDER_ID)
                ).scalars()
            )
        return self.purge(doc_ids=top_docs, folder_paths=top_folders)

    # ── Auto-purge by age ──────────────────────────────────────────

    def auto_purge(self, *, retention_days: int = 30) -> dict:
        """Purge any trashed item older than `retention_days`."""
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        cutoff_iso = cutoff.isoformat()

        with self.store.transaction() as sess:
            old_folders = list(
                sess.execute(
                    select(Folder.path).where(Folder.parent_id == TRASH_FOLDER_ID)
                ).scalars()
            )
            # Filter by trashed_at < cutoff via Python (metadata is JSON)
            def _stale(f_path: str) -> bool:
                f = FolderService(sess).get_by_path(f_path)
                if f is None:
                    return False
                ts = (f.trashed_metadata or {}).get("trashed_at")
                return bool(ts and ts < cutoff_iso)

            old_folder_paths = [p for p in old_folders if _stale(p)]

            old_docs = list(
                sess.execute(
                    select(Document).where(Document.folder_id == TRASH_FOLDER_ID)
                ).scalars()
            )
            old_doc_ids = [
                d.doc_id for d in old_docs
                if (d.trashed_metadata or {}).get("trashed_at", "") < cutoff_iso
            ]

        if not old_folder_paths and not old_doc_ids:
            return {"purged_documents": 0, "purged_folders": 0, "reason": "nothing stale"}

        result = self.purge(doc_ids=old_doc_ids, folder_paths=old_folder_paths)
        result["auto"] = True
        result["retention_days"] = retention_days
        log.info(
            "trash auto-purge: removed %d docs + %d folders (older than %d days)",
            result.get("purged_documents", 0),
            result.get("purged_folders", 0),
            retention_days,
        )
        return result


# ---------------------------------------------------------------------------
# DTO helpers
# ---------------------------------------------------------------------------


def _folder_to_trash_item(f: Folder) -> dict:
    meta = f.trashed_metadata or {}
    return {
        "type": "folder",
        "folder_id": f.folder_id,
        "path": f.path,
        "original_path": meta.get("original_path", ""),
        "trashed_at": meta.get("trashed_at", ""),
        "trashed_by": meta.get("trashed_by", ""),
        "name": f.name,
    }


def _doc_to_trash_item(d: Document) -> dict:
    meta = d.trashed_metadata or {}
    return {
        "type": "document",
        "doc_id": d.doc_id,
        "path": d.path,
        "original_path": meta.get("original_path", ""),
        "trashed_at": meta.get("trashed_at", ""),
        "trashed_by": meta.get("trashed_by", ""),
        "filename": d.filename,
    }
