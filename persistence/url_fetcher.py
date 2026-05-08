"""
URL fetcher used by FileStore.store_from_url.

Supports:
    https://...   http://...      stdlib urllib, streaming download
    s3://bucket/key                boto3 (optional dep)
    oss://bucket/key               oss2 (optional dep)

The fetcher always downloads to a local tempfile so the same
streaming-hash + BlobStore.put_path code path in FileStore handles
large objects regardless of the source.

Security:
    - max_bytes cap enforced during the download, not only after
    - Optional allowed_host whitelist for HTTP(S) sources
    - Server-supplied filename ('Content-Disposition') and
      Content-Type are surfaced back to the caller but never
      authoritative; the caller can override.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)


# RFC1918 + loopback + link-local + cloud metadata ranges. Resolved hostnames
# falling into any of these are rejected unless ``allow_private_hosts=True``
# (operator must opt in to fetch from internal services).
_PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # RFC1918
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local (incl. AWS/Azure/GCP metadata 169.254.169.254)
        "172.16.0.0/12",  # RFC1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.168.0.0/16",  # RFC1918
        "198.18.0.0/15",  # benchmarking
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved
        "::1/128",  # ipv6 loopback
        "fc00::/7",  # ipv6 unique-local
        "fe80::/10",  # ipv6 link-local
    )
]


def _is_private_address(host: str) -> bool:
    """True if *host* resolves to (or already is) a private/loopback/metadata address.

    Resolves all A/AAAA records — a hostname pointing partly at a public IP
    and partly at a private one is still rejected (DNS rebinding defence).
    """
    try:
        # Direct IP literal — no resolution needed
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_NETS)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Hostname doesn't resolve — let urlopen surface the real error
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if any(addr in net for net in _PRIVATE_NETS):
            return True
    return False


@dataclass
class FetchResult:
    local_path: Path
    original_name: str
    mime_type: str | None


@dataclass
class FetcherConfig:
    max_bytes: int = 500 * 1024 * 1024  # 500 MiB
    chunk_size: int = 1 << 20  # 1 MiB
    http_timeout: float = 60.0
    # Optional HTTP(S) host allowlist. Empty = no per-host restriction beyond
    # the SSRF check below.
    allowed_hosts: tuple[str, ...] = ()
    # SSRF defence: when False (default), HTTP(S) requests targeting private
    # / loopback / link-local / cloud-metadata addresses are rejected. Set
    # True only if the operator intentionally needs to reach internal hosts.
    allow_private_hosts: bool = False
    # Credentials for s3:// and oss:// are read from env, mirroring
    # the BlobStore convention (S3_ACCESS_KEY / OSS_ACCESS_KEY).
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_env: str = "S3_ACCESS_KEY"
    s3_secret_key_env: str = "S3_SECRET_KEY"
    oss_endpoint: str | None = None
    oss_access_key_env: str = "OSS_ACCESS_KEY"
    oss_secret_key_env: str = "OSS_SECRET_KEY"


# ---------------------------------------------------------------------------
# UrlFetcher
# ---------------------------------------------------------------------------


class UrlFetcher:
    def __init__(self, cfg: FetcherConfig | None = None):
        self.cfg = cfg or FetcherConfig()

    def fetch(self, url: str) -> FetchResult:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme in ("http", "https"):
            return self._fetch_http(url, parsed)
        if scheme == "s3":
            return self._fetch_s3(parsed)
        if scheme == "oss":
            return self._fetch_oss(parsed)
        raise ValueError(f"unsupported URL scheme: {scheme!r}")

    # ==================================================================
    # HTTP(S)
    # ==================================================================

    def _fetch_http(self, url: str, parsed) -> FetchResult:
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError("URL has no host")
        if self.cfg.allowed_hosts and host not in self.cfg.allowed_hosts:
            raise ValueError(f"host not allowed: {host}")
        if not self.cfg.allow_private_hosts and _is_private_address(host):
            raise ValueError(
                f"host {host!r} resolves to a private/loopback/metadata address; "
                "set FetcherConfig.allow_private_hosts=True to permit"
            )

        req = Request(url, headers={"User-Agent": "OpenCraig/1.0"})
        tmp = _tempfile(suffix=_suffix_from_path(parsed.path))
        bytes_written = 0
        try:
            with urlopen(req, timeout=self.cfg.http_timeout) as resp:
                mime = _http_content_type(resp)
                name = _http_filename(resp, parsed)
                with open(tmp, "wb") as f:
                    while True:
                        buf = resp.read(self.cfg.chunk_size)
                        if not buf:
                            break
                        bytes_written += len(buf)
                        if self.cfg.max_bytes and bytes_written > self.cfg.max_bytes:
                            raise ValueError(f"download exceeded max_bytes={self.cfg.max_bytes}")
                        f.write(buf)
        except Exception:
            _safe_unlink(tmp)
            raise
        log.info("fetched http url host=%s bytes=%d", host, bytes_written)
        return FetchResult(local_path=tmp, original_name=name, mime_type=mime)

    # ==================================================================
    # S3
    # ==================================================================

    def _fetch_s3(self, parsed) -> FetchResult:
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError("s3:// URLs require boto3") from e

        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise ValueError(f"malformed s3 URL: s3://{bucket}/{key}")

        access = os.environ.get(self.cfg.s3_access_key_env)
        secret = os.environ.get(self.cfg.s3_secret_key_env)
        if not access or not secret:
            raise RuntimeError(f"S3 credentials not set: {self.cfg.s3_access_key_env} / {self.cfg.s3_secret_key_env}")
        client = boto3.client(
            "s3",
            endpoint_url=self.cfg.s3_endpoint,
            region_name=self.cfg.s3_region,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
        )

        head = client.head_object(Bucket=bucket, Key=key)
        size = head.get("ContentLength", 0)
        if self.cfg.max_bytes and size > self.cfg.max_bytes:
            raise ValueError(f"s3 object too large: {size}")

        tmp = _tempfile(suffix=_suffix_from_path(key))
        try:
            client.download_file(Bucket=bucket, Key=key, Filename=str(tmp))
        except Exception:
            _safe_unlink(tmp)
            raise

        mime = head.get("ContentType")
        name = Path(key).name or "download.bin"
        log.info("fetched s3 object bucket=%s key=%s bytes=%d", bucket, key, size)
        return FetchResult(local_path=tmp, original_name=name, mime_type=mime)

    # ==================================================================
    # OSS
    # ==================================================================

    def _fetch_oss(self, parsed) -> FetchResult:
        try:
            import oss2  # type: ignore
        except ImportError as e:
            raise RuntimeError("oss:// URLs require oss2") from e

        bucket_name = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket_name or not key:
            raise ValueError(f"malformed oss URL: oss://{bucket_name}/{key}")
        if not self.cfg.oss_endpoint:
            raise RuntimeError("FetcherConfig.oss_endpoint is not set")

        access = os.environ.get(self.cfg.oss_access_key_env)
        secret = os.environ.get(self.cfg.oss_secret_key_env)
        if not access or not secret:
            raise RuntimeError(
                f"OSS credentials not set: {self.cfg.oss_access_key_env} / {self.cfg.oss_secret_key_env}"
            )

        auth = oss2.Auth(access, secret)
        bucket = oss2.Bucket(auth, self.cfg.oss_endpoint, bucket_name)

        meta = bucket.head_object(key)
        size = int(meta.content_length or 0)
        if self.cfg.max_bytes and size > self.cfg.max_bytes:
            raise ValueError(f"oss object too large: {size}")

        tmp = _tempfile(suffix=_suffix_from_path(key))
        try:
            bucket.get_object_to_file(key, str(tmp))
        except Exception:
            _safe_unlink(tmp)
            raise

        mime = meta.content_type
        name = Path(key).name or "download.bin"
        log.info("fetched oss object bucket=%s key=%s bytes=%d", bucket_name, key, size)
        return FetchResult(local_path=tmp, original_name=name, mime_type=mime)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tempfile(suffix: str = "") -> Path:
    tmp_dir = Path("./storage/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="fr_url_", suffix=suffix, dir=str(tmp_dir))
    os.close(fd)
    return Path(name)


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _suffix_from_path(p: str) -> str:
    s = Path(p).suffix
    return s if s else ""


def _http_content_type(resp) -> str | None:
    ct = resp.headers.get("Content-Type")
    if not ct:
        return None
    return ct.split(";", 1)[0].strip() or None


_CD_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-\d\'\')?"?([^";]+)"?', re.I)


def _http_filename(resp, parsed) -> str:
    cd = resp.headers.get("Content-Disposition") or ""
    m = _CD_FILENAME_RE.search(cd)
    if m:
        return m.group(1).strip()
    name = Path(parsed.path).name
    return name or "download.bin"
