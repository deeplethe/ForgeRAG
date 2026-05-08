"""
Project workdir file API + trash routes.

All endpoints under /api/v1/projects/{project_id}/files and
/api/v1/projects/{project_id}/trash. Mounted alongside the
projects router in api/app.py.

Routes:
    GET    /api/v1/projects/{id}/files?path=...       List dir contents
    POST   /api/v1/projects/{id}/files                Upload (multipart)
    GET    /api/v1/projects/{id}/files/download?path  Download a file
    PATCH  /api/v1/projects/{id}/files/move           Rename / move
    DELETE /api/v1/projects/{id}/files?path=...       Soft-delete
    POST   /api/v1/projects/{id}/files/mkdir          Create a subdir

    GET    /api/v1/projects/{id}/trash                List trash entries
    POST   /api/v1/projects/{id}/trash/{tid}/restore  Restore a file
    DELETE /api/v1/projects/{id}/trash/{tid}          Hard-purge one entry
    POST   /api/v1/projects/{id}/trash/empty          Empty whole trash

Authz:
- ``read`` ops (list / download / list-trash) need read access on
  the project (owner / admin / read-only viewer)
- ``write`` ops (upload / move / mkdir / soft-delete / restore /
  purge / empty) need write access (owner / admin only — no
  ``rw`` role today; viewers cannot mutate)
- 404 on no-access (existence privacy)

Path safety + quota are enforced by ``ProjectFileService``; this
module is just the FastAPI shape on top.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from persistence.project_file_service import (
    InvalidProjectPath,
    ProjectFileExists,
    ProjectFileNotFound,
    ProjectFileService,
    ProjectQuotaExceeded,
    ProjectUploadTooLarge,
    TrashEntry,
    TrashEntryNotFound,
)
from persistence.project_import_service import (
    ImportError as ProjectImportError,
)
from persistence.project_import_service import (
    ProjectImportService,
    SourceDocumentHasNoBlob,
    SourceDocumentNotFound,
)
from persistence.project_service import ProjectNotFound, ProjectService

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state, require_doc_access
from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects", tags=["project-files"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FileEntryOut(BaseModel):
    path: str
    name: str
    is_dir: bool
    size_bytes: int
    modified_at: str


class TrashEntryOut(BaseModel):
    trash_id: str
    original_path: str
    trashed_at: str
    trashed_by: str
    size_bytes: int
    is_dir: bool


class MoveReq(BaseModel):
    from_path: str = Field(..., description="Current relative path")
    to_path: str = Field(..., description="New relative path")


class MkdirReq(BaseModel):
    path: str = Field(..., description="Relative path of the directory to create")


class ImportFromLibraryReq(BaseModel):
    doc_id: str = Field(..., description="Library document to copy in")
    target_subdir: str = Field(
        default="inputs",
        description="Project subdir to land the file in (default: inputs/)",
    )


class ImportResultOut(BaseModel):
    artifact_id: str
    project_id: str
    source_doc_id: str
    target_path: str
    size_bytes: int
    mime: str
    sha256: str | None = None
    reused: bool


class WriteResultOut(FileEntryOut):
    """Same shape as FileEntryOut; declared separately so the OpenAPI
    schema reads cleanly per-endpoint."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _projects_root(state: AppState) -> Path:
    return Path(getattr(state.cfg.agent, "projects_root", "./storage/projects"))


def _is_admin(state: AppState, principal: AuthenticatedPrincipal) -> bool:
    if not state.cfg.auth.enabled:
        return True
    return principal.role == "admin" or principal.via == "auth_disabled"


def _make_file_service(
    sess,
    state: AppState,
    project,
    principal: AuthenticatedPrincipal,
) -> ProjectFileService:
    cfg = state.cfg.agent
    return ProjectFileService(
        sess,
        project=project,
        projects_root=_projects_root(state),
        max_workdir_bytes=getattr(cfg, "max_project_workdir_bytes", 0),
        max_upload_bytes=getattr(cfg, "max_workdir_upload_bytes", 0),
        actor_id=principal.user_id,
    )


def _resolve_project(
    state: AppState,
    principal: AuthenticatedPrincipal,
    project_id: str,
    *,
    require_write: bool,
):
    """Load the project row, gate on read or write permission, raise
    404 on miss/no-access. Returns (sess-context-manager, project)
    pairing so the caller can keep operating in the same transaction.

    Routes that read OR write all need this resolution; we keep the
    transaction inline at the call site so the response can stream
    after the session commits (download + list).
    """
    is_admin = _is_admin(state, principal)
    sess_ctx = state.store.transaction()
    sess = sess_ctx.__enter__()
    try:
        svc = ProjectService(
            sess,
            projects_root=_projects_root(state),
            actor_id=principal.user_id,
        )
        try:
            proj = svc.require(project_id)
        except ProjectNotFound:
            sess_ctx.__exit__(None, None, None)
            raise HTTPException(404, "project not found")
        action = "write" if require_write else "read"
        if not svc.can_access(
            proj, principal.user_id, action, is_admin=is_admin
        ):
            sess_ctx.__exit__(None, None, None)
            raise HTTPException(404, "project not found")
        return sess_ctx, sess, proj
    except HTTPException:
        raise
    except Exception:
        sess_ctx.__exit__(None, None, None)
        raise


def _entry_to_out(e) -> FileEntryOut:
    return FileEntryOut(
        path=e.path,
        name=e.name,
        is_dir=e.is_dir,
        size_bytes=e.size_bytes,
        modified_at=e.modified_at,
    )


def _trash_to_out(e: TrashEntry) -> TrashEntryOut:
    return TrashEntryOut(
        trash_id=e.trash_id,
        original_path=e.original_path,
        trashed_at=e.trashed_at,
        trashed_by=e.trashed_by,
        size_bytes=e.size_bytes,
        is_dir=e.is_dir,
    )


def _map_fs_error(e: Exception) -> HTTPException:
    """Map ProjectFileService exceptions to HTTP responses with
    consistent codes across endpoints."""
    if isinstance(e, ProjectFileNotFound):
        return HTTPException(404, f"file not found: {e}")
    if isinstance(e, TrashEntryNotFound):
        return HTTPException(404, f"trash entry not found: {e}")
    if isinstance(e, ProjectFileExists):
        return HTTPException(409, f"path already exists: {e}")
    if isinstance(e, ProjectQuotaExceeded):
        return HTTPException(413, f"project workdir quota exceeded: {e}")
    if isinstance(e, ProjectUploadTooLarge):
        return HTTPException(413, f"upload too large: {e}")
    if isinstance(e, InvalidProjectPath):
        return HTTPException(400, f"invalid path: {e}")
    return HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# File routes
# ---------------------------------------------------------------------------


@router.get("/{project_id}/files", response_model=list[FileEntryOut])
def list_files(
    project_id: str,
    path: str = Query("", description="Relative path; empty = workdir root"),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=False
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        try:
            entries = fsvc.list(path)
        except (InvalidProjectPath, ProjectFileNotFound) as e:
            raise _map_fs_error(e)
        sess_ctx.__exit__(None, None, None)
        return [_entry_to_out(e) for e in entries]
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise


@router.post(
    "/{project_id}/files", response_model=WriteResultOut, status_code=201
)
async def upload_file(
    project_id: str,
    file: UploadFile = File(...),
    path: str = Form(..., description="Target relative path"),
    overwrite: bool = Form(False),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Multipart upload to a path inside the project workdir.

    Path layout convention (not enforced):
      - inputs/    user-supplied data
      - outputs/   files the user wants to keep
      - scratch/   intermediate / safe to delete
    """
    data = await file.read()
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=True
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        try:
            entry = fsvc.write(path, data, overwrite=overwrite)
        except Exception as e:
            raise _map_fs_error(e)
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise
    return WriteResultOut(**vars(entry))


@router.get("/{project_id}/files/download")
def download_file(
    project_id: str,
    path: str = Query(..., description="Relative path of the file to download"),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=False
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        try:
            content, mime, name = fsvc.read(path)
        except Exception as e:
            raise _map_fs_error(e)
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise

    def _gen():
        yield content

    return StreamingResponse(
        _gen(),
        media_type=mime,
        headers={
            # `attachment` so browsers download rather than render
            # potentially-malicious content. Filename is sanitised
            # by the resolver so it's safe in the header.
            "Content-Disposition": f'attachment; filename="{name}"',
        },
    )


@router.patch("/{project_id}/files/move", response_model=FileEntryOut)
def move_file(
    project_id: str,
    body: MoveReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=True
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        try:
            entry = fsvc.move(body.from_path, body.to_path)
        except Exception as e:
            raise _map_fs_error(e)
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise
    return _entry_to_out(entry)


@router.delete("/{project_id}/files", response_model=TrashEntryOut)
def soft_delete_file(
    project_id: str,
    path: str = Query(..., description="Relative path to soft-delete"),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=True
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        try:
            entry = fsvc.soft_delete(path)
        except Exception as e:
            raise _map_fs_error(e)
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise
    return _trash_to_out(entry)


@router.post("/{project_id}/files/mkdir", response_model=FileEntryOut, status_code=201)
def make_dir(
    project_id: str,
    body: MkdirReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=True
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        try:
            entry = fsvc.mkdir(body.path)
        except Exception as e:
            raise _map_fs_error(e)
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise
    return _entry_to_out(entry)


# ---------------------------------------------------------------------------
# Library → Workspace import
# ---------------------------------------------------------------------------


@router.post(
    "/{project_id}/import",
    response_model=ImportResultOut,
    status_code=201,
)
def import_from_library(
    project_id: str,
    body: ImportFromLibraryReq,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Copy a Library document's blob into this project's workdir as
    an Artifact.

    Authz (both checks must pass):
      1. Caller has **write** access on the project (owner / admin).
         Read-only viewers cannot import — they'd be writing into
         someone else's workdir.
      2. Caller has **read** access on the source Library doc — the
         same gate the Library UI's "open this doc" button uses. A
         user can't import a file they wouldn't see in the Library.

    Idempotent: importing the same `doc_id` into the same project
    twice returns the existing artifact (200/201 with `reused=true`)
    without re-copying the blob.

    Phase 2 will mount the Phase-2 agent tool `import_from_library`
    on the same service — no other endpoint, no other authz model.
    """
    # 1. Project resolution + write-permission gate (404 on miss)
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=True
    )
    try:
        # 2. Library doc-access gate. ``require_doc_access`` returns
        #    the doc row OR raises 404 — same code as a missing doc,
        #    so an unauthorised caller can't enumerate doc_ids by
        #    diffing 404 vs 403.
        try:
            require_doc_access(state, principal, body.doc_id, "read")
        except HTTPException:
            sess_ctx.__exit__(None, None, None)
            raise

        # 3. Run the import
        svc = ProjectImportService(
            sess,
            file_store=state.file_store,
            projects_root=_projects_root(state),
            max_workdir_bytes=getattr(
                state.cfg.agent, "max_project_workdir_bytes", 0
            ),
            max_upload_bytes=getattr(
                state.cfg.agent, "max_workdir_upload_bytes", 0
            ),
            actor_id=principal.user_id,
        )
        try:
            result = svc.import_doc(
                proj, body.doc_id, target_subdir=body.target_subdir
            )
        except SourceDocumentNotFound:
            # Should not be reachable past require_doc_access, but
            # keep the explicit handler for clarity.
            sess_ctx.__exit__(None, None, None)
            raise HTTPException(404, "library document not found")
        except SourceDocumentHasNoBlob:
            sess_ctx.__exit__(None, None, None)
            raise HTTPException(
                422,
                "library document has no associated file blob to copy",
            )
        except ProjectQuotaExceeded as e:
            sess_ctx.__exit__(None, None, None)
            raise HTTPException(413, str(e))
        except InvalidProjectPath as e:
            sess_ctx.__exit__(None, None, None)
            raise HTTPException(400, str(e))
        except ProjectImportError as e:
            sess_ctx.__exit__(None, None, None)
            raise HTTPException(500, f"import failed: {e}")
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        raise
    except Exception:
        sess_ctx.__exit__(None, None, None)
        raise

    return ImportResultOut(
        artifact_id=result.artifact_id,
        project_id=result.project_id,
        source_doc_id=result.source_doc_id,
        target_path=result.target_path,
        size_bytes=result.size_bytes,
        mime=result.mime,
        sha256=result.sha256,
        reused=result.reused,
    )


# ---------------------------------------------------------------------------
# Trash routes
# ---------------------------------------------------------------------------


@router.get("/{project_id}/trash", response_model=list[TrashEntryOut])
def list_trash(
    project_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=False
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        entries = fsvc.list_trash()
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise
    return [_trash_to_out(e) for e in entries]


@router.post(
    "/{project_id}/trash/{trash_id}/restore", response_model=FileEntryOut
)
def restore_trash(
    project_id: str,
    trash_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=True
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        try:
            entry = fsvc.restore(trash_id)
        except Exception as e:
            raise _map_fs_error(e)
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise
    return _entry_to_out(entry)


@router.delete("/{project_id}/trash/{trash_id}", status_code=204)
def purge_trash_entry(
    project_id: str,
    trash_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=True
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        try:
            fsvc.purge(trash_id)
        except Exception as e:
            raise _map_fs_error(e)
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise
    return None


@router.post("/{project_id}/trash/empty")
def empty_trash(
    project_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    sess_ctx, sess, proj = _resolve_project(
        state, principal, project_id, require_write=True
    )
    try:
        fsvc = _make_file_service(sess, state, proj, principal)
        purged = fsvc.empty_trash()
        sess_ctx.__exit__(None, None, None)
    except HTTPException:
        sess_ctx.__exit__(None, None, None)
        raise
    return {"purged_count": purged}
