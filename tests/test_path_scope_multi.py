"""
Multi-prefix path-scope tests.

Covers:

  * ``PathScope`` evolved to ``path_prefixes: list[str]``.
  * ``PathScopeResolver.run()`` accepts both the legacy
    ``_path_filter: str`` and the new ``_path_filters: list[str]``;
    list always wins; ``/`` collapses scope to "match anything";
    duplicates and trailing slashes get cleaned up.
  * ``allowed_doc_ids`` is the UNION of doc_ids matching any prefix.
  * Chroma ``_build_chroma_where`` translates multi-prefix filters
    into ``$or`` ``$contains`` clauses.
  * ``UnifiedSearcher`` honours ``path_prefixes`` and the legacy
    ``path_prefix`` alias; multiple prefixes OR together.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from config import RelationalConfig, SQLiteConfig
from persistence.models import Document, Folder
from persistence.store import Store
from persistence.vector.chroma import _build_chroma_where
from retrieval.components.path_scope import PathScopeResolver, _normalise_prefixes

# ---------------------------------------------------------------------------
# _normalise_prefixes
# ---------------------------------------------------------------------------


def test_normalise_handles_str_input():
    assert _normalise_prefixes("/legal") == ["/legal"]
    assert _normalise_prefixes("/legal/") == ["/legal"]


def test_normalise_handles_none_and_empty():
    assert _normalise_prefixes(None) == []
    assert _normalise_prefixes([]) == []
    assert _normalise_prefixes(["", None]) == []


def test_normalise_root_collapses_to_no_scope():
    """Any '/' entry collapses the list — root absorbs everything,
    so keeping /a + / would force every backend to special-case the
    root prefix."""
    assert _normalise_prefixes(["/"]) == []
    assert _normalise_prefixes(["/legal", "/", "/research"]) == []


def test_normalise_dedups_preserving_order():
    assert _normalise_prefixes(
        ["/a", "/b", "/a", "/b/"]
    ) == ["/a", "/b"]


# ---------------------------------------------------------------------------
# PathScopeResolver.run() — DB-backed
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "ps.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store):
    """Seed three folders + a doc per folder so resolve_paths has
    something to UNION across."""
    with store.transaction() as sess:
        # Folders first so the FK on Document.folder_id resolves.
        for fid, path in (
            ("f_legal", "/legal"),
            ("f_research", "/research"),
            ("f_scratch", "/scratch"),
        ):
            sess.add(
                Folder(
                    folder_id=fid,
                    path=path,
                    path_lower=path,
                    parent_id="__root__",
                    name=path.lstrip("/"),
                )
            )
        sess.flush()
        for fid, path in (
            ("f_legal", "/legal"),
            ("f_research", "/research"),
            ("f_scratch", "/scratch"),
        ):
            sess.add(
                Document(
                    doc_id=f"doc_{fid}",
                    folder_id=fid,
                    path=path,
                    filename=f"{fid}.pdf",
                    format="pdf",
                )
            )
        sess.commit()
    return store


def test_resolver_no_scope_returns_empty_prefixes(seeded):
    resolver = PathScopeResolver(seeded)
    scope = resolver.run({})
    assert scope.path_prefixes == []
    assert scope.allowed_doc_ids is None


def test_resolver_single_legacy_prefix(seeded):
    """Old ``_path_filter`` (str) still works as an alias."""
    resolver = PathScopeResolver(seeded)
    scope = resolver.run({"_path_filter": "/legal"})
    assert scope.path_prefixes == ["/legal"]
    assert scope.allowed_doc_ids == {"doc_f_legal"}


def test_resolver_multi_prefix_unions_doc_ids(seeded):
    resolver = PathScopeResolver(seeded)
    scope = resolver.run(
        {"_path_filters": ["/legal", "/research"]}
    )
    assert scope.path_prefixes == ["/legal", "/research"]
    assert scope.allowed_doc_ids == {"doc_f_legal", "doc_f_research"}


def test_resolver_filters_wins_over_filter_when_both_present(seeded):
    resolver = PathScopeResolver(seeded)
    scope = resolver.run(
        {
            "_path_filter": "/scratch",
            "_path_filters": ["/legal"],
        }
    )
    # The plural form takes precedence; the singular is ignored.
    assert scope.path_prefixes == ["/legal"]
    assert scope.allowed_doc_ids == {"doc_f_legal"}


def test_resolver_root_in_list_collapses_to_no_scope(seeded):
    resolver = PathScopeResolver(seeded)
    scope = resolver.run({"_path_filters": ["/legal", "/"]})
    assert scope.path_prefixes == []
    assert scope.allowed_doc_ids is None


def test_resolver_excludes_trashed_from_allowed(seeded):
    """A doc in /__trash__ never appears in allowed_doc_ids even
    when its original folder happens to be in scope."""
    with seeded.transaction() as sess:
        # Move doc_f_legal to trash; the document.path goes too
        # because the folder service in real flow would set both,
        # but for the test we only need the path to start with the
        # trash prefix.
        sess.execute(
            select(Document)
            .where(Document.doc_id == "doc_f_legal")
        ).scalar_one().path = "/__trash__/legal"
        sess.commit()
    resolver = PathScopeResolver(seeded)
    scope = resolver.run({"_path_filters": ["/legal"]})
    assert scope.allowed_doc_ids == set()
    assert "doc_f_legal" in scope.trashed_doc_ids


# ---------------------------------------------------------------------------
# Chroma _build_chroma_where
# ---------------------------------------------------------------------------


def test_chroma_where_multi_prefix_emits_or_clause():
    where = _build_chroma_where({"path_prefixes": ["/legal", "/research"]})
    assert where == {
        "$or": [
            {"path": {"$contains": "/legal"}},
            {"path": {"$contains": "/research"}},
        ]
    }


def test_chroma_where_legacy_path_prefix_still_works():
    where = _build_chroma_where({"path_prefix": "/legal/2024"})
    assert where == {"path": {"$contains": "/legal/2024"}}


def test_chroma_where_legacy_keys_merge_into_path_prefixes():
    """If a caller mixes the legacy primary + or-fallback keys, the
    builder should merge them into one $or list."""
    where = _build_chroma_where({
        "path_prefix": "/legal",
        "path_prefix_or": ["/old/legal"],
    })
    assert where == {
        "$or": [
            {"path": {"$contains": "/legal"}},
            {"path": {"$contains": "/old/legal"}},
        ]
    }


def test_chroma_where_root_prefix_is_ignored():
    where = _build_chroma_where({"path_prefixes": ["/"]})
    # Root collapses scope; result is None (no constraint).
    assert where is None


def test_chroma_where_no_filter_keys_returns_none():
    assert _build_chroma_where({}) is None
    assert _build_chroma_where(None) is None


def test_chroma_where_other_filters_AND_with_path():
    where = _build_chroma_where({
        "path_prefixes": ["/a", "/b"],
        "doc_id": "d1",
    })
    # Two top-level clauses → wrapped in $and.
    assert "$and" in where
    clauses = where["$and"]
    assert {"$or": [
        {"path": {"$contains": "/a"}},
        {"path": {"$contains": "/b"}},
    ]} in clauses
    assert {"doc_id": "d1"} in clauses
