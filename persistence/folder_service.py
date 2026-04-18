"""
FolderService — the sole entry point for folder / path operations.

All code paths that create / rename / move / delete folders MUST go
through this service. It is the transactional boundary that keeps
the triple invariant:

    1.  folder.path is always path_of(folder.parent) + '/' + folder.name
        (or '/' for root).
    2.  document.path is always document.folder.path rstripped + '/'
        + document.filename.
    3.  folder.path_lower mirrors folder.path.lower() (for case-insensitive
        uniqueness checks — "/Legal" and "/legal" cannot both exist).

Two system folders are always present:
    __root__   (path='/', parent=None)
    __trash__  (path='/__trash__', parent=__root__)

All user content lives under __root__ directly or nested below.
Soft-deleted items live under __trash__.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from .models import AuditLogRow, Document, Folder


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROOT_FOLDER_ID = "__root__"
TRASH_FOLDER_ID = "__trash__"
ROOT_PATH = "/"
TRASH_PATH = "/__trash__"

# Forbidden characters in folder / document names. Mirrors filesystem
# limitations + keeps URLs clean.
_NAME_FORBIDDEN = re.compile(r'[\\/?*<>|":\x00-\x1f]')
_MAX_NAME_LEN = 255
_MAX_PATH_LEN = 1024
_MAX_DEPTH = 10   # path.count('/') cannot exceed this


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FolderError(RuntimeError):
    """Base class for folder-service errors."""


class FolderNotFound(FolderError):
    pass


class FolderAlreadyExists(FolderError):
    pass


class InvalidFolderName(FolderError):
    pass


class FolderIsSystemProtected(FolderError):
    """Raised when the caller tries to rename/delete __root__ or __trash__."""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class FolderInfo:
    folder_id: str
    path: str
    parent_id: str | None
    name: str
    is_system: bool
    trashed: bool
    child_folders: int
    document_count: int
    created_at: datetime | None
    updated_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def normalize_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise InvalidFolderName("folder name cannot be empty")
    if len(name) > _MAX_NAME_LEN:
        raise InvalidFolderName(f"folder name exceeds {_MAX_NAME_LEN} chars")
    if _NAME_FORBIDDEN.search(name):
        raise InvalidFolderName(
            f"folder name contains forbidden characters: {name!r}"
        )
    if name in (".", ".."):
        raise InvalidFolderName(f"reserved folder name: {name!r}")
    return name


def join_path(parent_path: str, name: str) -> str:
    """Compose a child path. parent_path is like '/' or '/a/b'."""
    if parent_path == "/":
        return "/" + name
    return parent_path.rstrip("/") + "/" + name


def parent_of(path: str) -> str:
    if path == "/" or "/" not in path[1:]:
        return "/"
    return path.rsplit("/", 1)[0] or "/"


def is_under(path: str, ancestor_path: str) -> bool:
    """Does ``path`` live inside ``ancestor_path`` (same or descendant)?"""
    if ancestor_path == "/":
        return True
    if path == ancestor_path:
        return True
    return path.startswith(ancestor_path + "/")


def depth_of(path: str) -> int:
    if path == "/":
        return 0
    return path.count("/")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FolderService:
    """
    Thin wrapper over the relational Store's session. All mutations
    run inside a single transaction supplied by the caller.

    Usage:
        with store.session() as sess:
            svc = FolderService(sess)
            folder = svc.create('/legal', '2024')
            sess.commit()
    """

    def __init__(self, sess: Session, *, actor_id: str = "local"):
        self.sess = sess
        self.actor_id = actor_id

    # ── Read ───────────────────────────────────────────────────────

    def get_by_id(self, folder_id: str) -> Folder | None:
        return self.sess.get(Folder, folder_id)

    def get_by_path(self, path: str) -> Folder | None:
        path_l = path.lower()
        return self.sess.execute(
            select(Folder).where(Folder.path_lower == path_l)
        ).scalar_one_or_none()

    def require_by_path(self, path: str) -> Folder:
        f = self.get_by_path(path)
        if f is None:
            raise FolderNotFound(f"no folder at path {path!r}")
        return f

    def list_children(self, parent_id: str) -> list[Folder]:
        return list(
            self.sess.execute(
                select(Folder)
                .where(Folder.parent_id == parent_id)
                .order_by(Folder.name)
            ).scalars()
        )

    def list_ancestors(self, folder_id: str) -> list[Folder]:
        """Return ancestors bottom-up (immediate parent first, root last)."""
        out: list[Folder] = []
        current = self.get_by_id(folder_id)
        while current is not None and current.parent_id is not None:
            parent = self.get_by_id(current.parent_id)
            if parent is None:
                break
            out.append(parent)
            current = parent
        return out

    def list_descendants(self, folder_id: str) -> list[Folder]:
        """All descendants of a folder, excluding itself. Uses path prefix."""
        f = self.get_by_id(folder_id)
        if f is None:
            return []
        prefix = f.path if f.path == "/" else f.path + "/"
        if f.path == "/":
            return list(
                self.sess.execute(
                    select(Folder).where(Folder.folder_id != ROOT_FOLDER_ID)
                ).scalars()
            )
        return list(
            self.sess.execute(
                select(Folder).where(Folder.path.like(prefix + "%"))
            ).scalars()
        )

    def count_documents(self, folder_id: str, *, recursive: bool = False) -> int:
        from sqlalchemy import func
        if not recursive:
            n = self.sess.execute(
                select(func.count()).select_from(Document).where(
                    Document.folder_id == folder_id
                )
            ).scalar_one()
            return int(n or 0)
        f = self.get_by_id(folder_id)
        if f is None:
            return 0
        prefix = f.path if f.path == "/" else f.path + "/"
        if f.path == "/":
            n = self.sess.execute(
                select(func.count()).select_from(Document)
            ).scalar_one()
        else:
            n = self.sess.execute(
                select(func.count()).select_from(Document)
                .where(Document.path.like(prefix + "%"))
            ).scalar_one()
        return int(n or 0)

    # ── Create ─────────────────────────────────────────────────────

    def create(
        self,
        parent_path: str,
        name: str,
        *,
        metadata: dict | None = None,
    ) -> Folder:
        name = normalize_name(name)
        parent = self.require_by_path(parent_path)

        new_path = join_path(parent.path, name)
        if len(new_path) > _MAX_PATH_LEN:
            raise InvalidFolderName(
                f"resulting path exceeds {_MAX_PATH_LEN} chars"
            )
        if depth_of(new_path) > _MAX_DEPTH:
            raise InvalidFolderName(f"folder depth exceeds {_MAX_DEPTH}")

        if self.get_by_path(new_path) is not None:
            raise FolderAlreadyExists(f"folder already exists: {new_path!r}")

        folder = Folder(
            folder_id=_new_id(),
            path=new_path,
            path_lower=new_path.lower(),
            parent_id=parent.folder_id,
            name=name,
            is_system=False,
            metadata_json=metadata or {},
        )
        self.sess.add(folder)
        self._audit("folder.create", "folder", folder.folder_id, {
            "path": new_path,
            "parent_id": parent.folder_id,
        })
        self.sess.flush()
        return folder

    def ensure_path(self, path: str) -> Folder:
        """Idempotently create all folders up to ``path``. Returns the leaf."""
        if path in ("", "/"):
            return self.require_by_path(ROOT_PATH)
        parts = [p for p in path.split("/") if p]
        cur_path = ROOT_PATH
        for seg in parts:
            child_path = join_path(cur_path, seg)
            existing = self.get_by_path(child_path)
            if existing is None:
                self.create(cur_path, seg)
            cur_path = child_path
        return self.require_by_path(cur_path)

    # ── Rename / Move ──────────────────────────────────────────────

    def rename(self, folder_id: str, new_name: str) -> Folder:
        folder = self.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound(folder_id)
        if folder.is_system:
            raise FolderIsSystemProtected(
                f"cannot rename system folder {folder.folder_id!r}"
            )
        new_name = normalize_name(new_name)
        old_path = folder.path
        new_path = join_path(parent_of(old_path), new_name)
        if new_path == old_path:
            return folder
        if self.get_by_path(new_path) is not None:
            raise FolderAlreadyExists(f"folder already exists: {new_path!r}")

        self._cascade_path_rewrite(old_path, new_path)
        # Sync ORM state after bulk UPDATE rewrote paths in DB
        folder.path = new_path
        folder.path_lower = new_path.lower()
        folder.name = new_name
        self._audit(
            "folder.rename",
            "folder",
            folder.folder_id,
            {"old_path": old_path, "new_path": new_path},
        )
        self.sess.flush()
        return folder

    def move(self, folder_id: str, new_parent_path: str) -> Folder:
        folder = self.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound(folder_id)
        if folder.is_system:
            raise FolderIsSystemProtected(
                f"cannot move system folder {folder.folder_id!r}"
            )
        new_parent = self.require_by_path(new_parent_path)
        # Cannot move a folder into itself / its own subtree
        if is_under(new_parent.path, folder.path):
            raise FolderError(
                f"cannot move folder {folder.path!r} into its own subtree "
                f"{new_parent.path!r}"
            )
        old_path = folder.path
        new_path = join_path(new_parent.path, folder.name)
        if new_path == old_path:
            return folder
        if self.get_by_path(new_path) is not None:
            raise FolderAlreadyExists(
                f"destination already exists: {new_path!r}"
            )
        if len(new_path) > _MAX_PATH_LEN:
            raise InvalidFolderName(f"resulting path exceeds {_MAX_PATH_LEN}")

        self._cascade_path_rewrite(old_path, new_path)
        # Sync ORM state after bulk UPDATE rewrote paths in DB
        folder.path = new_path
        folder.path_lower = new_path.lower()
        folder.parent_id = new_parent.folder_id
        self._audit(
            "folder.move",
            "folder",
            folder.folder_id,
            {"old_path": old_path, "new_path": new_path},
        )
        self.sess.flush()
        return folder

    # ── Delete / restore ───────────────────────────────────────────

    def move_to_trash(self, folder_id: str) -> Folder:
        """Send a folder (and its whole subtree) into /__trash__."""
        folder = self.get_by_id(folder_id)
        if folder is None:
            raise FolderNotFound(folder_id)
        if folder.is_system:
            raise FolderIsSystemProtected(
                f"cannot trash system folder {folder.folder_id!r}"
            )
        trash = self.require_by_path(TRASH_PATH)
        original_parent_id = folder.parent_id
        original_path = folder.path
        # Name inside trash: <timestamp>_<name> to avoid collisions
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        trash_name = f"{ts}_{folder.name}"
        new_path = join_path(trash.path, trash_name)
        self._cascade_path_rewrite(original_path, new_path)
        # Sync ORM state after the bulk UPDATE (the cascade rewrote path
        # in the DB directly, but this managed instance hasn't refreshed).
        folder.path = new_path
        folder.path_lower = new_path.lower()
        folder.name = trash_name
        folder.parent_id = trash.folder_id
        folder.trashed_metadata = {
            "original_folder_id": original_parent_id,
            "original_path": original_path,
            "trashed_at": datetime.utcnow().isoformat(),
            "trashed_by": self.actor_id,
        }
        self._audit(
            "folder.trash",
            "folder",
            folder.folder_id,
            {"original_path": original_path, "new_path": new_path},
        )
        self.sess.flush()
        return folder

    # ── Internals ──────────────────────────────────────────────────

    def _cascade_path_rewrite(self, old_prefix: str, new_prefix: str) -> None:
        """
        Update `folders.path`, `documents.path`, and `chunks.path` for
        every descendant whose path starts with ``old_prefix``. Runs as
        a single transaction-scoped bulk UPDATE — for 100k chunks this
        completes in ~5 seconds on Postgres.

        Chroma and Neo4j denormalized metadata is NOT touched here.
        Callers that cross the async threshold enqueue a
        ``pending_folder_ops`` row so the nightly maintenance script
        catches those stores up.
        """
        from sqlalchemy import func

        from .models import ChunkRow

        # folders.path + path_lower
        folders_stmt = (
            update(Folder)
            .where(
                (Folder.path == old_prefix)
                | (Folder.path.like(old_prefix.rstrip("/") + "/%"))
            )
            .values(
                path=func.concat(
                    new_prefix,
                    func.substr(Folder.path, len(old_prefix) + 1),
                ),
                path_lower=func.concat(
                    new_prefix.lower(),
                    func.substr(Folder.path_lower, len(old_prefix) + 1),
                ),
            )
            .execution_options(synchronize_session=False)
        )
        self.sess.execute(folders_stmt)

        # documents.path
        docs_stmt = (
            update(Document)
            .where(
                (Document.path == old_prefix)
                | (Document.path.like(old_prefix.rstrip("/") + "/%"))
            )
            .values(
                path=func.concat(
                    new_prefix,
                    func.substr(Document.path, len(old_prefix) + 1),
                )
            )
            .execution_options(synchronize_session=False)
        )
        self.sess.execute(docs_stmt)

        # chunks.path — new in D1: denormalized mirror for fast retrieval.
        chunks_stmt = (
            update(ChunkRow)
            .where(
                (ChunkRow.path == old_prefix)
                | (ChunkRow.path.like(old_prefix.rstrip("/") + "/%"))
            )
            .values(
                path=func.concat(
                    new_prefix,
                    func.substr(ChunkRow.path, len(old_prefix) + 1),
                )
            )
            .execution_options(synchronize_session=False)
        )
        self.sess.execute(chunks_stmt)

    def _audit(
        self,
        action: str,
        target_type: str,
        target_id: str,
        details: dict | None = None,
    ) -> None:
        self.sess.add(
            AuditLogRow(
                actor_id=self.actor_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        )


# ---------------------------------------------------------------------------
# Collision-safe name auto-rename for documents
# ---------------------------------------------------------------------------


def unique_document_path(
    sess: Session, folder: Folder, filename: str
) -> str:
    """
    Compose `<folder.path>/<filename>` — if the path already exists in
    `documents` under the same folder, append `(1)`, `(2)`, ... before
    the extension until unique. Case-insensitive comparison using a
    single-folder scan (cheap — a folder has at most a few hundred docs).
    """
    # Only docs in the same folder can collide on filename
    existing_paths = set(
        (p or "").lower()
        for p in sess.execute(
            select(Document.path).where(Document.folder_id == folder.folder_id)
        ).scalars()
    )
    base_path = join_path(folder.path, filename)
    if base_path.lower() not in existing_paths:
        return base_path
    stem, dot, ext = filename.rpartition(".")
    i = 1
    while True:
        if dot:
            candidate_name = f"{stem} ({i}).{ext}"
        else:
            candidate_name = f"{filename} ({i})"
        candidate = join_path(folder.path, candidate_name)
        if candidate.lower() not in existing_paths:
            return candidate
        i += 1
