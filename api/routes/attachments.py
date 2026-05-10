"""Chat-message attachments — upload + retrieve + delete.

Attachments are files the user pins to a chat conversation before
sending a message. The blob lives on disk under
``<cfg.agent.user_uploads_root>/<user_id>/<conv_id>/<id>__<name>``;
metadata lives in the ``attachments`` table.

Two-phase lifecycle:

  Draft   ``message_id`` is NULL — the user uploaded but hasn't sent.
  Bound   ``message_id`` is set — the chat route promotes drafts on
          send so the attachment lives as long as the message does.

This module owns Draft creation, listing (drafts + bound), blob
download, and delete. The Draft → Bound promotion happens in
``claude_chat.py`` when the user message is persisted.

Plain-text-y MIME types (``text/*`` + a curated set of textual
``application/*`` aliases) are always accepted regardless of the
configured generator's modality flags. Non-text MIMEs require the
matching ``answering.generator.capabilities`` flag (vision for
``image/*``, pdf for ``application/pdf``); without the flag the
upload is refused so the user doesn't ship bytes the model will
silently ignore.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
from ..state import AppState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["attachments"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AttachmentOut(BaseModel):
    attachment_id: str
    conversation_id: str
    message_id: str | None = None
    filename: str
    mime: str
    size_bytes: int
    kind: str
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Plain-text family — always allowed regardless of model capability.
# These inline cleanly into the prompt as text without any model-side
# parsing. Curated rather than wildcard so we don't accidentally
# accept e.g. ``application/octet-stream`` (= unknown binary).
_TEXTUAL_MIMES = {
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "text/html",
    "text/csv",
    "text/tab-separated-values",
    "text/xml",
    "text/x-log",
    "text/x-python",
    "text/x-shellscript",
    "application/json",
    "application/x-yaml",
    "application/yaml",
    "application/xml",
    "application/x-log",
    "application/x-python-code",
    "application/javascript",
    "application/typescript",
    "application/sql",
    "application/toml",
}

_MAX_BYTES = 25 * 1024 * 1024  # 25 MiB per upload — enough for "a paper" but
                               # below the typical LLM context window cap


def _user_uploads_root(state: AppState, user_id: str) -> Path:
    """Compute (and auto-create) the user's chat-attachments root.

    Tree: ``<user_uploads_root>/<user_id>/`` with per-conversation
    subdirs created on demand by ``_conv_dir``. Mirrors how
    ``user_workdirs_root`` lays out its tree, just on a separate
    branch of the storage tree.
    """
    root_cfg = (
        getattr(state.cfg.agent, "user_uploads_root", None)
        or "./storage/user-uploads"
    )
    root = Path(root_cfg) / user_id
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _conv_dir(state: AppState, user_id: str, conv_id: str) -> Path:
    """Per-conversation subdir, auto-created on first upload.

    Containing the conv id in the path means "delete the
    conversation's attachments" is a single ``shutil.rmtree`` of
    this dir — no scanning + matching the way a flat tree would
    require.
    """
    d = _user_uploads_root(state, user_id) / conv_id
    d.mkdir(parents=True, exist_ok=True)
    return d


_FILENAME_SAFE_RE = re.compile(r"[\\/\x00]")


def _safe_filename(raw: str | None) -> str:
    """Strip path separators + control chars, fall back to a safe name.

    Keeps the original filename's general look (so the user sees
    "report.pdf" not "8a3...c2.pdf") while making it impossible for
    the multipart filename to traverse out of the per-user dir or
    smuggle a different file extension via embedded slashes.
    """
    name = (raw or "").strip()
    if not name:
        return "upload.bin"
    # Path separators / nul → underscore. Leading dot kept off so we
    # don't accidentally land on dotfile-style hidden uploads.
    name = _FILENAME_SAFE_RE.sub("_", name)
    if name.startswith("."):
        name = "_" + name[1:]
    # Cap the length so an absurdly long filename can't blow the
    # column limit (filename varchar(512)).
    return name[:200]


def _classify(mime: str) -> str:
    """Coarse kind label saved alongside each row.

    Mirrors the MIME-family branching in the agent runtime: every
    attachment ends up in exactly one of ``text`` / ``image`` /
    ``pdf`` / ``other`` so the runtime can ``match kind: ...`` once
    instead of re-parsing the MIME on every chat turn.
    """
    if mime in _TEXTUAL_MIMES or mime.startswith("text/"):
        return "text"
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    return "other"


def _capabilities(state: AppState) -> tuple[bool, bool]:
    """Read ``vision`` / ``pdf`` flags from the configured generator.

    Returns ``(False, False)`` when the answering section / generator
    section / capabilities subsection is missing — the safe default
    so an unconfigured deployment can't accidentally accept binary
    uploads it can't process.
    """
    gen = getattr(getattr(state.cfg, "answering", None), "generator", None)
    caps = getattr(gen, "capabilities", None)
    if caps is None:
        return False, False
    return bool(getattr(caps, "vision", False)), bool(getattr(caps, "pdf", False))


def _owns_conversation(row: dict, owner_user_id: str) -> bool:
    """Per-user privacy check — same shape as the conversations route.

    A conversation belongs to its creator; even admins don't get to
    upload attachments to other users' chats.
    """
    return bool(row) and row.get("user_id") == owner_user_id


def _to_out(row: dict) -> AttachmentOut:
    return AttachmentOut(
        attachment_id=row["attachment_id"],
        conversation_id=row["conversation_id"],
        message_id=row.get("message_id"),
        filename=row["filename"],
        mime=row["mime"],
        size_bytes=row["size_bytes"],
        kind=row["kind"],
        created_at=row.get("created_at"),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/conversations/{conversation_id}/attachments",
    response_model=AttachmentOut,
    status_code=201,
)
async def upload_attachment(
    conversation_id: str,
    file: UploadFile = File(...),
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> AttachmentOut:
    """Upload a draft attachment to a conversation.

    Refuses uploads whose MIME isn't one of the always-allowed text
    types AND isn't covered by the configured generator's capability
    flags. Returns 413 for over-budget uploads and 415 for refused
    MIMEs so the frontend can surface a clear error.
    """
    conv = state.store.get_conversation(conversation_id)
    if not conv or not _owns_conversation(conv, principal.user_id):
        raise HTTPException(status_code=404, detail="conversation not found")

    raw_name = file.filename
    filename = _safe_filename(raw_name)
    # Browser-supplied MIME first; fall back to extension-based guess
    # when missing (some uploaders don't bother setting it). Default
    # to ``application/octet-stream`` so the kind classifier returns
    # ``other`` and the capability check rejects the upload — safer
    # than silently accepting it.
    mime = (file.content_type or "").strip().lower()
    if not mime or mime == "application/octet-stream":
        guess, _ = mimetypes.guess_type(filename)
        mime = (guess or "application/octet-stream").lower()

    kind = _classify(mime)
    cap_vision, cap_pdf = _capabilities(state)
    if kind == "image" and not cap_vision:
        raise HTTPException(
            status_code=415,
            detail="The configured model does not accept images. Switch to a vision-capable model or paste the content as text.",
        )
    if kind == "pdf" and not cap_pdf:
        raise HTTPException(
            status_code=415,
            detail="The configured model does not accept PDFs natively. Switch to a PDF-capable model or convert the file to text.",
        )
    if kind == "other":
        raise HTTPException(
            status_code=415,
            detail=f"Attachment type {mime!r} not supported.",
        )

    aid = uuid4().hex
    dest_dir = _conv_dir(state, principal.user_id, conversation_id)
    dest = dest_dir / f"{aid}__{filename}"

    sha = hashlib.sha256()
    written = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_BYTES:
                    out.close()
                    try:
                        os.unlink(dest)
                    except OSError:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=f"Attachment exceeds {_MAX_BYTES} bytes.",
                    )
                sha.update(chunk)
                out.write(chunk)
    except HTTPException:
        raise
    except OSError:
        log.exception("attachment upload: write failed dest=%s", dest)
        raise HTTPException(status_code=500, detail="write failed")

    record = {
        "attachment_id": aid,
        "conversation_id": conversation_id,
        "message_id": None,
        "user_id": principal.user_id,
        "filename": filename,
        "mime": mime,
        "size_bytes": written,
        "sha256": sha.hexdigest(),
        "kind": kind,
        # Stored relative to the per-user-uploads root (re-resolved
        # at read time against the live ``user_uploads_root`` cfg —
        # so moving the storage tree only requires re-pointing the
        # root, no row rewrites).
        "blob_path": f"{principal.user_id}/{conversation_id}/{aid}__{filename}",
    }
    state.store.add_attachment(record)
    return _to_out(record)


@router.get(
    "/conversations/{conversation_id}/attachments",
    response_model=list[AttachmentOut],
)
def list_attachments(
    conversation_id: str,
    only_drafts: bool = Query(
        False,
        description=(
            "When true, return only attachments not yet bound to a "
            "message (the user-staged uploads visible in the input "
            "row's chip rail). False returns every attachment that "
            "lives on this conversation, bound or not."
        ),
    ),
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> list[AttachmentOut]:
    conv = state.store.get_conversation(conversation_id)
    if not conv or not _owns_conversation(conv, principal.user_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = state.store.list_attachments_for_conversation(
        conversation_id, only_drafts=only_drafts
    )
    return [_to_out(r) for r in rows]


@router.get("/attachments/{attachment_id}/blob")
def download_attachment(
    attachment_id: str,
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    """Stream the raw blob back. Used by the frontend's preview
    affordance (clicking an attachment chip) and by the agent
    runtime when it needs to inline the bytes into a content block.

    Authz: the caller must own the conversation the attachment lives
    on. Same per-user privacy stance as the rest of the chat API —
    no admin bypass, no token-based public read.
    """
    row = state.store.get_attachment(attachment_id)
    if not row or row.get("user_id") != principal.user_id:
        raise HTTPException(status_code=404, detail="attachment not found")

    root_cfg = (
        getattr(state.cfg.agent, "user_uploads_root", None)
        or "./storage/user-uploads"
    )
    blob = (Path(root_cfg) / row["blob_path"]).resolve()
    # Defence in depth: the path stored in DB is built from
    # uuid-prefixed components we control, but a future migration
    # bug could let a malicious value land in ``blob_path``. Verify
    # the resolved file stays under the cfg root before opening it.
    try:
        blob.relative_to(Path(root_cfg).resolve())
    except ValueError:
        log.error("attachment blob escapes uploads root: id=%s path=%s", attachment_id, blob)
        raise HTTPException(status_code=404, detail="attachment not found")
    if not blob.exists() or not blob.is_file():
        raise HTTPException(status_code=404, detail="attachment file missing")

    def _stream():
        with blob.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    safe_name = (row.get("filename") or "attachment").replace('"', '\\"')
    headers = {
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _stream(), media_type=row.get("mime") or "application/octet-stream", headers=headers,
    )


@router.delete("/attachments/{attachment_id}", status_code=204)
def delete_attachment(
    attachment_id: str,
    principal: AuthenticatedPrincipal = Depends(get_principal),
    state: AppState = Depends(get_state),
) -> Response:
    """Delete an attachment (DB row + blob).

    Allowed for both drafts and bound attachments — the user might
    decide a previous turn's attachment is sensitive and want it
    gone. The bound message's ``content`` text isn't touched; only
    the file disappears.
    """
    row = state.store.get_attachment(attachment_id)
    if not row or row.get("user_id") != principal.user_id:
        raise HTTPException(status_code=404, detail="attachment not found")

    state.store.delete_attachment(attachment_id)

    root_cfg = (
        getattr(state.cfg.agent, "user_uploads_root", None)
        or "./storage/user-uploads"
    )
    blob = (Path(root_cfg) / row["blob_path"]).resolve()
    try:
        blob.relative_to(Path(root_cfg).resolve())
        if blob.exists():
            blob.unlink()
    except (ValueError, OSError):
        # DB row is already gone; failing to remove the blob just
        # means a stale file on disk — non-fatal, log + continue.
        log.warning("attachment delete: blob unlink failed id=%s path=%s",
                    attachment_id, blob)

    return Response(status_code=204)
