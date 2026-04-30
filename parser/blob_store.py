"""
BlobStore: unified object storage for parser artifacts (figures,
cropped table images, optional page renders).

Used by:
    - parser backends   -> put() figures during parsing
    - citation resolver -> url_for() to expose them to the frontend

Switching deployment modes (local dev vs S3 vs OSS) is a config
change; no code in the parser or retrieval layers needs to know
which backend is active.

Keys
----
Keys are POSIX-style relative paths, e.g.
    "doc_abc123/v1/p0014/b_0007.png"
They are stable across storage backends so a doc parsed locally
can later be migrated to S3 by copying bytes under the same key.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class LocalStoreConfig:
    root: str  # filesystem root for figures
    public_base_url: str | None = None
    # When serving via a local HTTP endpoint (e.g. /static/figures/),
    # set public_base_url to that prefix. If None, url_for() returns
    # a file:// URL (dev-only).


@dataclass
class S3StoreConfig:
    endpoint: str
    bucket: str
    region: str
    access_key_env: str
    secret_key_env: str
    prefix: str = ""
    public_base_url: str | None = None  # CDN or bucket public URL


@dataclass
class OSSStoreConfig:
    endpoint: str
    bucket: str
    access_key_env: str
    secret_key_env: str
    prefix: str = ""
    public_base_url: str | None = None


@dataclass
class StorageConfig:
    mode: str  # "local" | "s3" | "oss"
    local: LocalStoreConfig | None = None
    s3: S3StoreConfig | None = None
    oss: OSSStoreConfig | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class BlobStore(Protocol):
    mode: str

    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes under key. Returns the public URL for the object."""
        ...

    def put_path(self, key: str, local_path: str, content_type: str) -> str:
        """
        Store a local file under key. Backends use their
        optimal path (shutil for local, multipart upload_file for
        S3, resumable for OSS) so large files don't blow memory.
        """
        ...

    def get(self, key: str) -> bytes: ...

    def download_to(self, key: str, local_path: str) -> None:
        """Stream the blob to a local path without loading it fully."""
        ...

    def exists(self, key: str) -> bool: ...

    def url_for(self, key: str) -> str:
        """Return a URL the frontend can fetch. May be public or signed."""
        ...


# ---------------------------------------------------------------------------
# Local implementation
# ---------------------------------------------------------------------------


class LocalBlobStore:
    mode = "local"

    def __init__(self, cfg: LocalStoreConfig):
        self._root = Path(cfg.root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._public_base_url = cfg.public_base_url.rstrip("/") if cfg.public_base_url else None
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        # Defend against path traversal: reject any absolute key or ".."
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"invalid blob key: {key!r}")
        return self._root / key

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file + rename
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return self.url_for(key)

    def put_path(self, key: str, local_path: str, content_type: str) -> str:
        import shutil

        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        with self._lock:
            shutil.copyfile(local_path, tmp)
            os.replace(tmp, dst)
        return self.url_for(key)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def download_to(self, key: str, local_path: str) -> None:
        import shutil

        src = self._path(key)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local_path)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def url_for(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}/{quote(key)}"
        # Dev fallback: file:// URL
        return self._path(key).as_uri()


# ---------------------------------------------------------------------------
# S3 implementation (lazy import boto3 so local dev doesn't need it)
# ---------------------------------------------------------------------------


class S3BlobStore:
    mode = "s3"

    def __init__(self, cfg: S3StoreConfig):
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError("S3BlobStore requires boto3. Install with: pip install boto3") from e

        access_key = os.environ.get(cfg.access_key_env)
        secret_key = os.environ.get(cfg.secret_key_env)
        if not access_key or not secret_key:
            raise RuntimeError(f"S3 credentials not set: {cfg.access_key_env} / {cfg.secret_key_env}")

        self._cfg = cfg
        self._prefix = cfg.prefix.strip("/")
        self._public_base_url = cfg.public_base_url.rstrip("/") if cfg.public_base_url else None
        self._client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint,
            region_name=cfg.region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(
            Bucket=self._cfg.bucket,
            Key=self._full_key(key),
            Body=data,
            ContentType=content_type,
        )
        return self.url_for(key)

    def put_path(self, key: str, local_path: str, content_type: str) -> str:
        # boto3's upload_file auto-multiparts large files.
        self._client.upload_file(
            Filename=local_path,
            Bucket=self._cfg.bucket,
            Key=self._full_key(key),
            ExtraArgs={"ContentType": content_type},
        )
        return self.url_for(key)

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._cfg.bucket, Key=self._full_key(key))
        return resp["Body"].read()

    def download_to(self, key: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(
            Bucket=self._cfg.bucket,
            Key=self._full_key(key),
            Filename=local_path,
        )

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # type: ignore

        try:
            self._client.head_object(Bucket=self._cfg.bucket, Key=self._full_key(key))
            return True
        except ClientError:
            return False

    def url_for(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}/{quote(key)}"
        # Presigned URL fallback (1 hour)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._cfg.bucket, "Key": self._full_key(key)},
            ExpiresIn=3600,
        )


# ---------------------------------------------------------------------------
# Aliyun OSS implementation
# ---------------------------------------------------------------------------


class OSSBlobStore:
    mode = "oss"

    def __init__(self, cfg: OSSStoreConfig):
        try:
            import oss2  # type: ignore
        except ImportError as e:
            raise RuntimeError("OSSBlobStore requires oss2. Install with: pip install oss2") from e

        access_key = os.environ.get(cfg.access_key_env)
        secret_key = os.environ.get(cfg.secret_key_env)
        if not access_key or not secret_key:
            raise RuntimeError(f"OSS credentials not set: {cfg.access_key_env} / {cfg.secret_key_env}")

        self._cfg = cfg
        self._prefix = cfg.prefix.strip("/")
        self._public_base_url = cfg.public_base_url.rstrip("/") if cfg.public_base_url else None
        auth = oss2.Auth(access_key, secret_key)
        self._bucket = oss2.Bucket(auth, cfg.endpoint, cfg.bucket)

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self._bucket.put_object(
            self._full_key(key),
            data,
            headers={"Content-Type": content_type},
        )
        return self.url_for(key)

    def put_path(self, key: str, local_path: str, content_type: str) -> str:
        # oss2 has a streaming upload that reads directly from disk.
        self._bucket.put_object_from_file(
            self._full_key(key),
            local_path,
            headers={"Content-Type": content_type},
        )
        return self.url_for(key)

    def get(self, key: str) -> bytes:
        return self._bucket.get_object(self._full_key(key)).read()

    def download_to(self, key: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self._bucket.get_object_to_file(self._full_key(key), local_path)

    def exists(self, key: str) -> bool:
        return self._bucket.object_exists(self._full_key(key))

    def url_for(self, key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}/{quote(key)}"
        return self._bucket.sign_url("GET", self._full_key(key), 3600)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_blob_store(cfg: StorageConfig) -> BlobStore:
    if cfg.mode == "local":
        if cfg.local is None:
            raise ValueError("storage.local config missing")
        return LocalBlobStore(cfg.local)
    if cfg.mode == "s3":
        if cfg.s3 is None:
            raise ValueError("storage.s3 config missing")
        return S3BlobStore(cfg.s3)
    if cfg.mode == "oss":
        if cfg.oss is None:
            raise ValueError("storage.oss config missing")
        return OSSBlobStore(cfg.oss)
    raise ValueError(f"unknown storage mode: {cfg.mode!r}")


# ---------------------------------------------------------------------------
# Key helpers -- keep key layout consistent across the codebase
# ---------------------------------------------------------------------------


def image_key(doc_id: str, parse_version: int, page_no: int, block_seq: int, ext: str = "png") -> str:
    """Canonical key for an image block's blob (was: ``figure_key``)."""
    return f"images/{doc_id}/v{parse_version}/p{page_no:04d}/b_{block_seq:04d}.{ext}"


def table_image_key(doc_id: str, parse_version: int, page_no: int, block_seq: int, ext: str = "png") -> str:
    """Canonical key for a rasterized table image (optional)."""
    return f"images/{doc_id}/v{parse_version}/p{page_no:04d}/t_{block_seq:04d}.{ext}"


def file_key(content_hash: str, ext: str, *, levels: int = 2) -> str:
    """
    Canonical key for a user-uploaded file, content-addressed with
    hash-prefix directory sharding.

    levels=2 -> files/aa/bb/<full_hash>.<ext>
    levels=3 -> files/aa/bb/cc/<full_hash>.<ext>
    """
    parts = ["files"]
    for i in range(levels):
        parts.append(content_hash[i * 2 : i * 2 + 2])
    parts.append(f"{content_hash}.{ext.lstrip('.')}")
    return "/".join(parts)
