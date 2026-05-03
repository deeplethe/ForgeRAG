"""
/api/v1/documents — ingestion, listing, detail, delete, reparse
/api/v1/documents/{doc_id}/blocks
/api/v1/documents/{doc_id}/chunks
/api/v1/documents/{doc_id}/tree
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from ..deps import get_state
from ..schemas import (
    BlockOut,
    ChunkOut,
    DocumentOut,
    IngestAcceptedResponse,
    IngestRequest,
    IngestResponse,
    PaginatedResponse,
    TreeNodeOut,
    TreeOut,
)
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_out(row: dict, state: AppState | None = None) -> DocumentOut:
    out = DocumentOut(**{k: row[k] for k in DocumentOut.model_fields if k in row})
    if state is not None:
        pv = row.get("active_parse_version", 1)
        doc_id = row["doc_id"]
        try:
            out.num_blocks = state.store.count_blocks(doc_id, pv)
            out.num_chunks = state.store.count_chunks(doc_id, pv)
        except Exception as e:
            log.warning("failed to count blocks/chunks for %s: %s", doc_id, e)
        file_id = row.get("file_id")
        if file_id:
            try:
                f = state.store.get_file(file_id)
                if f:
                    out.file_name = f.get("original_name")
                    out.file_size_bytes = f.get("size_bytes")
            except Exception:
                pass
    return out


def _block_out(row: dict) -> BlockOut:
    return BlockOut(
        block_id=row["block_id"],
        doc_id=row["doc_id"],
        parse_version=row["parse_version"],
        page_no=row["page_no"],
        seq=row["seq"],
        bbox={"x0": row["bbox_x0"], "y0": row["bbox_y0"], "x1": row["bbox_x1"], "y1": row["bbox_y1"]},
        type=row["type"],
        level=row.get("level"),
        text=row["text"],
        confidence=row["confidence"],
        table_html=row.get("table_html"),
        table_markdown=row.get("table_markdown"),
        image_storage_key=row.get("image_storage_key"),
        image_caption=row.get("image_caption"),
        formula_latex=row.get("formula_latex"),
        code_text=row.get("code_text"),
        code_language=row.get("code_language"),
        excluded=row["excluded"],
        excluded_reason=row.get("excluded_reason"),
        caption_of=row.get("caption_of"),
        cross_ref_targets=list(row.get("cross_ref_targets") or []),
    )


def _chunk_out(row: dict) -> ChunkOut:
    return ChunkOut(**{k: row[k] for k in ChunkOut.model_fields if k in row})


def _ingest_response(result) -> IngestResponse:
    return IngestResponse(
        file_id=result.file_id,
        doc_id=result.doc_id,
        parse_version=result.parse_version,
        num_blocks=result.num_blocks,
        num_chunks=result.num_chunks,
        tree_quality=result.tree_quality,
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@router.post("", response_model=IngestAcceptedResponse, status_code=202)
def ingest_document(
    req: IngestRequest,
    state: AppState = Depends(get_state),
):
    from pathlib import Path as _Path

    from ingestion.queue import IngestionJob
    from persistence.folder_service import (
        FolderNotFound,
        FolderService,
        unique_document_path,
    )

    # Validate file exists
    file_row = state.store.get_file(req.file_id)
    if not file_row:
        raise HTTPException(404, f"file {req.file_id} not found")

    doc_id = req.doc_id or f"doc_{req.file_id[:12]}"
    # Use the user's original filename here, NOT ``display_name`` (which
    # is the ``<stem>_<ts>_<rand>`` internal blob name) — leaks the
    # internal name into Document.filename / Document.path otherwise.
    # ``unique_document_path`` below appends ``" (1).pdf"``-style
    # suffixes for collisions, so we don't need the timestamp+rand
    # mangling for uniqueness.
    name = file_row.get("original_name") or file_row["display_name"]
    ext = _Path(name).suffix.lower().lstrip(".")
    fmt = {
        "pdf": "pdf",
        "docx": "docx",
        "doc": "docx",
        "pptx": "pptx",
        "ppt": "pptx",
        "xlsx": "xlsx",
        "xls": "xlsx",
        "txt": "text",
        "md": "text",
        "html": "html",
        "htm": "html",
    }.get(ext, ext or "unknown")

    # Create placeholder (if not force_reparse with existing doc)
    if not req.force_reparse:
        folder_path = req.folder_path or "/"
        with state.store.transaction() as sess:
            try:
                folder = FolderService(sess).require_by_path(folder_path)
            except FolderNotFound:
                raise HTTPException(404, f"folder not found: {folder_path!r}")
            doc_path = unique_document_path(sess, folder, name)
        state.store.create_document_placeholder(
            doc_id=doc_id,
            file_id=req.file_id,
            filename=_Path(doc_path).name,  # keep suffix from collision (e.g. 'foo (1).pdf')
            format=fmt,
            status="pending",
            folder_id=folder.folder_id,
            path=doc_path,
        )
    else:
        state.store.update_document_status(doc_id, status="pending")

    job = IngestionJob(
        file_id=req.file_id,
        doc_id=doc_id,
        parse_version=req.parse_version,
        enrich_summary=req.enrich_summary,
        force_reparse=req.force_reparse,
    )
    state.ingest_queue.submit(job)

    return IngestAcceptedResponse(
        file_id=req.file_id,
        doc_id=doc_id,
        status="pending",
        message="queued for processing",
    )


@router.post("/upload-and-ingest", response_model=IngestAcceptedResponse, status_code=202)
async def upload_and_ingest(
    file: UploadFile = File(...),
    original_name: str | None = Form(None),
    mime_type: str | None = Form(None),
    doc_id: str | None = Form(None),
    folder_path: str | None = Form(
        None,
        description="Destination folder, e.g. '/legal/2024'. Default = '/'.",
    ),
    state: AppState = Depends(get_state),
):
    """
    Upload a file and queue it for background ingestion. Returns 202 —
    the document appears in listings with status='pending' and transitions
    through processing → parsing → parsed → ready (or error).

    The new document lives under ``folder_path`` (defaults to root). If a
    sibling with the same name exists the filename is auto-suffixed
    (``foo.pdf`` → ``foo (1).pdf``).
    """
    from pathlib import Path as _Path

    from ingestion.queue import IngestionJob
    from persistence.folder_service import (
        FolderNotFound,
        FolderService,
        unique_document_path,
    )

    name = original_name or file.filename or "upload.bin"
    mime = mime_type or file.content_type
    data = await file.read()

    # Resolve destination folder BEFORE doing the upload so a bad path
    # returns 404 without orphaning a blob.
    target_folder_path = folder_path or "/"
    with state.store.transaction() as sess:
        try:
            folder = FolderService(sess).require_by_path(target_folder_path)
        except FolderNotFound:
            raise HTTPException(404, f"folder not found: {target_folder_path!r}")
        target_folder_id = folder.folder_id
        doc_path = unique_document_path(sess, folder, name)

    # Phase A: upload file (fast, synchronous)
    try:
        file_id = state.ingestion.upload(data, original_name=name, mime_type=mime)
    except ValueError as e:
        raise HTTPException(400, str(e))

    actual_doc_id = doc_id or f"doc_{file_id[:12]}"

    ext = _Path(name).suffix.lower().lstrip(".")
    fmt = {
        "pdf": "pdf",
        "docx": "docx",
        "doc": "docx",
        "pptx": "pptx",
        "ppt": "pptx",
        "xlsx": "xlsx",
        "xls": "xlsx",
        "txt": "text",
        "md": "text",
        "html": "html",
        "htm": "html",
    }.get(ext, ext or "unknown")

    state.store.create_document_placeholder(
        doc_id=actual_doc_id,
        file_id=file_id,
        filename=_Path(doc_path).name,  # preserve collision suffix
        format=fmt,
        status="pending",
        folder_id=target_folder_id,
        path=doc_path,
    )

    state.ingest_queue.submit(IngestionJob(file_id=file_id, doc_id=actual_doc_id))

    return IngestAcceptedResponse(
        file_id=file_id,
        doc_id=actual_doc_id,
        status="pending",
        message="queued for processing",
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedResponse)
def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None),
    status: str | None = Query(None),
    path_filter: str | None = Query(
        None,
        description=(
            "Filter by folder path. With recursive=true (default) matches the "
            "whole subtree under the path; with recursive=false only direct "
            "children of that folder. Same semantics as /api/v1/query's "
            "path_filter. Trashed docs are always excluded."
        ),
    ),
    recursive: bool = Query(
        True,
        description="When path_filter is set: true = subtree, false = direct children only.",
    ),
    state: AppState = Depends(get_state),
):
    folder_id: str | None = None
    path_prefix: str | None = None
    if path_filter is not None:
        if recursive:
            # Subtree prefix match — uses Document.path LIKE index
            path_prefix = path_filter
        else:
            # Direct-children only — resolve folder_id once (O(1) via path_lower index),
            # then filter by indexed folder_id column.
            from persistence.folder_service import FolderNotFound, FolderService

            with state.store.transaction() as sess:
                try:
                    folder_id = FolderService(sess).require_by_path(path_filter).folder_id
                except FolderNotFound:
                    raise HTTPException(404, f"folder not found: {path_filter!r}")

    rows = state.store.list_documents(
        limit=limit,
        offset=offset,
        search=search,
        status=status,
        folder_id=folder_id,
        path_prefix=path_prefix,
    )
    total = state.store.count_documents(
        status=status,
        search=search,
        folder_id=folder_id,
        path_prefix=path_prefix,
    )
    return PaginatedResponse(
        items=[_doc_out(r, state) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


class _DocLookupRequest(BaseModel):
    doc_ids: list[str]


@router.post("/lookup", response_model=list[DocumentOut])
def lookup_documents(
    body: _DocLookupRequest,
    state: AppState = Depends(get_state),
) -> list[DocumentOut]:
    """Batch-fetch documents by doc_id list. One SQL roundtrip.

    Used by the chat citation card (and similar UIs) so rendering a
    conversation's references doesn't fan out into N parallel
    GET /documents/{id} calls. Missing IDs are silently dropped from
    the response (caller already knows what it asked for).
    """
    # Cap to keep a single call from blowing up DB IO. Frontend caches
    # so this is sized for "all citations on one screen".
    ids = list(dict.fromkeys(body.doc_ids))[:200]  # dedupe + cap
    if not ids:
        return []
    rows = state.store.get_documents_by_ids(ids)
    return [_doc_out(r, state) for r in rows]


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: str, state: AppState = Depends(get_state)):
    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")
    return _doc_out(row, state)


@router.delete("/{doc_id}", status_code=204)
def delete_document(
    doc_id: str,
    hard: bool = Query(False, description="Skip trash and purge immediately"),
    state: AppState = Depends(get_state),
):
    """Soft-delete by default — moves the document into ``/__trash__``.

    - ``DELETE /documents/{id}``           → trash (recoverable, auto-purge after retention_days)
    - ``DELETE /documents/{id}?hard=true`` → permanent delete, equivalent to old behaviour

    The trash path stashes ``original_path`` in metadata; restore replays
    it via ``FolderService.ensure_path`` (Windows Recycle Bin semantics).
    Vector / KG / file blobs stay until permanent purge.
    """
    from persistence.trash_service import TrashService

    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")

    svc = TrashService(state)
    if hard:
        # Permanent delete: route through TrashService.purge so the
        # vector / KG / relational / file cleanup logic lives in one
        # place. Purge gates on ``_doc_in_trash`` (a deliberate guard
        # for the trash UI's per-item button), so a doc not yet in
        # trash has to be moved there first — otherwise the call is a
        # silent no-op and the route lies with 204. Idempotent: a doc
        # already in trash just falls through.
        if not row.get("path", "").startswith("/__trash__"):
            svc.move_document_to_trash(doc_id)
        svc.purge(doc_ids=[doc_id])
        return

    svc.move_document_to_trash(doc_id)


# ---------------------------------------------------------------------------
# Folder membership (move + bulk-move into another folder)
# ---------------------------------------------------------------------------


class MoveDocumentReq(BaseModel):
    to_path: str  # destination folder (e.g. '/legal/2024')


class BulkMoveReq(BaseModel):
    doc_ids: list[str]
    to_path: str


@router.patch("/{doc_id}/path")
def move_document(doc_id: str, body: MoveDocumentReq, state: AppState = Depends(get_state)):
    """Move a single document to another folder."""
    from persistence.folder_service import (
        FolderNotFound,
        FolderService,
        unique_document_path,
    )
    from persistence.scope import ScopeMode, ScopeService

    scope = ScopeService(state.store)
    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")

    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            target = svc.require_by_path(body.to_path)
        except FolderNotFound:
            raise HTTPException(404, f"target folder not found: {body.to_path!r}")
        scope.require_folder(target.folder_id, ScopeMode.WRITE)

        from persistence.models import Document

        doc = sess.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")

        # Filename stays the same; just compute new path with collision suffix
        filename = doc.filename or (doc.path.rsplit("/", 1)[-1] if doc.path else doc_id)
        new_path = unique_document_path(sess, target, filename)

        doc.folder_id = target.folder_id
        doc.path = new_path

        sess.add_all(
            [
                # audit log via sess (committed with the transaction)
            ]
        )
        from persistence.models import AuditLogRow

        sess.add(
            AuditLogRow(
                actor_id="local",
                action="document.move",
                target_type="document",
                target_id=doc_id,
                details={"to_path": new_path, "to_folder_id": target.folder_id},
            )
        )

    return {"doc_id": doc_id, "path": new_path, "folder_id": target.folder_id}


@router.post("/bulk-move")
def bulk_move_documents(body: BulkMoveReq, state: AppState = Depends(get_state)):
    """Move many documents at once."""
    from persistence.folder_service import (
        FolderNotFound,
        FolderService,
        unique_document_path,
    )
    from persistence.scope import ScopeMode, ScopeService

    scope = ScopeService(state.store)
    moved: list[dict] = []
    errors: list[dict] = []
    with state.store.transaction() as sess:
        svc = FolderService(sess)
        try:
            target = svc.require_by_path(body.to_path)
        except FolderNotFound:
            raise HTTPException(404, f"target folder not found: {body.to_path!r}")
        scope.require_folder(target.folder_id, ScopeMode.WRITE)

        from persistence.models import AuditLogRow, Document

        for doc_id in body.doc_ids:
            doc = sess.get(Document, doc_id)
            if doc is None:
                errors.append({"doc_id": doc_id, "error": "not found"})
                continue
            filename = doc.filename or (doc.path.rsplit("/", 1)[-1] if doc.path else doc_id)
            new_path = unique_document_path(sess, target, filename)
            doc.folder_id = target.folder_id
            doc.path = new_path
            moved.append({"doc_id": doc_id, "path": new_path})
            sess.add(
                AuditLogRow(
                    actor_id="local",
                    action="document.move",
                    target_type="document",
                    target_id=doc_id,
                    details={"to_path": new_path, "to_folder_id": target.folder_id, "bulk": True},
                )
            )

    return {"moved": moved, "errors": errors}


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


class RenameDocumentReq(BaseModel):
    new_filename: str


@router.patch("/{doc_id}/filename")
def rename_document(doc_id: str, body: RenameDocumentReq, state: AppState = Depends(get_state)):
    """Rename a document's user-facing filename in place.

    Updates ``Document.filename``, ``Document.path``, and the
    ``ChunkRow.path`` mirror in PG, then synchronously rewrites the
    same path in the vector store (so folder-scoped retrieval keeps
    matching) and the graph store (entity ``source_paths`` references).
    Returns 409 on collision with a sibling document.
    """
    from sqlalchemy import select, update

    from persistence.folder_service import (
        InvalidFolderName,
        normalize_name,
    )
    from persistence.models import AuditLogRow, ChunkRow, Document
    from persistence.scope import ScopeMode, ScopeService

    # Validate via the same primitive folder rename uses — the constraint
    # set (no slashes / control chars / reserved names, ≤255 chars) is
    # identical for filenames and folder names. Dots are allowed, so
    # ``foo.md`` passes through.
    try:
        new_filename = normalize_name(body.new_filename)
    except InvalidFolderName as e:
        raise HTTPException(422, str(e))

    scope = ScopeService(state.store)

    old_path: str | None = None
    new_path: str | None = None
    chunk_ids: list[str] = []
    folder_id: str | None = None

    with state.store.transaction() as sess:
        doc = sess.get(Document, doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        scope.require_folder(doc.folder_id, ScopeMode.WRITE)

        if doc.filename == new_filename:
            return {"doc_id": doc_id, "filename": doc.filename, "path": doc.path}

        # Reject sibling collisions explicitly. Auto-suffixing (``foo (1).pdf``)
        # would silently rewrite what the user typed — surprising for an
        # explicit rename, even though we accept it for move/upload.
        sibling = sess.execute(
            select(Document).where(
                Document.folder_id == doc.folder_id,
                Document.filename == new_filename,
                Document.doc_id != doc_id,
            )
        ).scalar_one_or_none()
        if sibling is not None:
            raise HTTPException(
                409,
                f"a document named {new_filename!r} already exists in this folder",
            )

        old_path = doc.path
        # Replace just the last segment of the path; parent stays put.
        parent = old_path.rsplit("/", 1)[0]
        new_path = (parent + "/" + new_filename) if parent else "/" + new_filename
        folder_id = doc.folder_id

        doc.filename = new_filename
        doc.path = new_path

        # Cascade the path change into the chunk table so PG-side
        # folder-scoped queries see the new path immediately.
        sess.execute(update(ChunkRow).where(ChunkRow.doc_id == doc_id).values(path=new_path))
        chunk_ids = [r.chunk_id for r in sess.execute(select(ChunkRow.chunk_id).where(ChunkRow.doc_id == doc_id)).all()]

        sess.add(
            AuditLogRow(
                actor_id="local",
                action="document.rename",
                target_type="document",
                target_id=doc_id,
                details={"old_path": old_path, "new_path": new_path},
            )
        )

    # Post-commit cross-store sync. Match the folder-rename pattern:
    # apply AFTER the PG transaction commits so a rolled-back rename
    # never leaks into Chroma / Neo4j.
    if chunk_ids and getattr(state, "vector", None) is not None:
        try:
            state.vector.update_paths({cid: new_path for cid in chunk_ids})
        except Exception as e:
            log.warning("vector update_paths failed for document.rename: %s", e)
    if getattr(state, "graph_store", None) is not None and hasattr(state.graph_store, "update_paths"):
        try:
            state.graph_store.update_paths(old_path, new_path)
        except Exception as e:
            log.warning("graph update_paths failed for document.rename: %s", e)

    return {"doc_id": doc_id, "filename": new_filename, "path": new_path, "folder_id": folder_id}


@router.post("/{doc_id}/reparse", response_model=IngestAcceptedResponse, status_code=202)
def reparse_document(
    doc_id: str,
    enrich_summary: bool | None = Query(None),
    state: AppState = Depends(get_state),
):
    from ingestion.queue import IngestionJob

    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")
    file_id = row.get("file_id")
    if not file_id:
        raise HTTPException(400, "document has no file_id, cannot reparse")

    # Reject if a reparse is already in flight. ``status`` is the
    # overall document state — anything other than ``ready`` /
    # ``error`` means the previous reparse hasn't finished (parsing,
    # structuring, embedding, etc). Returning 409 lets the frontend
    # leave its optimistic "Processing" state in place rather than
    # double-queuing the job.
    current_status = row.get("status")
    kg_status = row.get("kg_status")
    in_flight = current_status not in ("ready", "error", None) or (
        current_status == "ready" and kg_status not in ("done", "skipped", "disabled", None)
    )
    if in_flight:
        raise HTTPException(
            409,
            f"document is already being reparsed (status={current_status}, kg_status={kg_status})",
        )

    pv = row["active_parse_version"]

    # ── Clean up old associated data ──
    # 1. Vector store (need chunk_ids before relational delete)
    try:
        chunks = state.store.get_chunks(doc_id, pv)
        chunk_ids = [c["chunk_id"] for c in chunks]
        if chunk_ids:
            state.vector.delete_chunks(chunk_ids)
    except Exception:
        log.warning("reparse: vector cleanup failed for %s", doc_id)

    # 2. Knowledge Graph
    try:
        if state.graph_store is not None:
            state.graph_store.delete_by_doc(doc_id)
    except Exception:
        log.warning("reparse: KG cleanup failed for %s", doc_id)

    # 3. Relational data (blocks, chunks, tree) for current parse version
    try:
        state.store.delete_parse_version(doc_id, pv)
    except Exception:
        log.warning("reparse: relational cleanup failed for %s", doc_id)

    # 4. Reset all status fields to clean state
    state.store.update_document_status(
        doc_id,
        status="pending",
        embed_status="pending",
        embed_model=None,
        embed_at=None,
        enrich_status="pending",
        enrich_model=None,
        enrich_summary_count=0,
        enrich_image_count=0,
        enrich_at=None,
        enrich_started_at=None,
        parse_started_at=None,
        parse_completed_at=None,
        structure_started_at=None,
        structure_completed_at=None,
        embed_started_at=None,
        kg_status=None,
        kg_entity_count=None,
        kg_relation_count=None,
        kg_started_at=None,
        kg_completed_at=None,
        kg_model=None,
    )

    # 5. Refresh BM25 (old chunks removed)
    state.refresh_bm25()

    job = IngestionJob(
        file_id=file_id,
        doc_id=doc_id,
        enrich_summary=enrich_summary,
        force_reparse=True,
    )
    state.ingest_queue.submit(job)

    return IngestAcceptedResponse(
        file_id=file_id,
        doc_id=doc_id,
        status="pending",
        message="queued for reparse",
    )


@router.post("/{doc_id}/stop", status_code=200)
def stop_document(doc_id: str, state: AppState = Depends(get_state)):
    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")
    current = row.get("status")
    if current in ("ready", "error"):
        raise HTTPException(400, f"document is already {current}")
    # Best-effort cancellation of any queued/running ingestion job
    try:
        if hasattr(state, "ingest_queue") and hasattr(state.ingest_queue, "cancel"):
            state.ingest_queue.cancel(doc_id)
    except Exception:
        log.debug("ingest_queue.cancel not available or failed for %s", doc_id)
    state.store.update_document_status(doc_id, status="error")
    return {"doc_id": doc_id, "status": "error", "message": "stopped"}


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


@router.get("/{doc_id}/blocks", response_model=PaginatedResponse)
def list_blocks(
    doc_id: str,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    state: AppState = Depends(get_state),
):
    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")
    pv = row["active_parse_version"]
    blocks = state.store.get_blocks_paginated(doc_id, pv, limit=limit, offset=offset)
    total = state.store.count_blocks(doc_id, pv)
    return PaginatedResponse(
        items=[_block_out(b) for b in blocks],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------


@router.get("/{doc_id}/chunks", response_model=PaginatedResponse)
def list_chunks(
    doc_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    state: AppState = Depends(get_state),
):
    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")
    pv = row["active_parse_version"]
    chunks = state.store.get_chunks_paginated(doc_id, pv, limit=limit, offset=offset)
    total = state.store.count_chunks(doc_id, pv)
    return PaginatedResponse(
        items=[_chunk_out(c) for c in chunks],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


@router.get("/{doc_id}/tree", response_model=TreeOut)
def get_tree(doc_id: str, state: AppState = Depends(get_state)):
    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")
    pv = row["active_parse_version"]
    tree = state.store.load_tree(doc_id, pv)
    if not tree:
        raise HTTPException(404, "tree not found")
    nodes_out = {}
    for nid, n in tree.get("nodes", {}).items():
        nodes_out[nid] = TreeNodeOut(
            node_id=n.get("node_id", nid),
            parent_id=n.get("parent_id"),
            level=n.get("level", 0),
            title=n.get("title", ""),
            page_start=n.get("page_start", 0),
            page_end=n.get("page_end", 0),
            children=n.get("children", []),
            block_ids=n.get("block_ids", []),
            element_types=n.get("element_types", []),
            table_count=n.get("table_count", 0),
            image_count=n.get("image_count", 0),
            summary=n.get("summary"),
            key_entities=n.get("key_entities", []),
            role=n.get("role", "main"),
        )
    return TreeOut(
        doc_id=tree.get("doc_id", doc_id),
        parse_version=tree.get("parse_version", pv),
        root_id=tree.get("root_id", ""),
        quality_score=tree.get("quality_score", 0.0),
        generation_method=tree.get("generation_method", ""),
        nodes=nodes_out,
    )


@router.get("/{doc_id}/tree/{node_id}", response_model=TreeNodeOut)
def get_tree_node(doc_id: str, node_id: str, state: AppState = Depends(get_state)):
    row = state.store.get_document(doc_id)
    if not row:
        raise HTTPException(404, "document not found")
    tree = state.store.load_tree(doc_id, row["active_parse_version"])
    if not tree:
        raise HTTPException(404, "tree not found")
    n = tree.get("nodes", {}).get(node_id)
    if not n:
        raise HTTPException(404, "node not found")
    return TreeNodeOut(
        node_id=n.get("node_id", node_id),
        parent_id=n.get("parent_id"),
        level=n.get("level", 0),
        title=n.get("title", ""),
        page_start=n.get("page_start", 0),
        page_end=n.get("page_end", 0),
        children=n.get("children", []),
        block_ids=n.get("block_ids", []),
        element_types=n.get("element_types", []),
        table_count=n.get("table_count", 0),
        image_count=n.get("image_count", 0),
        summary=n.get("summary"),
        key_entities=n.get("key_entities", []),
        role=n.get("role", "main"),
    )
