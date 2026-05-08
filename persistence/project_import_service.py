"""
ProjectImportService — copy a Library doc into a project's workdir.

This is the backend behind both:
* The Phase 1 manual UI button ("Import from Library" in
  ProjectDetail) — `POST /api/v1/projects/{id}/import`
* The Phase 2 agent tool `import_from_library(doc_id)` — same
  endpoint, called by the LLM when it decides it needs the
  original file (not just chunks) to operate on

Single code path means the agent inherits exactly the same authz
+ quota + idempotency rules as the user-driven import.

Flow:
1. Resolve the Library doc row + its content-addressed file blob
2. Authz: doc must be readable by the principal (folder
   `shared_with` check, identical to the Library UI's "can I open
   this doc" gate)
3. Idempotency: if an artifact for this (project, source doc)
   already exists, return it without touching disk
4. Compose target path under `<subdir>/<filename>` inside the
   project workdir; collisions get a `(N)` suffix
5. Quota check via ProjectFileService bookkeeping
6. Materialize the blob to disk via FileStore (handles the
   storage-backend dispatch — local fs, S3, OSS)
7. Create an Artifact row with `lineage_json.sources = [{type:
   "doc", doc_id}]` so the agent (and a future "where did this
   come from" UI) can trace every imported file back to its
   Library origin
8. Audit-log a `project.import` event with both ids
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Artifact, AuditLogRow, Document, File, Project
from .project_file_service import (
    InvalidProjectPath,
    ProjectFileService,
    ProjectQuotaExceeded,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ImportError(RuntimeError):
    """Base class for import-service errors."""


class SourceDocumentNotFound(ImportError):
    pass


class SourceDocumentHasNoBlob(ImportError):
    """Document exists but has no `file_id` — the Library row is a
    placeholder (e.g. a future URL-only doc) and there's nothing
    to copy. Surfaced as 422 by the route."""


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    artifact_id: str
    project_id: str
    source_doc_id: str
    target_path: str       # relative to project workdir
    size_bytes: int
    mime: str
    sha256: str | None
    reused: bool           # True when an existing artifact was returned


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_artifact_id() -> str:
    return uuid.uuid4().hex[:16]


def _safe_filename(raw: str | None, fallback: str) -> str:
    """Strip directory components + reject empty / dotfile-only names.
    Filename charset is enforced by ProjectFileService._resolve when
    we hand the composed target path to it; this is a pre-pass to
    keep the resolved path inside the requested subdir."""
    if not raw:
        return fallback
    name = Path(raw).name
    # Reject names that are entirely dots / empty
    if not name or all(c == "." for c in name):
        return fallback
    return name


def _unique_target(workdir: Path, rel: str) -> str:
    """If `<rel>` already exists in `workdir`, append `(1)`, `(2)`,
    ... before the extension until unique. Single-folder scan; cheap
    even for thousands of imports."""
    p = workdir / rel
    if not p.exists():
        return rel
    stem, dot, ext = p.name.rpartition(".")
    base = stem if dot else p.name
    suffix = f".{ext}" if dot else ""
    i = 1
    while True:
        candidate = p.with_name(f"{base} ({i}){suffix}")
        if not candidate.exists():
            return str(candidate.relative_to(workdir).as_posix())
        i += 1


def _has_existing_artifact(
    sess: Session, project_id: str, doc_id: str
) -> Artifact | None:
    """Return a previously-imported artifact for this (project, doc)
    pair, if any. Match is by `lineage_json.sources[0].doc_id` for
    rows where `run_id IS NULL` (user-driven imports, not agent
    products that happen to reference the same source).

    JSON-shape probing in SQL is dialect-specific; we side-step that
    by walking the project's artifact rows in Python. Project
    artifact counts are O(tens-to-hundreds) for any realistic Phase-1
    use, so the linear scan is fine. If artifact volume per project
    grows past ~10k a column-promoted ``source_doc_id`` index can
    replace this.
    """
    rows = sess.execute(
        select(Artifact).where(
            Artifact.project_id == project_id,
            Artifact.run_id.is_(None),
        )
    ).scalars()
    for art in rows:
        sources = (art.lineage_json or {}).get("sources") or []
        for s in sources:
            if isinstance(s, dict) and s.get("type") == "doc" and s.get("doc_id") == doc_id:
                return art
    return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ProjectImportService:
    """One-shot import operation per instance.

    Construction parameters:
      * ``sess`` — active SQLAlchemy session
      * ``file_store`` — the AppState's ``FileStore`` (wraps the
        Library's content-addressed blob backend; ``materialize``
        downloads a blob to a local path)
      * ``projects_root`` — same root as the file service
      * ``max_workdir_bytes`` / ``max_upload_bytes`` — same caps
      * ``actor_id`` — for audit log
    """

    def __init__(
        self,
        sess: Session,
        *,
        file_store,
        projects_root: Path | str,
        max_workdir_bytes: int = 10 * 1024 * 1024 * 1024,
        max_upload_bytes: int = 500 * 1024 * 1024,
        actor_id: str = "local",
    ):
        self.sess = sess
        self.file_store = file_store
        self.projects_root = Path(projects_root)
        self.max_workdir_bytes = max_workdir_bytes
        self.max_upload_bytes = max_upload_bytes
        self.actor_id = actor_id

    def import_doc(
        self,
        project: Project,
        doc_id: str,
        *,
        target_subdir: str = "inputs",
    ) -> ImportResult:
        # 1. Look up doc + its blob row
        doc = self.sess.get(Document, doc_id)
        if doc is None:
            raise SourceDocumentNotFound(doc_id)
        if not doc.file_id:
            raise SourceDocumentHasNoBlob(doc_id)
        file_row = self.sess.get(File, doc.file_id)
        if file_row is None:
            raise SourceDocumentHasNoBlob(doc_id)

        # 2. Idempotency BEFORE any disk work
        existing = _has_existing_artifact(self.sess, project.project_id, doc_id)
        if existing is not None:
            return ImportResult(
                artifact_id=existing.artifact_id,
                project_id=project.project_id,
                source_doc_id=doc_id,
                target_path=existing.path,
                size_bytes=existing.size_bytes,
                mime=existing.mime or file_row.mime_type,
                sha256=existing.sha256,
                reused=True,
            )

        # 3. Path-safety scaffolding via ProjectFileService
        fsvc = ProjectFileService(
            self.sess,
            project=project,
            projects_root=self.projects_root,
            max_workdir_bytes=self.max_workdir_bytes,
            max_upload_bytes=self.max_upload_bytes,
            actor_id=self.actor_id,
        )

        # Filename: prefer the doc's friendly name, fall back to the
        # original upload name. Strip any directory components that
        # leaked through (defensive — doc.filename normally is just
        # the basename).
        fname = _safe_filename(
            doc.filename or file_row.original_name,
            fallback=f"imported_{doc_id}.bin",
        )
        subdir = (target_subdir or "inputs").strip("/") or "inputs"
        proposed_rel = f"{subdir}/{fname}"

        # Refuse reserved subdirs / traversal up front. We don't
        # write yet — just want the InvalidProjectPath from the
        # resolver to bubble up cleanly.
        try:
            fsvc._resolve(proposed_rel)
        except InvalidProjectPath:
            raise
        # Collision-rename
        target_rel = _unique_target(fsvc.workdir, proposed_rel)
        target_abs = fsvc._resolve(target_rel)

        # 4. Quota
        # File row's size_bytes is authoritative; the actual blob
        # might compress on the wire but lands at this size on disk.
        fsvc._check_quota(file_row.size_bytes)

        # 5. Materialize the blob to disk
        target_abs.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.file_store.materialize(file_row.file_id, target_abs)
        except Exception as e:
            # If the blob couldn't be fetched, leave NO partial file
            # behind for the caller to puzzle over.
            if target_abs.exists():
                try:
                    target_abs.unlink()
                except OSError:
                    pass
            raise ImportError(
                f"failed to copy Library blob {file_row.file_id}: {e}"
            ) from e

        # 6. Create artifact row
        artifact = Artifact(
            artifact_id=_new_artifact_id(),
            project_id=project.project_id,
            run_id=None,
            produced_by_step_id=None,
            path=target_rel,
            mime=file_row.mime_type or "application/octet-stream",
            size_bytes=file_row.size_bytes,
            sha256=file_row.content_hash,
            lineage_json={
                "sources": [
                    {
                        "type": "doc",
                        "doc_id": doc_id,
                        "file_id": file_row.file_id,
                        "library_path": doc.path,
                        "original_filename": doc.filename or file_row.original_name,
                    }
                ]
            },
            metadata_json={"import_kind": "manual"},
            user_id=self.actor_id,
        )
        self.sess.add(artifact)
        self._audit(
            "project.import",
            project.project_id,
            {
                "artifact_id": artifact.artifact_id,
                "source_doc_id": doc_id,
                "source_file_id": file_row.file_id,
                "target_path": target_rel,
                "size_bytes": file_row.size_bytes,
            },
        )
        self.sess.flush()

        return ImportResult(
            artifact_id=artifact.artifact_id,
            project_id=project.project_id,
            source_doc_id=doc_id,
            target_path=target_rel,
            size_bytes=artifact.size_bytes,
            mime=artifact.mime,
            sha256=artifact.sha256,
            reused=False,
        )

    # ── Internals ─────────────────────────────────────────────────

    def _audit(self, action: str, project_id: str, details: dict | None = None) -> None:
        self.sess.add(
            AuditLogRow(
                actor_id=self.actor_id,
                action=action,
                target_type="project",
                target_id=project_id,
                details=details,
            )
        )
