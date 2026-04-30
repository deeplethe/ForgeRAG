"""Tests for parser.blob_store.LocalBlobStore."""

from __future__ import annotations

import pytest

from parser.blob_store import (
    LocalBlobStore,
    LocalStoreConfig,
    StorageConfig,
    image_key,
    make_blob_store,
)


@pytest.fixture
def local_store(tmp_path) -> LocalBlobStore:
    cfg = LocalStoreConfig(
        root=str(tmp_path / "figs"),
        public_base_url="https://cdn.test/figs",
    )
    return LocalBlobStore(cfg)


class TestLocalBlobStore:
    def test_put_then_get_roundtrip(self, local_store):
        key = "doc_a/v1/p0001/b_0001.png"
        url = local_store.put(key, b"\x89PNG\r\n\x1a\n", "image/png")
        assert url.startswith("https://cdn.test/figs/")
        assert local_store.exists(key)
        assert local_store.get(key) == b"\x89PNG\r\n\x1a\n"

    def test_url_for_uses_public_base_url(self, local_store):
        url = local_store.url_for("a/b/c.png")
        assert url == "https://cdn.test/figs/a/b/c.png"

    def test_url_for_falls_back_to_file_uri(self, tmp_path):
        store = LocalBlobStore(LocalStoreConfig(root=str(tmp_path)))
        url = store.url_for("x.png")
        assert url.startswith("file://")

    def test_put_creates_parent_dirs(self, local_store):
        key = "deep/nested/path/to/image.jpg"
        local_store.put(key, b"abc", "image/jpeg")
        assert local_store.exists(key)

    def test_put_atomic_overwrite(self, local_store):
        key = "overwrite.bin"
        local_store.put(key, b"v1", "application/octet-stream")
        local_store.put(key, b"v2", "application/octet-stream")
        assert local_store.get(key) == b"v2"

    def test_rejects_absolute_key(self, local_store):
        with pytest.raises(ValueError):
            local_store.put("/etc/passwd", b"x", "text/plain")

    def test_rejects_path_traversal(self, local_store):
        with pytest.raises(ValueError):
            local_store.put("../escape.txt", b"x", "text/plain")
        with pytest.raises(ValueError):
            local_store.put("a/../../escape.txt", b"x", "text/plain")

    def test_exists_false_for_missing(self, local_store):
        assert not local_store.exists("nope.png")


class TestFactory:
    def test_make_local(self, tmp_path):
        cfg = StorageConfig(mode="local", local=LocalStoreConfig(root=str(tmp_path)))
        store = make_blob_store(cfg)
        assert store.mode == "local"

    def test_make_unknown_mode_raises(self):
        cfg = StorageConfig(mode="ftp")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            make_blob_store(cfg)

    def test_make_s3_missing_section_raises(self):
        cfg = StorageConfig(mode="s3", s3=None)
        with pytest.raises(ValueError):
            make_blob_store(cfg)


class TestKeyHelpers:
    def test_image_key_format(self):
        k = image_key("doc_abc", 1, 14, 7)
        assert k == "images/doc_abc/v1/p0014/b_0007.png"

    def test_image_key_custom_ext(self):
        k = image_key("doc_abc", 2, 3, 1, ext="jpg")
        assert k == "images/doc_abc/v2/p0003/b_0001.jpg"
