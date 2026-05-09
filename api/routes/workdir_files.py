"""
User workdir file API — folder-as-cwd routes.

Replaces ``project_files.py`` (project-bound) with the folder-as-cwd
model: each user has a private workdir tree at
``<user_workdirs_root>/<user_id>/`` that's bind-mounted into their
sandbox container at ``/workdir/``. The Workspace UI browses this
tree; the chat ``cwd_path`` names a subfolder within it.

Routes (all under /api/v1/workdir):

    GET    /files?path=...           List directory contents
    POST   /folders                  Create a subfolder (mkdir)
    POST   /upload                   Upload a file (multipart)
    GET    /download?path=...        Download a file (streamed)

Authz:
    Every endpoint requires an authenticated principal (cookie or
    bearer). Each user can only see / mutate their OWN workdir —
    even admins. Path-as-authz applies at the cwd boundary: the
    user can never escape ``<root>/<their_user_id>/`` regardless
    of what ``path`` they send (path-traversal attempts get a 400).

What's NOT here (deliberately):
    * Soft-delete / trash / restore — postponed to v1.1; for v1.0
      ``DELETE`` removes immediately. Trash for the user-workdir
      model needs a redesigned trash root that doesn't conflict
      with subfolder names; layered separately.
    * Move / rename — same; v1.1.
    * Per-folder quota — single user-workdir-wide cap is enough
      for now. Multi-folder quota policies are an Enterprise feature.
    * import_from_library — refactored into a separate route
      under /workdir/import (S6).
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workdir", tags=["workdir-files"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FileEntryOut(BaseModel):
    path: str = Field(..., description="Path relative to the user's workdir root, e.g. /sales/2025/Q3.xlsx")
    name: str
    is_dir: bool
    size_bytes: int
    modified_at: str


class MkdirRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Folder path to create (relative to workdir root)")


class WorkdirInfo(BaseModel):
    """Surfaced to the client so the UI knows where the user is —
    useful for breadcrumbs / banner display."""
    user_id: str
    root_path: str = "/"


# ---------------------------------------------------------------------------
# Helpers — path resolution + safety
# ---------------------------------------------------------------------------


def _user_workdir_root(state: AppState, user_id: str) -> Path:
    """Compute (and auto-create) the user's private workdir root.

    Lives at ``<cfg.agent.user_workdirs_root>/<user_id>/`` — same
    location SandboxManager bind-mounts into the container at
    ``/workdir/``. First access auto-creates the directory so a
    user who hasn't chatted yet can still upload via the UI.
    """
    root_cfg = (
        getattr(state.cfg.agent, "user_workdirs_root", None)
        or "./storage/user-workdirs"
    )
    root = Path(root_cfg) / user_id
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _resolve_safe(workdir_root: Path, rel: str | None, *, allow_root: bool = False) -> Path:
    """Resolve ``rel`` against ``workdir_root``, refusing path-traversal.

    Path interpretation:
      * Leading ``/`` is allowed and means "from workdir root" — this
        is what the UI sends (``cwd_path = "/sales/2025"``). It does
        NOT mean host-absolute; the leading slash is just stripped
        and the rest is joined onto the workdir root.
      * Empty / "/" / "." → workdir root (only when ``allow_root=True``)
      * Windows drive letter (``C:\\...``), ``..`` segment → 400
      * Resolved path must remain a descendant of ``workdir_root`` —
        defence-in-depth against subtle traversal (symlinks etc.)
    """
    if rel is None:
        rel = ""
    rel = rel.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if rel in ("", "/", "."):
        if not allow_root:
            raise HTTPException(status_code=400, detail="path is required")
        return workdir_root
    # Windows drive letter (``C:foo`` / ``C:/foo``) is never valid —
    # we don't support host-absolute paths regardless of OS.
    if len(rel) >= 2 and rel[1] == ":":
        raise HTTPException(status_code=400, detail="drive-letter paths not allowed")
    # Strip leading slash — it just means "from workdir root", which
    # is what every join below already does. Keeping it would produce
    # a host-absolute path on POSIX which then gets rejected by the
    # ``relative_to`` check below; better to handle it as user intent.
    rel = rel.lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="path traversal not allowed")
    target = (workdir_root / "/".join(parts)).resolve()
    try:
        target.relative_to(workdir_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes workdir root")
    return target


def _to_workdir_relative(workdir_root: Path, abs_path: Path) -> str:
    """Format an absolute path as the cwd_path-style relative
    string the UI displays (leading slash, posix separators)."""
    try:
        rel = abs_path.relative_to(workdir_root)
    except ValueError:
        return ""
    s = "/" + str(rel).replace("\\", "/")
    return s.rstrip("/") or "/"


def _entry_out(workdir_root: Path, entry: Path) -> FileEntryOut:
    stat = entry.stat()
    return FileEntryOut(
        path=_to_workdir_relative(workdir_root, entry),
        name=entry.name,
        is_dir=entry.is_dir(),
        size_bytes=int(stat.st_size) if entry.is_file() else 0,
        modified_at=datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/info", response_model=WorkdirInfo)
def workdir_info(
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> WorkdirInfo:
    """Return basic info about the caller's private workdir.

    Used by the Workspace UI on first load to confirm the workdir
    is up + reportable, before kicking off the recursive listing.
    """
    _user_workdir_root(state, principal.user_id)  # ensure auto-create
    return WorkdirInfo(user_id=principal.user_id, root_path="/")


@router.get("/files", response_model=list[FileEntryOut])
def list_files(
    path: str | None = Query("", description="Folder path within workdir; '' = root"),
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> list[FileEntryOut]:
    """List the contents of a folder in the user's workdir.

    Folders sort first, then files; both alphabetically. Hidden
    entries (``.``-prefixed) are filtered — they're typically agent
    runtime state (``.agent-state/``) the user doesn't want to see.
    """
    root = _user_workdir_root(state, principal.user_id)
    target = _resolve_safe(root, path, allow_root=True)
    if not target.exists():
        raise HTTPException(status_code=404, detail="folder not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="path is a file, not a folder")

    entries = []
    for child in target.iterdir():
        if child.name.startswith("."):
            continue
        try:
            entries.append(_entry_out(root, child))
        except OSError:
            log.exception("workdir list: stat failed path=%s", child)
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


@router.post("/folders", response_model=FileEntryOut, status_code=201)
def make_dir(
    body: MkdirRequest,
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> FileEntryOut:
    """Create a folder inside the user's workdir. Idempotent —
    re-creating an existing folder returns its current entry,
    not 409, so the UI's "create or open" flow works without
    a precondition check."""
    root = _user_workdir_root(state, principal.user_id)
    target = _resolve_safe(root, body.path, allow_root=False)
    if target.exists() and not target.is_dir():
        raise HTTPException(status_code=409, detail="path exists and is not a folder")
    target.mkdir(parents=True, exist_ok=True)
    return _entry_out(root, target)


@router.post("/upload", response_model=FileEntryOut, status_code=201)
async def upload_file(
    path: str = Form(..., description="Destination folder, e.g. /sales/2025"),
    file: UploadFile = File(...),
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> FileEntryOut:
    """Upload a file to the user's workdir.

    ``path`` is the destination FOLDER (must already exist or be
    creatable). The uploaded file's filename comes from the
    multipart payload; the route refuses anything with path
    separators in the filename so a malicious upload can't
    escape the destination folder.
    """
    root = _user_workdir_root(state, principal.user_id)
    folder = _resolve_safe(root, path, allow_root=True)
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail="path is not a folder")

    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="filename must not contain slashes or start with '.'")

    dest = folder / filename
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except OSError as e:
        log.exception("workdir upload: write failed path=%s", dest)
        raise HTTPException(status_code=500, detail=f"write failed: {type(e).__name__}")
    return _entry_out(root, dest)


@router.get("/download")
def download_file(
    path: str = Query(..., description="File path within workdir"),
    inline: bool = Query(
        False,
        description=(
            "When true, serve with ``Content-Disposition: inline`` and a "
            "mime type derived from the extension so the browser renders "
            "the bytes directly (image, video, audio, pdf, html, plain "
            "text). Default false → forced download (existing 'Download' "
            "button behaviour)."
        ),
    ),
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    """Stream a file's bytes back to the client. Used by the
    Workbench UI's preview / "open file" gesture and by the
    post-agent-turn artifact download flow.

    Two modes:

      * ``inline=false`` (default) — ``application/octet-stream`` +
        ``Content-Disposition: attachment``. Forces a save dialog.
      * ``inline=true`` — best-guess mime from extension (e.g.
        ``image/png``, ``video/mp4``, ``application/pdf``,
        ``text/markdown``, ``text/csv``) + ``Content-Disposition:
        inline``. Lets ``<img>`` / ``<video>`` / ``<iframe src=...>``
        render the bytes directly without download. Unknown
        extensions still serve as ``application/octet-stream`` —
        the front-end's preview shell handles the unsupported case.
    """
    root = _user_workdir_root(state, principal.user_id)
    target = _resolve_safe(root, path, allow_root=False)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    def _stream():
        with target.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    if inline:
        media_type = _guess_inline_media_type(target.name)
        # Quote-escape the filename so names containing ``"`` don't
        # break the header (RFC 6266 — ``filename="..."`` is the
        # most-compatible shape; the path safety check upstream
        # already rejected slashes).
        safe_name = target.name.replace('"', '\\"')
        headers = {
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "X-Accel-Buffering": "no",
        }
    else:
        media_type = "application/octet-stream"
        headers = {
            "Content-Disposition": f'attachment; filename="{target.name}"',
            "X-Accel-Buffering": "no",
        }
    return StreamingResponse(_stream(), media_type=media_type, headers=headers)


# Keep this list narrow — only types the Workbench preview shell
# actually renders. Anything missing falls back to
# ``application/octet-stream`` which the browser will offer to
# download (the preview UI handles "unsupported" before reaching here).
_INLINE_MIME_BY_EXT: dict[str, str] = {
    # images
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "tif": "image/tiff", "tiff": "image/tiff",
    # video
    "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    # audio
    "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    "m4a": "audio/mp4", "flac": "audio/flac", "aac": "audio/aac",
    # docs
    "pdf": "application/pdf",
    "html": "text/html", "htm": "text/html",
    "md": "text/markdown", "markdown": "text/markdown",
    "txt": "text/plain", "log": "text/plain",
    "csv": "text/csv", "tsv": "text/tab-separated-values",
    "json": "application/json", "xml": "application/xml",
    "yaml": "text/yaml", "yml": "text/yaml", "toml": "text/plain",
}


def _guess_inline_media_type(name: str) -> str:
    """Map a filename extension to an inline-displayable mime type.
    Returns ``application/octet-stream`` when the extension isn't
    in the explicit allow-list — keeps the surface area narrow so
    we never accidentally serve a script with a content type the
    browser would execute (the preview UI's CSP + sandbox attrs
    are the second line of defence)."""
    name = name or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _INLINE_MIME_BY_EXT.get(ext, "application/octet-stream")
