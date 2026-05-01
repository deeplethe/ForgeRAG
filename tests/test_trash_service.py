"""Unit tests for TrashService — document soft-delete + Windows-style restore."""

from __future__ import annotations

import pytest

from config.persistence import RelationalConfig, SQLiteConfig
from persistence.folder_service import (
    ROOT_FOLDER_ID,
    TRASH_FOLDER_ID,
    TRASH_PATH,
    FolderService,
)
from persistence.models import Document
from persistence.store import Store
from persistence.trash_service import TrashService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeState:
    """Minimal AppState stand-in — TrashService uses ``store``, optional
    ``vector`` / ``graph_store`` / ``refresh_bm25``. We supply ``store`` only;
    the missing attributes exercise the defensive-cleanup branches."""

    def __init__(self, store: Store):
        self.store = store


@pytest.fixture
def store(tmp_path):
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "trash_test.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema()
    yield s
    s.close()


@pytest.fixture
def state(store):
    return _FakeState(store)


def _add_doc(store: Store, *, doc_id: str, folder_id: str, path: str, filename: str) -> None:
    with store.transaction() as sess:
        sess.add(
            Document(
                doc_id=doc_id,
                folder_id=folder_id,
                path=path,
                filename=filename,
                format="pdf",
            )
        )


# ---------------------------------------------------------------------------
# move_document_to_trash
# ---------------------------------------------------------------------------


class TestMoveDocumentToTrash:
    def test_moves_doc_into_trash_folder(self, state, store):
        with store.transaction() as sess:
            FolderService(sess).create("/", "projects")
        _add_doc(store, doc_id="d1", folder_id=ROOT_FOLDER_ID, path="/projects/foo.pdf", filename="foo.pdf")
        # Tie the doc to the right folder via folder_id
        with store.transaction() as sess:
            projects = FolderService(sess).require_by_path("/projects")
            sess.get(Document, "d1").folder_id = projects.folder_id

        result = TrashService(state).move_document_to_trash("d1")
        assert result["path"].startswith(TRASH_PATH + "/")

        with store.transaction() as sess:
            doc = sess.get(Document, "d1")
            assert doc.folder_id == TRASH_FOLDER_ID
            assert doc.path.startswith(TRASH_PATH + "/")
            assert doc.trashed_metadata["original_path"] == "/projects/foo.pdf"
            assert "trashed_at" in doc.trashed_metadata

    def test_idempotent_when_already_trashed(self, state, store):
        _add_doc(state.store, doc_id="d1", folder_id=ROOT_FOLDER_ID, path="/foo.pdf", filename="foo.pdf")
        first = TrashService(state).move_document_to_trash("d1")
        second = TrashService(state).move_document_to_trash("d1")
        assert second.get("already_trashed") is True
        assert second["path"] == first["path"]

    def test_missing_doc_returns_error(self, state):
        result = TrashService(state).move_document_to_trash("nope")
        assert result.get("error") == "not found"

    def test_collision_in_trash_auto_suffixed(self, state, store):
        # Two docs with identical filename trashed in quick succession
        # share the timestamp prefix; ``unique_document_path`` resolves
        # the collision with `(1)`, `(2)`, etc.
        _add_doc(state.store, doc_id="a", folder_id=ROOT_FOLDER_ID, path="/foo.pdf", filename="foo.pdf")
        _add_doc(state.store, doc_id="b", folder_id=ROOT_FOLDER_ID, path="/foo.pdf", filename="foo.pdf")
        path_a = TrashService(state).move_document_to_trash("a")["path"]
        path_b = TrashService(state).move_document_to_trash("b")["path"]
        assert path_a != path_b


# ---------------------------------------------------------------------------
# restore — Windows Recycle Bin semantics (path + auto-recreate)
# ---------------------------------------------------------------------------


class TestRestoreDocument:
    def test_restores_to_existing_parent(self, state, store):
        with store.transaction() as sess:
            FolderService(sess).create("/", "projects")
        with store.transaction() as sess:
            projects = FolderService(sess).require_by_path("/projects")
            sess.add(
                Document(
                    doc_id="d1",
                    folder_id=projects.folder_id,
                    path="/projects/foo.pdf",
                    filename="foo.pdf",
                    format="pdf",
                )
            )

        TrashService(state).move_document_to_trash("d1")
        result = TrashService(state).restore(doc_ids=["d1"])

        assert result["restored"][0]["path"] == "/projects/foo.pdf"
        with store.transaction() as sess:
            doc = sess.get(Document, "d1")
            assert doc.path == "/projects/foo.pdf"
            assert doc.filename == "foo.pdf"
            assert doc.trashed_metadata is None
            # folder_id points at the surviving /projects folder
            projects = FolderService(sess).require_by_path("/projects")
            assert doc.folder_id == projects.folder_id

    def test_recreates_missing_parent_chain(self, state, store):
        """Windows-style: if the parent folder was permanently deleted,
        restore auto-creates the missing chain."""
        with store.transaction() as sess:
            FolderService(sess).create("/", "projects")
            FolderService(sess).create("/projects", "deep")
        with store.transaction() as sess:
            deep = FolderService(sess).require_by_path("/projects/deep")
            sess.add(
                Document(
                    doc_id="d1",
                    folder_id=deep.folder_id,
                    path="/projects/deep/foo.pdf",
                    filename="foo.pdf",
                    format="pdf",
                )
            )

        TrashService(state).move_document_to_trash("d1")

        # Permanently delete the parent chain (simulate user emptying
        # those folders out of trash later, or hard-deleting them).
        # Two separate transactions so SQLAlchemy doesn't batch the
        # deletes and trip the parent-before-child FK constraint.
        with store.transaction() as sess:
            sess.delete(FolderService(sess).require_by_path("/projects/deep"))
        with store.transaction() as sess:
            sess.delete(FolderService(sess).require_by_path("/projects"))

        result = TrashService(state).restore(doc_ids=["d1"])

        assert result["restored"][0]["path"] == "/projects/deep/foo.pdf"
        with store.transaction() as sess:
            assert FolderService(sess).get_by_path("/projects") is not None
            assert FolderService(sess).get_by_path("/projects/deep") is not None

    def test_filename_collision_appends_marker(self, state, store):
        """Restoring foo.pdf when foo.pdf already exists at target path:
        unique_document_path appends ``(1)``."""
        _add_doc(state.store, doc_id="d1", folder_id=ROOT_FOLDER_ID, path="/foo.pdf", filename="foo.pdf")
        TrashService(state).move_document_to_trash("d1")

        # A new doc with the same filename appeared at the original path
        # while ``d1`` was in trash.
        _add_doc(state.store, doc_id="d2", folder_id=ROOT_FOLDER_ID, path="/foo.pdf", filename="foo.pdf")

        result = TrashService(state).restore(doc_ids=["d1"])
        restored_path = result["restored"][0]["path"]
        assert restored_path != "/foo.pdf"
        assert restored_path.startswith("/foo")
        assert "1" in restored_path  # something like "/foo (1).pdf"

    def test_not_in_trash_errors(self, state, store):
        _add_doc(state.store, doc_id="d1", folder_id=ROOT_FOLDER_ID, path="/foo.pdf", filename="foo.pdf")
        result = TrashService(state).restore(doc_ids=["d1"])
        assert result["restored"] == []
        assert result["errors"][0]["error"] == "not in trash"
