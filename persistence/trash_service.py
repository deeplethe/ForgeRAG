"""
TrashService — restore / permanent delete / auto-purge.

Works on the /__trash__ folder subtree populated by FolderService.move_to_trash
and the soft-delete path for documents. Permanent deletion cascades through
the relational DB, vector store, BM25 index, and KG graph store.

Multi-user authz: every trashed item carries
``trashed_metadata.original_folder_id`` pointing at the folder the
item lived in (for documents) or its parent (for folders) BEFORE
the move-to-trash. ``list`` / ``restore`` / ``purge`` accept
``user_id`` + ``is_admin`` and filter / gate per-item using
``state.authz.can(original_folder_id, action)``. When ``user_id``
is None (single-user dev) or ``is_admin`` is True the filter is a
passthrough and the call behaves as before.

Items whose ``original_folder_id`` is no longer resolvable
(orphans — the source folder was hard-deleted while the item sat in
trash) are visible / actionable to admins only; non-admins simply
don't see them in ``list`` and get a deny in ``restore`` / ``purge``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from .folder_service import (
    ROOT_FOLDER_ID,
    TRASH_FOLDER_ID,
    TRASH_PATH,
    FolderService,
    join_path,
    parent_of,
    unique_document_path,
)
from .models import AuditLogRow, ChunkRow, Document, Folder

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

    def list(
        self,
        *,
        user_id: str | None = None,
        is_admin: bool = False,
    ) -> dict:
        """Return all items (docs + top-level trashed folders) currently in trash.

        When ``user_id`` is provided and ``is_admin`` is False, items
        are filtered by ``authz.can(original_folder_id, "read")`` —
        the user only sees trash from folders they currently have at
        least read access to. Orphans (original folder hard-deleted)
        are admin-only.
        """
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
            raw_docs = list(sess.execute(select(Document).where(Document.path.like(TRASH_PATH + "/%"))).scalars())
            top_level_docs = [d for d in raw_docs if not any(d.path.startswith(p) for p in trashed_folder_prefixes)]

            if not is_admin and user_id is not None:
                trashed_folders = [
                    f for f in trashed_folders
                    if self._user_can(user_id, _orig_folder_id(f), "read")
                ]
                top_level_docs = [
                    d for d in top_level_docs
                    if self._user_can(user_id, _orig_folder_id(d), "read")
                ]

            items: list[dict] = []
            for f in trashed_folders:
                items.append(_folder_to_trash_item(f))
            for d in top_level_docs:
                items.append(_doc_to_trash_item(d))
        return {"items": items, "count": len(items)}

    # ── authz helper ───────────────────────────────────────────────

    def _user_can(
        self, user_id: str, original_folder_id: str | None, action: str
    ) -> bool:
        """Per-trash-item access check.

        ``original_folder_id`` comes from the item's
        ``trashed_metadata`` and points at the source folder. ``None``
        means orphan (legacy row missing the field, or original
        folder was hard-deleted) — treat as "no access" so non-admins
        don't see them.
        """
        if not original_folder_id:
            return False
        authz = getattr(self.state, "authz", None)
        if authz is None:
            return True  # single-user dev: no authz layer
        return authz.can(user_id, original_folder_id, action)

    # ── Soft-delete (single document) ──────────────────────────────

    def move_document_to_trash(self, doc_id: str) -> dict:
        """Send a single document into /__trash__.

        Path-based, Windows-style: stash ``original_path`` in metadata so
        ``restore`` can rebuild the original parent chain even if the
        folder was permanently deleted in the meantime. Vector / KG /
        chunk rows stay in place — retrieval already filters trash by
        ``Document.path LIKE '/__trash__/%'`` (see retrieval.pipeline),
        and the nightly auto-purge handles the eventual cleanup.

        Idempotent: a doc already in trash is returned unchanged.
        """
        from datetime import datetime as _dt

        from sqlalchemy import update

        with self.store.transaction() as sess:
            doc = sess.get(Document, doc_id)
            if doc is None:
                return {"doc_id": doc_id, "error": "not found"}
            if _doc_in_trash(doc):
                return {"doc_id": doc_id, "path": doc.path, "already_trashed": True}

            original_path = doc.path
            original_filename = doc.filename or original_path.rsplit("/", 1)[-1]

            trash_folder = sess.get(Folder, TRASH_FOLDER_ID)
            ts = _dt.utcnow().strftime("%Y%m%dT%H%M%S")
            trash_filename = f"{ts}_{original_filename}"
            new_path = unique_document_path(sess, trash_folder, trash_filename)

            original_folder_id = doc.folder_id
            doc.folder_id = TRASH_FOLDER_ID
            doc.path = new_path
            doc.trashed_metadata = {
                "original_folder_id": original_folder_id,
                "original_path": original_path,
                "trashed_at": _dt.utcnow().isoformat(),
                "trashed_by": self.actor_id,
            }

            # Mirror path into chunks so PG-side scope queries that filter
            # by ChunkRow.path stay in sync. Retrieval's trashed-doc filter
            # uses Document.path so this is belt-and-suspenders, but the
            # path mirror is a documented invariant elsewhere.
            sess.execute(update(ChunkRow).where(ChunkRow.doc_id == doc_id).values(path=new_path))

            sess.add(
                AuditLogRow(
                    actor_id=self.actor_id,
                    action="document.trash",
                    target_type="document",
                    target_id=doc_id,
                    details={"original_path": original_path, "new_path": new_path},
                )
            )

        # BM25 rebuild so the trashed doc disappears from keyword hits
        # without waiting for the next periodic refresh.
        try:
            if hasattr(self.state, "refresh_bm25"):
                self.state.refresh_bm25()
        except Exception:
            log.warning("post-trash bm25 refresh failed for %s", doc_id)

        return {"doc_id": doc_id, "path": new_path}

    # ── Restore ────────────────────────────────────────────────────

    def restore(
        self,
        *,
        doc_ids: list[str] | None = None,
        folder_paths: list[str] | None = None,
        user_id: str | None = None,
        is_admin: bool = False,
    ) -> dict:
        """
        Move items out of trash back to their original path.
        Destination conflicts resolved by appending "(restored)" or "(N)".

        When ``user_id`` is provided and ``is_admin`` is False, each
        item is gated by ``authz.can(original_folder_id, "soft_delete")``.
        Items the caller can't act on land in ``denied`` rather than
        ``errors`` — partial-success semantics so a batch with one
        unauthorized item doesn't fail the whole call.
        """
        restored: list[dict] = []
        errors: list[dict] = []
        denied: list[dict] = []

        with self.store.transaction() as sess:
            svc = FolderService(sess, actor_id=self.actor_id)

            for doc_id in doc_ids or []:
                doc = sess.get(Document, doc_id)
                if doc is None or not _doc_in_trash(doc):
                    errors.append({"doc_id": doc_id, "error": "not in trash"})
                    continue
                meta = doc.trashed_metadata or {}
                if not is_admin and user_id is not None:
                    if not self._user_can(
                        user_id, meta.get("original_folder_id"), "soft_delete"
                    ):
                        denied.append({"doc_id": doc_id, "error": "forbidden"})
                        continue
                original_path = meta.get("original_path") or ""

                # Windows Recycle Bin semantics: rebuild the original
                # parent chain if it's missing. Folder rename / move
                # produces a "two-folder" outcome — same as Windows.
                # Falls back to root only when ``original_path`` is
                # missing (legacy rows trashed before path was stored).
                if original_path:
                    parent_path = parent_of(original_path)
                    target_folder = svc.ensure_path(parent_path)
                    original_filename = original_path.rsplit("/", 1)[-1] or doc.filename
                else:
                    target_folder = sess.get(Folder, ROOT_FOLDER_ID)
                    original_filename = doc.filename or doc.path.rsplit("/", 1)[-1]

                new_path = unique_document_path(sess, target_folder, original_filename)
                doc.folder_id = target_folder.folder_id
                doc.path = new_path
                doc.filename = original_filename
                doc.trashed_metadata = None

                # Mirror path back into chunks to keep ChunkRow.path
                # consistent with Document.path for scope queries.
                from sqlalchemy import update as _update

                sess.execute(_update(ChunkRow).where(ChunkRow.doc_id == doc_id).values(path=new_path))

                restored.append({"doc_id": doc_id, "path": new_path})
                sess.add(
                    AuditLogRow(
                        actor_id=self.actor_id,
                        action="document.restore",
                        target_type="document",
                        target_id=doc_id,
                        details={"to_path": new_path},
                    )
                )

            for folder_path in folder_paths or []:
                folder = svc.get_by_path(folder_path)
                if folder is None or not _folder_in_trash(folder):
                    errors.append({"folder_path": folder_path, "error": "not in trash"})
                    continue
                meta = folder.trashed_metadata or {}
                if not is_admin and user_id is not None:
                    if not self._user_can(
                        user_id, meta.get("original_folder_id"), "soft_delete"
                    ):
                        denied.append(
                            {"folder_path": folder_path, "error": "forbidden"}
                        )
                        continue
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
                sess.add(
                    AuditLogRow(
                        actor_id=self.actor_id,
                        action="folder.restore",
                        target_type="folder",
                        target_id=folder.folder_id,
                        details={"old_path": old_path, "new_path": new_path},
                    )
                )

        return {"restored": restored, "errors": errors, "denied": denied}

    # ── Permanent delete ───────────────────────────────────────────

    def purge(
        self,
        *,
        doc_ids: list[str] | None = None,
        folder_paths: list[str] | None = None,
        user_id: str | None = None,
        is_admin: bool = False,
    ) -> dict:
        """Permanently delete items (must already be in trash).

        When ``user_id`` is provided and ``is_admin`` is False, each
        item is gated by ``authz.can(original_folder_id, "purge")``.
        Items the caller can't act on are reported in ``denied`` and
        are NOT included in the cascade — partial-success semantics
        (one unauthorized item shouldn't block the rest of the
        batch).
        """
        denied: list[dict] = []
        # Collect ids to purge in a read-only pass
        with self.store.transaction() as sess:
            purge_doc_ids: list[str] = []
            purge_folder_ids: list[str] = []

            for doc_id in doc_ids or []:
                doc = sess.get(Document, doc_id)
                if doc is None or not _doc_in_trash(doc):
                    continue
                if not is_admin and user_id is not None:
                    meta = doc.trashed_metadata or {}
                    if not self._user_can(
                        user_id, meta.get("original_folder_id"), "purge"
                    ):
                        denied.append({"doc_id": doc_id, "error": "forbidden"})
                        continue
                purge_doc_ids.append(doc_id)

            for fp in folder_paths or []:
                f = FolderService(sess).get_by_path(fp)
                if f is None or not _folder_in_trash(f):
                    continue
                if not is_admin and user_id is not None:
                    meta = f.trashed_metadata or {}
                    if not self._user_can(
                        user_id, meta.get("original_folder_id"), "purge"
                    ):
                        denied.append({"folder_path": fp, "error": "forbidden"})
                        continue
                # Collect folder + all descendants + their docs
                purge_folder_ids.append(f.folder_id)
                subtree = FolderService(sess).list_descendants(f.folder_id)
                purge_folder_ids.extend(x.folder_id for x in subtree)
                subtree_paths = [f.path] + [x.path for x in subtree]
                for p in subtree_paths:
                    docs = list(
                        sess.execute(
                            select(Document.doc_id).where((Document.path == p) | (Document.path.like(p + "/%")))
                        ).scalars()
                    )
                    purge_doc_ids.extend(docs)

        # De-dup
        purge_doc_ids = sorted(set(purge_doc_ids))
        purge_folder_ids = sorted(set(purge_folder_ids))

        # Snapshot per-doc metadata BEFORE deletion: we need
        # ``active_parse_version`` to find chunk_ids for vector cleanup
        # (previous code hardcoded ``1`` which silently missed any doc
        # that had been reparsed) and ``file_id`` for orphan-blob cleanup.
        doc_meta: dict[str, dict] = {}
        for did in purge_doc_ids:
            row = self.store.get_document(did) or {}
            doc_meta[did] = {
                "active_parse_version": row.get("active_parse_version") or 1,
                "file_id": row.get("file_id"),
            }

        # Perform per-doc cleanup (outside the relational transaction so
        # failures don't block each other). Order: vector → KG → relational.
        for did in purge_doc_ids:
            pv = doc_meta[did]["active_parse_version"]
            try:
                chunk_ids = [c["chunk_id"] for c in self.store.get_chunks(did, pv)]
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

        # File-blob cleanup: drop the underlying file row only when no
        # other document still references it (multiple parse_versions
        # of the same upload share a file_id).
        purged_file_ids = {m["file_id"] for m in doc_meta.values() if m.get("file_id")}
        if purged_file_ids:
            try:
                surviving = self.store.list_documents(limit=10000)
                still_referenced = {d.get("file_id") for d in surviving if d.get("file_id")}
                for fid in purged_file_ids - still_referenced:
                    try:
                        self.store.delete_file(fid)
                    except Exception as e:
                        log.warning("file cleanup failed for %s: %s", fid, e)
            except Exception as e:
                log.warning("file orphan-scan failed: %s", e)

        # Drop trashed folders (only after their contents are gone)
        with self.store.transaction() as sess:
            for fid in purge_folder_ids:
                f = sess.get(Folder, fid)
                if f is None:
                    continue
                sess.delete(f)
                sess.add(
                    AuditLogRow(
                        actor_id=self.actor_id,
                        action="folder.purge",
                        target_type="folder",
                        target_id=fid,
                        details={"path": f.path},
                    )
                )
            for did in purge_doc_ids:
                sess.add(
                    AuditLogRow(
                        actor_id=self.actor_id,
                        action="document.purge",
                        target_type="document",
                        target_id=did,
                        details={},
                    )
                )

        # BM25 refresh — once, at the end
        try:
            if hasattr(self.state, "refresh_bm25"):
                self.state.refresh_bm25()
        except Exception:
            log.warning("post-purge bm25 refresh failed")

        return {
            "purged_documents": len(purge_doc_ids),
            "purged_folders": len(purge_folder_ids),
            "denied": denied,
        }

    # ── Empty trash ────────────────────────────────────────────────

    def empty(self) -> dict:
        """Permanently delete everything in /__trash__."""
        with self.store.transaction() as sess:
            top_folders = list(sess.execute(select(Folder.path).where(Folder.parent_id == TRASH_FOLDER_ID)).scalars())
            top_docs = list(
                sess.execute(select(Document.doc_id).where(Document.folder_id == TRASH_FOLDER_ID)).scalars()
            )
        return self.purge(doc_ids=top_docs, folder_paths=top_folders)

    # ── Auto-purge by age ──────────────────────────────────────────

    def auto_purge(self, *, retention_days: int = 30) -> dict:
        """Purge any trashed item older than `retention_days`."""
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        cutoff_iso = cutoff.isoformat()

        with self.store.transaction() as sess:
            old_folders = list(sess.execute(select(Folder.path).where(Folder.parent_id == TRASH_FOLDER_ID)).scalars())

            # Filter by trashed_at < cutoff via Python (metadata is JSON)
            def _stale(f_path: str) -> bool:
                f = FolderService(sess).get_by_path(f_path)
                if f is None:
                    return False
                ts = (f.trashed_metadata or {}).get("trashed_at")
                return bool(ts and ts < cutoff_iso)

            old_folder_paths = [p for p in old_folders if _stale(p)]

            old_docs = list(sess.execute(select(Document).where(Document.folder_id == TRASH_FOLDER_ID)).scalars())
            old_doc_ids = [d.doc_id for d in old_docs if (d.trashed_metadata or {}).get("trashed_at", "") < cutoff_iso]

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


def _orig_folder_id(item) -> str | None:
    """Pull the source folder id off a trashed Folder or Document.

    For trashed folders the metadata's ``original_folder_id`` is the
    parent the folder lived under; for documents it's the folder
    directly containing the doc. Either way, that's the folder whose
    grants gate access in the new authz model.
    """
    meta = getattr(item, "trashed_metadata", None) or {}
    return meta.get("original_folder_id")


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
