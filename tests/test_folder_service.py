"""Unit tests for FolderService — the core of Phase 1."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

os.environ["TESTING_ALLOW_SQLITE"] = "1"

from config.persistence import RelationalConfig, SQLiteConfig
from persistence.folder_service import (
    ROOT_FOLDER_ID,
    TRASH_FOLDER_ID,
    TRASH_PATH,
    FolderAlreadyExists,
    FolderError,
    FolderIsSystemProtected,
    FolderNotFound,
    FolderService,
    InvalidFolderName,
    depth_of,
    is_under,
    join_path,
    normalize_name,
    parent_of,
    unique_document_path,
)
from persistence.models import Document, Folder
from persistence.store import Store


# ---------------------------------------------------------------------------
# Helpers for path math (pure — no DB)
# ---------------------------------------------------------------------------


class TestPathHelpers:
    def test_normalize_name_trims_whitespace(self):
        assert normalize_name("  foo  ") == "foo"

    def test_normalize_name_rejects_empty(self):
        with pytest.raises(InvalidFolderName):
            normalize_name("")

    def test_normalize_name_rejects_slash(self):
        with pytest.raises(InvalidFolderName):
            normalize_name("a/b")

    def test_normalize_name_rejects_dot_dotdot(self):
        for bad in (".", ".."):
            with pytest.raises(InvalidFolderName):
                normalize_name(bad)

    def test_normalize_name_rejects_too_long(self):
        with pytest.raises(InvalidFolderName):
            normalize_name("X" * 300)

    def test_normalize_name_rejects_forbidden(self):
        for bad in ("foo?", "bar*", "<boom>", 'a"b', "a|b", "a:b"):
            with pytest.raises(InvalidFolderName):
                normalize_name(bad)

    def test_join_path_root(self):
        assert join_path("/", "a") == "/a"

    def test_join_path_nested(self):
        assert join_path("/a", "b") == "/a/b"
        assert join_path("/a/", "b") == "/a/b"

    def test_parent_of_nested(self):
        assert parent_of("/a/b/c") == "/a/b"

    def test_parent_of_top_level(self):
        assert parent_of("/a") == "/"

    def test_parent_of_root(self):
        assert parent_of("/") == "/"

    def test_is_under_self(self):
        assert is_under("/a", "/a") is True

    def test_is_under_descendant(self):
        assert is_under("/a/b/c", "/a") is True

    def test_is_under_sibling(self):
        assert is_under("/b", "/a") is False

    def test_is_under_root(self):
        assert is_under("/anything", "/") is True

    def test_depth(self):
        assert depth_of("/") == 0
        assert depth_of("/a") == 1
        assert depth_of("/a/b") == 2


# ---------------------------------------------------------------------------
# Integration tests with an in-memory SQLite store
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """SQLite store with ensure_schema so __root__ and __trash__ exist."""
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "fs_test.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema()
    yield s
    s.close()


class TestFolderService:
    def test_system_folders_exist(self, store):
        with store.transaction() as sess:
            assert sess.get(Folder, ROOT_FOLDER_ID) is not None
            assert sess.get(Folder, TRASH_FOLDER_ID) is not None

    def test_create_top_level(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            f = svc.create("/", "legal")
            assert f.path == "/legal"
            assert f.parent_id == ROOT_FOLDER_ID
            assert f.is_system is False

    def test_create_nested(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            svc.create("/", "legal")
        with store.transaction() as sess:
            svc = FolderService(sess)
            f = svc.create("/legal", "2024")
            assert f.path == "/legal/2024"

    def test_create_rejects_duplicate(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            svc.create("/", "legal")
        with store.transaction() as sess:
            svc = FolderService(sess)
            with pytest.raises(FolderAlreadyExists):
                svc.create("/", "legal")

    def test_create_rejects_missing_parent(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            with pytest.raises(FolderNotFound):
                svc.create("/does/not/exist", "child")

    def test_rename_updates_own_and_descendants(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            svc.create("/", "legal")
            svc.create("/legal", "2024")
            svc.create("/legal/2024", "docs")
        with store.transaction() as sess:
            svc = FolderService(sess)
            legal = svc.require_by_path("/legal")
            svc.rename(legal.folder_id, "law")
        with store.transaction() as sess:
            svc = FolderService(sess)
            assert svc.get_by_path("/legal") is None
            assert svc.get_by_path("/law") is not None
            assert svc.get_by_path("/law/2024") is not None
            assert svc.get_by_path("/law/2024/docs") is not None

    def test_rename_system_folder_rejected(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            with pytest.raises(FolderIsSystemProtected):
                svc.rename(ROOT_FOLDER_ID, "newroot")
            with pytest.raises(FolderIsSystemProtected):
                svc.rename(TRASH_FOLDER_ID, "Bin")

    def test_move_folder(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            svc.create("/", "legal")
            svc.create("/", "finance")
            svc.create("/legal", "2024")
        with store.transaction() as sess:
            svc = FolderService(sess)
            yr = svc.require_by_path("/legal/2024")
            svc.move(yr.folder_id, "/finance")
        with store.transaction() as sess:
            svc = FolderService(sess)
            assert svc.get_by_path("/legal/2024") is None
            assert svc.get_by_path("/finance/2024") is not None

    def test_move_into_own_subtree_rejected(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            svc.create("/", "a")
            svc.create("/a", "b")
        with store.transaction() as sess:
            svc = FolderService(sess)
            a = svc.require_by_path("/a")
            with pytest.raises(FolderError):
                svc.move(a.folder_id, "/a/b")

    def test_move_to_trash_updates_subtree(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            svc.create("/", "legal")
            svc.create("/legal", "2024")
        with store.transaction() as sess:
            svc = FolderService(sess)
            f = svc.require_by_path("/legal")
            trashed = svc.move_to_trash(f.folder_id)
            # Under /__trash__/<timestamp>_legal
            assert trashed.path.startswith(TRASH_PATH + "/")
            assert trashed.trashed_metadata is not None
            assert trashed.trashed_metadata["original_path"] == "/legal"
        with store.transaction() as sess:
            svc = FolderService(sess)
            # descendants also moved
            assert svc.get_by_path("/legal/2024") is None
            # find the trashed subtree folder
            all_in_trash = [
                x for x in sess.execute(select(Folder)).scalars()
                if x.path.startswith(TRASH_PATH + "/")
            ]
            # Expect at least 2 (legal + 2024 under it)
            assert len(all_in_trash) >= 2

    def test_ensure_path_idempotent(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            svc.ensure_path("/a/b/c")
            svc.ensure_path("/a/b/c")  # should not raise
        with store.transaction() as sess:
            svc = FolderService(sess)
            assert svc.get_by_path("/a/b/c") is not None

    def test_list_children_order(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            for n in ["c", "a", "b"]:
                svc.create("/", n)
        with store.transaction() as sess:
            svc = FolderService(sess)
            kids = svc.list_children(ROOT_FOLDER_ID)
            names = [k.name for k in kids if not k.is_system]
            assert names == sorted(names)

    def test_unique_document_path_no_collision(self, store):
        with store.transaction() as sess:
            svc = FolderService(sess)
            folder = svc.get_by_id(ROOT_FOLDER_ID)
            path = unique_document_path(sess, folder, "foo.pdf")
            assert path == "/foo.pdf"

    def test_unique_document_path_auto_suffix(self, store):
        """Insert a 'foo.pdf' document and verify the next call appends (1)."""
        with store.transaction() as sess:
            sess.add(Document(
                doc_id="d1", folder_id=ROOT_FOLDER_ID, path="/foo.pdf",
                filename="foo.pdf", format="pdf",
            ))
        with store.transaction() as sess:
            svc = FolderService(sess)
            folder = svc.get_by_id(ROOT_FOLDER_ID)
            assert unique_document_path(sess, folder, "foo.pdf") == "/foo (1).pdf"

    def test_case_insensitive_collision(self, store):
        """'/Legal' and '/legal' must be considered duplicates."""
        with store.transaction() as sess:
            svc = FolderService(sess)
            svc.create("/", "Legal")
        with store.transaction() as sess:
            svc = FolderService(sess)
            # path_lower lookup — we can't create a case-variant duplicate
            assert svc.get_by_path("/legal") is not None
            assert svc.get_by_path("/Legal") is not None
