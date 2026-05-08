"""
ProjectFileService — file operations on a project's workdir.

Sole entry point for routes (and the Phase-2 agent's `read_file` /
`write_file` / `list_files` tools) that touch the on-disk layout
under ``<projects_root>/<project_id>/``.

Responsibilities:

* **Path safety.** All callers hand in a relative path string
  (``inputs/sales.csv``); we resolve against the workdir, refuse
  ``..`` traversal, refuse absolute paths, refuse symlink escape,
  and refuse direct writes into the system-managed ``.trash`` /
  ``.agent-state`` subdirs.
* **Quota.** Every write checks the project's total workdir size
  against ``cfg.agent.max_project_workdir_bytes`` and refuses with
  ``ProjectQuotaExceeded`` once the cap is hit. Trash counts toward
  the quota — purging actually frees space.
* **Soft delete.** ``soft_delete`` moves the target into
  ``.trash/<trash_id>`` and appends to ``.agent-state/trash.json``.
  Restore + purge + empty round-trip the same index. The trash
  index is rewritten atomically (write-temp-then-rename).
* **Mime sniffing.** ``read`` returns ``(content, mime)`` using
  ``mimetypes.guess_type`` — good enough for the Phase-1 download
  endpoint; richer detection (libmagic) lands later if needed.

Stateless (per-request); the caller scopes a service instance to
one ``Project`` row + the actor identity for audit.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLogRow, Project
from .project_service import TRASH_INDEX_REL_PATH, _NAME_FORBIDDEN

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reserved subdir guards
# ---------------------------------------------------------------------------

# Top-level subdirs the file API refuses to read/write/list directly.
# ``.trash`` is reachable through the trash routes; ``.agent-state``
# is system-only.
_RESERVED_TOP_DIRS = (".trash", ".agent-state")

# Subdirs that DO exist by default but stay editable (the agent's
# soft conventions). Listed here only for documentation — there's no
# write check against them.
_CONVENTIONAL_SUBDIRS = ("inputs", "outputs", "scratch")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProjectFileError(RuntimeError):
    """Base class for file-service errors."""


class InvalidProjectPath(ProjectFileError):
    """Path is malformed, traverses outside the workdir, or hits
    a reserved system subdir."""


class ProjectFileNotFound(ProjectFileError):
    pass


class ProjectFileExists(ProjectFileError):
    """Target path already occupied (rename / move collision)."""


class ProjectQuotaExceeded(ProjectFileError):
    pass


class ProjectUploadTooLarge(ProjectFileError):
    pass


class TrashEntryNotFound(ProjectFileError):
    pass


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class FileEntry:
    """One row in a directory listing or the result of a write."""

    path: str            # relative to workdir, posix-style
    name: str            # last segment
    is_dir: bool
    size_bytes: int      # 0 for directories
    modified_at: str     # iso8601


@dataclass
class TrashEntry:
    trash_id: str
    original_path: str
    trashed_at: str
    trashed_by: str
    size_bytes: int
    is_dir: bool
    trash_path: str       # always under ``.trash/<trash_id>``


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_trash_id() -> str:
    return "tr_" + secrets.token_hex(8)


def _validate_segment(seg: str) -> str:
    """A single path segment (folder name or filename) must clear
    the same charset rules as a project name. Empty / dot / dot-dot
    are explicitly rejected by the resolver."""
    if seg in (".", ".."):
        raise InvalidProjectPath(f"reserved path segment: {seg!r}")
    if not seg:
        raise InvalidProjectPath("empty path segment")
    if _NAME_FORBIDDEN.search(seg):
        raise InvalidProjectPath(
            f"path segment contains forbidden characters: {seg!r}"
        )
    return seg


def _posix(rel: Path) -> str:
    """Render a relative Path in posix form for cross-platform
    consistency on the wire (Windows dev box ↔ Linux container)."""
    return rel.as_posix()


def _size_of(p: Path) -> int:
    """File size for a file, recursive sum for a directory."""
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in p.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def _atomic_write_text(path: Path, content: str) -> None:
    """Write-temp-then-rename so a crashed write never corrupts
    the trash index. Same dir keeps the rename atomic on the same
    filesystem."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProjectFileService:
    """Owns one (project, actor) interaction with the workdir.

    Usage:
        with store.transaction() as sess:
            svc = ProjectFileService(
                sess,
                project=proj,
                projects_root=Path("storage/projects"),
                max_workdir_bytes=10 * 1024**3,
                max_upload_bytes=500 * 1024**2,
                actor_id="u_alice",
            )
            svc.write("inputs/data.csv", b"col1,col2\\n1,2\\n")

    All read operations bypass DB altogether (Workspace UI reads
    the host filesystem directly per the architecture decision);
    writes / deletes touch the audit log via the supplied session.
    """

    def __init__(
        self,
        sess: Session,
        *,
        project: Project,
        projects_root: Path | str,
        max_workdir_bytes: int = 10 * 1024 * 1024 * 1024,
        max_upload_bytes: int = 500 * 1024 * 1024,
        actor_id: str = "local",
    ):
        self.sess = sess
        self.project = project
        self.projects_root = Path(projects_root)
        self.max_workdir_bytes = max_workdir_bytes
        self.max_upload_bytes = max_upload_bytes
        self.actor_id = actor_id

    # ── Path helpers ──────────────────────────────────────────────

    @property
    def workdir(self) -> Path:
        return self.projects_root / self.project.project_id

    @property
    def trash_dir(self) -> Path:
        return self.workdir / ".trash"

    @property
    def trash_index_path(self) -> Path:
        return self.workdir / TRASH_INDEX_REL_PATH

    def _resolve(
        self,
        rel: str,
        *,
        allow_root: bool = False,
        allow_reserved: bool = False,
    ) -> Path:
        """Safely resolve ``rel`` against the workdir.

        Raises ``InvalidProjectPath`` for malformed input, traversal,
        or reserved-subdir hits (unless ``allow_reserved=True`` for
        internal trash ops).

        With ``allow_root=True`` an empty / "/" / "." input resolves
        to the workdir root — used by the list endpoint. Other ops
        require a non-root path.
        """
        if rel is None:
            raise InvalidProjectPath("path is required")
        # Normalize whitespace + windows separators; trailing
        # slashes are fine but leading slashes mean "absolute"
        # which we explicitly refuse. Detect BEFORE we strip them.
        rel = rel.strip().replace("\\", "/")
        # ./xxx is fine (just collapse it)
        while rel.startswith("./"):
            rel = rel[2:]
        # Anything that LOOKS absolute on either OS or is a Windows
        # drive letter is rejected up front.
        if rel.startswith("/") or (len(rel) >= 2 and rel[1] == ":"):
            raise InvalidProjectPath("path must be relative to project workdir")
        rel = rel.rstrip("/")

        if not rel or rel == ".":
            if allow_root:
                return self.workdir
            raise InvalidProjectPath("path cannot reference the workdir root")

        if Path(rel).is_absolute():
            raise InvalidProjectPath("path must be relative to project workdir")

        # Validate every segment
        parts = Path(rel).parts
        for seg in parts:
            _validate_segment(seg)

        # Reserved-subdir gate (unless the caller is the internal
        # trash plumbing).
        if not allow_reserved and parts and parts[0] in _RESERVED_TOP_DIRS:
            raise InvalidProjectPath(
                f"{parts[0]} is a system-managed directory; use the "
                f"trash routes instead of touching it directly"
            )

        target = (self.workdir / rel).resolve()
        # Final escape check — covers symlinks pointing out
        try:
            target.relative_to(self.workdir.resolve())
        except ValueError:
            raise InvalidProjectPath("path escapes project workdir")
        return target

    def _rel(self, abs_path: Path) -> str:
        """Render a workdir-absolute path back as the relative posix
        form callers see on the wire."""
        return _posix(abs_path.relative_to(self.workdir.resolve()))

    # ── Quota ────────────────────────────────────────────────────

    def current_workdir_size(self) -> int:
        """Total bytes used by the workdir (including trash). Cheap
        for typical project sizes (~MB-scale walk); cache on
        ``Project.metadata_json["workdir_bytes"]`` if it ever
        becomes the hot path."""
        return _size_of(self.workdir)

    def _check_quota(self, additional: int) -> None:
        if self.max_workdir_bytes <= 0:
            return  # disabled
        if additional > self.max_upload_bytes > 0:
            raise ProjectUploadTooLarge(
                f"upload {additional} bytes exceeds per-file cap "
                f"{self.max_upload_bytes}"
            )
        used = self.current_workdir_size()
        if used + additional > self.max_workdir_bytes:
            raise ProjectQuotaExceeded(
                f"project workdir would reach {used + additional} bytes "
                f"(cap {self.max_workdir_bytes})"
            )

    # ── List ─────────────────────────────────────────────────────

    def list(self, rel: str = "") -> list[FileEntry]:
        """List files + subdirs under ``rel``. Excludes the system
        top-level ``.trash`` and ``.agent-state`` subdirs from the
        root listing — those have dedicated trash routes."""
        target = self._resolve(rel or "", allow_root=True)
        if not target.exists():
            raise ProjectFileNotFound(rel)
        if not target.is_dir():
            raise InvalidProjectPath(f"not a directory: {rel!r}")

        out: list[FileEntry] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            # Hide system dirs at the root level only — sub-dirs
            # named the same are unlikely but harmless.
            if target == self.workdir and child.name in _RESERVED_TOP_DIRS:
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            is_dir = child.is_dir()
            out.append(
                FileEntry(
                    path=self._rel(child),
                    name=child.name,
                    is_dir=is_dir,
                    size_bytes=0 if is_dir else stat.st_size,
                    modified_at=datetime.utcfromtimestamp(
                        stat.st_mtime
                    ).isoformat(),
                )
            )
        return out

    # ── Read ─────────────────────────────────────────────────────

    def read(self, rel: str) -> tuple[bytes, str, str]:
        """Return ``(content_bytes, mime, filename)`` for download
        endpoints. Refuses directories — the UI lists those, not
        downloads them. (Future zip-on-the-fly is a polish item.)
        """
        target = self._resolve(rel)
        if not target.exists():
            raise ProjectFileNotFound(rel)
        if target.is_dir():
            raise InvalidProjectPath(
                f"{rel!r} is a directory; cannot read as a file"
            )
        mime, _ = mimetypes.guess_type(target.name)
        return (target.read_bytes(), mime or "application/octet-stream", target.name)

    # ── Write ────────────────────────────────────────────────────

    def write(
        self,
        rel: str,
        content: bytes,
        *,
        overwrite: bool = False,
    ) -> FileEntry:
        """Write a file. Parent directories created on the fly.
        Refuses to overwrite an existing path unless explicitly
        opted-in; the route layer maps this to a 409."""
        target = self._resolve(rel)
        if target.exists():
            if target.is_dir():
                raise InvalidProjectPath(
                    f"{rel!r} is a directory; cannot overwrite as a file"
                )
            if not overwrite:
                raise ProjectFileExists(rel)
            # Overwriting reclaims the old size — adjust quota check
            old_size = target.stat().st_size
            self._check_quota(max(0, len(content) - old_size))
        else:
            self._check_quota(len(content))
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish: write temp, rename. Best-effort on Windows
        # (rename semantics differ but `replace` covers it).
        tmp = target.with_suffix(target.suffix + ".uploading")
        tmp.write_bytes(content)
        tmp.replace(target)
        self._audit(
            "project.file.upload" if not overwrite else "project.file.overwrite",
            {"path": rel, "size_bytes": len(content)},
        )
        return self._stat_entry(target)

    def mkdir(self, rel: str) -> FileEntry:
        target = self._resolve(rel)
        if target.exists():
            if target.is_dir():
                return self._stat_entry(target)
            raise ProjectFileExists(rel)
        target.mkdir(parents=True)
        self._audit("project.file.mkdir", {"path": rel})
        return self._stat_entry(target)

    def move(self, from_rel: str, to_rel: str) -> FileEntry:
        src = self._resolve(from_rel)
        dst = self._resolve(to_rel)
        if not src.exists():
            raise ProjectFileNotFound(from_rel)
        if dst.exists():
            raise ProjectFileExists(to_rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        self._audit(
            "project.file.move",
            {"from": from_rel, "to": to_rel},
        )
        return self._stat_entry(dst)

    # ── Soft delete + trash ──────────────────────────────────────

    def soft_delete(self, rel: str) -> TrashEntry:
        target = self._resolve(rel)
        if not target.exists():
            raise ProjectFileNotFound(rel)

        trash_id = _new_trash_id()
        self.trash_dir.mkdir(exist_ok=True)
        trash_path = self.trash_dir / trash_id

        is_dir = target.is_dir()
        size_bytes = _size_of(target)
        shutil.move(str(target), str(trash_path))

        entry = TrashEntry(
            trash_id=trash_id,
            original_path=rel,
            trashed_at=datetime.utcnow().isoformat(),
            trashed_by=self.actor_id,
            size_bytes=size_bytes,
            is_dir=is_dir,
            trash_path=f".trash/{trash_id}",
        )
        self._append_trash_index(entry)
        self._audit(
            "project.file.delete",
            {
                "path": rel,
                "trash_id": trash_id,
                "size_bytes": size_bytes,
                "is_dir": is_dir,
            },
        )
        return entry

    def list_trash(self) -> list[TrashEntry]:
        return list(self._read_trash_index())

    def restore(self, trash_id: str) -> FileEntry:
        index = self._read_trash_index()
        entry = next((e for e in index if e.trash_id == trash_id), None)
        if entry is None:
            raise TrashEntryNotFound(trash_id)

        trash_path = self.workdir / entry.trash_path
        if not trash_path.exists():
            # Index references a missing file — drop the row, surface
            # a clean error.
            self._write_trash_index(
                [e for e in index if e.trash_id != trash_id]
            )
            raise TrashEntryNotFound(trash_id)

        # Restore target — if the original path is occupied, append
        # ``(restored)`` (and ``(restored 2)`` etc) before the suffix.
        original = self.workdir / entry.original_path
        target = original
        if target.exists():
            stem, dot, ext = (
                original.name.rpartition(".") if "." in original.name
                else (original.name, "", "")
            )
            suffix = f".{ext}" if ext else ""
            base = stem if dot else original.name
            i = 1
            while target.exists():
                marker = "(restored)" if i == 1 else f"(restored {i})"
                candidate = original.with_name(f"{base} {marker}{suffix}")
                target = candidate
                i += 1

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash_path), str(target))
        self._write_trash_index(
            [e for e in index if e.trash_id != trash_id]
        )
        self._audit(
            "project.file.restore",
            {
                "trash_id": trash_id,
                "original_path": entry.original_path,
                "restored_to": self._rel(target),
            },
        )
        return self._stat_entry(target)

    def purge(self, trash_id: str) -> None:
        index = self._read_trash_index()
        entry = next((e for e in index if e.trash_id == trash_id), None)
        if entry is None:
            raise TrashEntryNotFound(trash_id)
        trash_path = self.workdir / entry.trash_path
        if trash_path.exists():
            if trash_path.is_dir():
                shutil.rmtree(trash_path)
            else:
                trash_path.unlink()
        self._write_trash_index(
            [e for e in index if e.trash_id != trash_id]
        )
        self._audit(
            "project.file.purge",
            {"trash_id": trash_id, "original_path": entry.original_path},
        )

    def empty_trash(self) -> int:
        index = self._read_trash_index()
        count = 0
        for entry in index:
            trash_path = self.workdir / entry.trash_path
            if trash_path.exists():
                if trash_path.is_dir():
                    shutil.rmtree(trash_path, ignore_errors=True)
                else:
                    try:
                        trash_path.unlink()
                    except OSError:
                        pass
            count += 1
        self._write_trash_index([])
        self._audit("project.file.empty_trash", {"purged_count": count})
        return count

    # ── Internals ────────────────────────────────────────────────

    def _stat_entry(self, abs_path: Path) -> FileEntry:
        stat = abs_path.stat()
        return FileEntry(
            path=self._rel(abs_path),
            name=abs_path.name,
            is_dir=abs_path.is_dir(),
            size_bytes=0 if abs_path.is_dir() else stat.st_size,
            modified_at=datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
        )

    def _read_trash_index(self) -> list[TrashEntry]:
        if not self.trash_index_path.exists():
            return []
        try:
            raw = json.loads(self.trash_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning(
                "trash index unreadable for project %s — treating as empty",
                self.project.project_id,
            )
            return []
        out: list[TrashEntry] = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            try:
                out.append(
                    TrashEntry(
                        trash_id=row["trash_id"],
                        original_path=row["original_path"],
                        trashed_at=row.get("trashed_at", ""),
                        trashed_by=row.get("trashed_by", ""),
                        size_bytes=int(row.get("size_bytes", 0)),
                        is_dir=bool(row.get("is_dir", False)),
                        trash_path=row.get(
                            "trash_path", f".trash/{row['trash_id']}"
                        ),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def _write_trash_index(self, entries: list[TrashEntry]) -> None:
        self.trash_index_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            self.trash_index_path,
            json.dumps([asdict(e) for e in entries], indent=2),
        )

    def _append_trash_index(self, entry: TrashEntry) -> None:
        entries = self._read_trash_index()
        entries.append(entry)
        self._write_trash_index(entries)

    def _audit(self, action: str, details: dict[str, Any] | None = None) -> None:
        self.sess.add(
            AuditLogRow(
                actor_id=self.actor_id,
                action=action,
                target_type="project",
                target_id=self.project.project_id,
                details=details,
            )
        )
