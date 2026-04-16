"""
FileStore: content-hashed file ingestion.

Flow:
    1. User provides bytes or a local path + original filename + mime
    2. If bytes -> spill to a tempfile so we have a single code path
    3. Stream-hash the file (chunked, constant memory)
    4. Compute blob key from hash + extension (files/aa/bb/<hash>.ext)
    5. Check blob_store.exists(key); if not, blob_store.put_path(key, local_path, mime)
    6. Insert a files row (always, one per upload event)
    7. Clean up any tempfile

Each upload produces a fresh file_id (UUID). Multiple uploads of the
same content share the underlying blob but have distinct rows, so
metadata (original_name, uploaded_at) is preserved per-upload.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import mimetypes
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Union
from uuid import uuid4

from config import FilesConfig
from parser.blob_store import BlobStore, file_key

from .store import Store
from .url_fetcher import FetcherConfig, UrlFetcher

log = logging.getLogger(__name__)

# Source acceptance: either raw bytes or a path to an existing file
FileSource = Union[bytes, bytearray, str, Path]


class FileStore:
    def __init__(
        self,
        cfg: FilesConfig,
        blob_store: BlobStore,
        relational_store: Store,
        url_fetcher: UrlFetcher | None = None,
    ):
        self.cfg = cfg
        self.blob = blob_store
        self.rel = relational_store
        self.url_fetcher = url_fetcher or UrlFetcher(
            FetcherConfig(
                max_bytes=cfg.max_bytes,
                chunk_size=cfg.chunk_size,
            )
        )

    # ------------------------------------------------------------------
    def store(
        self,
        source: FileSource,
        *,
        original_name: str,
        mime_type: str | None = None,
    ) -> dict:
        """
        Persist a file and return its files-row as a dict.
        """
        mime = mime_type or _guess_mime(original_name)
        self._check_mime(mime)

        local_path, cleanup = _materialize_source(source)
        try:
            size = local_path.stat().st_size
            if self.cfg.max_bytes and size > self.cfg.max_bytes:
                raise ValueError(f"file too large: {size} bytes > max_bytes={self.cfg.max_bytes}")
            if size == 0:
                raise ValueError("empty file")

            content_hash = self._hash_file(local_path)
            ext = _pick_ext(original_name, mime)
            key = file_key(content_hash, ext, levels=self.cfg.hash_levels)

            # Dedup at the blob layer only; DB rows are one-per-upload.
            if not self.blob.exists(key):
                self.blob.put_path(key, str(local_path), mime)
                log.info("file blob written: key=%s size=%d", key, size)
            else:
                log.info("file blob dedup hit: key=%s", key)

            record = {
                "file_id": uuid4().hex,
                "content_hash": content_hash,
                "storage_key": key,
                "original_name": original_name,
                "display_name": _build_display_name(original_name, ext),
                "size_bytes": size,
                "mime_type": mime,
                "uploaded_at": datetime.utcnow(),
                "metadata_json": {},
            }
            self.rel.insert_file(record)
            return record
        finally:
            if cleanup:
                with contextlib.suppress(OSError):
                    local_path.unlink()

    # ------------------------------------------------------------------
    def store_from_url(
        self,
        url: str,
        *,
        original_name: str | None = None,
        mime_type: str | None = None,
    ) -> dict:
        """
        Download a file from a URL (http/https/s3/oss) into the
        FileStore. `original_name` / `mime_type` override whatever
        the fetcher auto-detects.
        """
        result = self.url_fetcher.fetch(url)
        try:
            return self.store(
                result.local_path,
                original_name=original_name or result.original_name,
                mime_type=mime_type or result.mime_type,
            )
        finally:
            with contextlib.suppress(OSError):
                result.local_path.unlink()

    # ------------------------------------------------------------------
    def url_for(self, file_id: str) -> str | None:
        row = self.rel.get_file(file_id)
        if not row:
            return None
        return self.blob.url_for(row["storage_key"])

    def materialize(self, file_id: str, local_path: Union[str, Path]) -> Path:
        """
        Download a file's blob to a local path for local-only tools
        (e.g. the parser backends expect a filesystem path).
        """
        row = self.rel.get_file(file_id)
        if not row:
            raise KeyError(f"file {file_id} not found")
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        self.blob.download_to(row["storage_key"], str(dst))
        return dst

    # ==================================================================
    # Internals
    # ==================================================================

    def _hash_file(self, path: Path) -> str:
        algo = self.cfg.hash_algorithm
        if algo == "blake3":
            try:
                import blake3  # type: ignore

                h = blake3.blake3()
            except ImportError as e:
                raise RuntimeError("hash_algorithm=blake3 requires the blake3 package") from e
        else:
            h = hashlib.sha256()

        chunk = self.cfg.chunk_size
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()

    def _check_mime(self, mime: str) -> None:
        allow = self.cfg.allowed_mime_prefixes
        if not allow:
            return
        for prefix in allow:
            if mime.startswith(prefix):
                return
        raise ValueError(f"mime type not allowed: {mime}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _materialize_source(source: FileSource) -> tuple[Path, bool]:
    """
    Return (local_path, needs_cleanup). If source is bytes we spill
    to a tempfile and cleanup=True; if it's a path we use it as-is.
    """
    if isinstance(source, bytes | bytearray):
        tmp_dir = Path("./storage/tmp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            prefix="qr_upload_",
            suffix=".bin",
            delete=False,
            dir=str(tmp_dir),
        )
        try:
            tmp.write(source)
            tmp.flush()
        finally:
            tmp.close()
        return Path(tmp.name), True
    if isinstance(source, str | Path):
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(p)
        return p, False
    raise TypeError(f"unsupported source type: {type(source)!r}")


def _guess_mime(original_name: str) -> str:
    guess, _ = mimetypes.guess_type(original_name)
    return guess or "application/octet-stream"


def _pick_ext(original_name: str, mime: str) -> str:
    """Pick a reasonable file extension: trust the filename first."""
    name_ext = Path(original_name).suffix.lstrip(".").lower()
    if name_ext:
        return name_ext
    guessed = mimetypes.guess_extension(mime) or ".bin"
    return guessed.lstrip(".").lower()


def _build_display_name(original_name: str, ext: str) -> str:
    """
    Produce `<stem>_<timestamp>_<random>.<ext>` for display/download.
    The stem is sanitized to be filesystem-friendly across platforms.
    """
    stem = Path(original_name).stem or "file"
    safe_stem = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in stem)[:80]
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rand = secrets.token_hex(4)
    return f"{safe_stem}_{ts}_{rand}.{ext}"
