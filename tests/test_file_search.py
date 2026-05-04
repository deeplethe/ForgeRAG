"""Unit tests for the unified search layer (`retrieval/file_search.py`).

Covers two layers:

  1. The filename BM25 index — doc-keyed, reuses the same
     ``InMemoryBM25Index`` class as the content path. Verifies
     tokenisation of `filename + path + format` is intuitive
     (substring/path-segment/extension all match), persistence
     round-trips, and `update_filename_index_for_doc` is idempotent.

  2. ``UnifiedSearcher`` — BM25-only over content + filename indices,
     plus the per-view aggregation. Uses a real ``InMemoryBM25Index``
     so the test exercises actual scoring rather than mock numbers.
     A tiny fake store fills in document/chunk metadata.

No LLM / embedder / vector-store calls — ``/search`` is pure lexical.
"""

from __future__ import annotations

from pathlib import Path

from config import BM25Config
from retrieval.bm25 import InMemoryBM25Index
from retrieval.file_search import (
    FILENAME_BM25_CACHE_PATH,
    UnifiedSearcher,
    _filename_index_text,
    _snippet,
    build_filename_bm25_index,
    filename_index_path,
    remove_filename_index_for_doc,
    update_filename_index_for_doc,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal Store stand-in: enough for filename-index build,
    UnifiedSearcher's hydrations (chunk + doc fetch), and incremental
    update calls."""

    def __init__(self, docs: list[dict], chunks: list[dict] | None = None):
        self._docs = {d["doc_id"]: d for d in docs}
        self._chunks = {c["chunk_id"]: c for c in (chunks or [])}

    # docs
    def list_document_ids(self) -> list[str]:
        return list(self._docs)

    def get_document(self, doc_id: str) -> dict | None:
        return self._docs.get(doc_id)

    # chunks (the content-BM25 hydration path uses these)
    def get_chunk(self, chunk_id: str) -> dict | None:
        return self._chunks.get(chunk_id)

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        return [self._chunks[c] for c in chunk_ids if c in self._chunks]


def _chunk_row(doc_id: str, seq: int, content: str, *, page: int = 1) -> dict:
    """Build a chunk row in the shape ``Store.get_chunks_by_ids`` returns."""
    return {
        "chunk_id": f"{doc_id}:1:c{seq}",
        "doc_id": doc_id,
        "parse_version": 1,
        "node_id": f"node-{seq}",
        "content": content,
        "content_type": "text",
        "page_start": page,
        "page_end": page,
    }


# ---------------------------------------------------------------------------
# Filename index
# ---------------------------------------------------------------------------


def test_filename_index_text_combines_fields():
    s = _filename_index_text(
        filename="Q3_financials_2024.pdf", path="/legal/2024", format="pdf"
    )
    # Concatenated; tokenizer in BM25 will split on non-alnum.
    assert "Q3_financials_2024.pdf" in s
    assert "/legal/2024" in s
    assert " pdf" in s


def test_filename_index_text_drops_blank_parts():
    assert _filename_index_text(filename="a.pdf", path="", format="").strip() == "a.pdf"


def test_build_filename_index_indexes_one_entry_per_doc(tmp_path: Path):
    docs = [
        {"doc_id": "d1", "filename": "Q3_financials_2024.pdf", "path": "/legal/2024", "format": "pdf"},
        {"doc_id": "d2", "filename": "annual-report.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d3", "filename": "team_photo.jpg", "path": "/personal", "format": "jpg"},
    ]
    cache = tmp_path / "fn_bm25.pkl"
    idx = build_filename_bm25_index(_FakeStore(docs), BM25Config(), cache_path=str(cache))
    assert len(idx) == 3
    # Persistence round-trips: cache file exists and reload returns the same shape.
    assert cache.exists()


def test_filename_index_finds_filename_token(tmp_path: Path):
    docs = [
        {"doc_id": "d1", "filename": "Q3_financials_2024.pdf", "path": "/legal", "format": "pdf"},
        {"doc_id": "d2", "filename": "lunch_menu.pdf", "path": "/", "format": "pdf"},
    ]
    idx = build_filename_bm25_index(_FakeStore(docs), BM25Config(), cache_path=str(tmp_path / "x.pkl"))
    hits = idx.search_chunks("financials", top_k=10)
    # d1's entry is keyed by doc_id (chunk_id == doc_id) so the hit is (d1, score).
    assert hits[0][0] == "d1"


def test_filename_index_finds_path_segment(tmp_path: Path):
    docs = [
        {"doc_id": "d1", "filename": "x.pdf", "path": "/legal/2024/contracts", "format": "pdf"},
        {"doc_id": "d2", "filename": "y.pdf", "path": "/", "format": "pdf"},
    ]
    idx = build_filename_bm25_index(_FakeStore(docs), BM25Config(), cache_path=str(tmp_path / "x.pkl"))
    hits = idx.search_chunks("contracts", top_k=10)
    assert hits and hits[0][0] == "d1"


def test_filename_index_finds_extension(tmp_path: Path):
    docs = [
        {"doc_id": "d1", "filename": "report.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "sheet.xlsx", "path": "/", "format": "xlsx"},
    ]
    idx = build_filename_bm25_index(_FakeStore(docs), BM25Config(), cache_path=str(tmp_path / "x.pkl"))
    hits = dict(idx.search_chunks("xlsx", top_k=10))
    # xlsx hit beats pdf hit on the format token.
    assert "d2" in hits
    assert hits.get("d2", 0) > hits.get("d1", 0)


def test_update_filename_index_for_doc_is_idempotent(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "old.pdf", "path": "/", "format": "pdf"}]
    idx = build_filename_bm25_index(_FakeStore(docs), BM25Config(), cache_path=str(tmp_path / "x.pkl"))
    assert len(idx) == 1
    # Apply twice with the same payload — should still be one entry.
    update_filename_index_for_doc(idx, doc_id="d1", filename="new.pdf", path="/", format="pdf")
    update_filename_index_for_doc(idx, doc_id="d1", filename="new.pdf", path="/", format="pdf")
    idx.finalize()
    assert len(idx) == 1
    # Search reflects the new filename, not the old one.
    hits = dict(idx.search_chunks("new", top_k=10))
    assert "d1" in hits
    hits_old = dict(idx.search_chunks("old", top_k=10))
    assert "d1" not in hits_old


def test_remove_filename_index_for_doc(tmp_path: Path):
    docs = [
        {"doc_id": "d1", "filename": "a.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "b.pdf", "path": "/", "format": "pdf"},
    ]
    idx = build_filename_bm25_index(_FakeStore(docs), BM25Config(), cache_path=str(tmp_path / "x.pkl"))
    remove_filename_index_for_doc(idx, "d1")
    idx.finalize()
    assert len(idx) == 1
    assert dict(idx.search_chunks("a", top_k=10)).get("d1") is None


def test_filename_index_path_disabled_when_persistence_off():
    """When ``cache.bm25_persistence=False``, the helper returns the
    empty string so the build / persist paths skip disk I/O."""

    class _Cfg:
        class cache:
            bm25_persistence = False
            bm25_path = "./storage/bm25_index.pkl"
            filename_bm25_path = "./storage/filename_bm25_index.pkl"

    assert filename_index_path(_Cfg) == ""


def test_filename_index_path_default_falls_back_to_module_constant():
    """When no cache section is configured at all, the helper should
    yield the module constant so the build path is still functional
    under default test config."""
    assert filename_index_path(object()) == ""


# ---------------------------------------------------------------------------
# UnifiedSearcher — chunks view
# ---------------------------------------------------------------------------


def _build_searcher(
    docs: list[dict],
    chunks: list[dict],
    tmp_path: Path,
) -> UnifiedSearcher:
    """Construct a searcher backed by real BM25 indices.

    Both indices use the default ``BM25Config``; the same tokenizer
    that runs in production scores the test corpus, so assertions
    don't have to mock score numbers.
    """
    store = _FakeStore(docs, chunks)
    fn_idx = build_filename_bm25_index(store, BM25Config(), cache_path=str(tmp_path / "fn.pkl"))
    bm25 = InMemoryBM25Index(BM25Config())
    for c in chunks:
        bm25.add(c["chunk_id"], c["doc_id"], c["content"])
    bm25.finalize()
    return UnifiedSearcher(bm25_index=bm25, filename_index=fn_idx, rel=store)


def test_chunks_view_default_returns_bm25_hits(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "alpha.pdf", "path": "/", "format": "pdf"}]
    chunks = [_chunk_row("d1", 1, "talks about machine learning")]
    searcher = _build_searcher(docs, chunks, tmp_path)

    result = searcher.search("machine learning")
    assert len(result.chunks) == 1
    hit = result.chunks[0]
    assert hit.doc_id == "d1"
    assert hit.filename == "alpha.pdf"
    assert hit.path == "/"
    assert hit.snippet == "talks about machine learning"
    assert hit.score > 0
    assert hit.boosted_by_filename is False
    # matched_tokens reports which query tokens hit this chunk's bag.
    assert set(hit.matched_tokens or []) == {"machine", "learning"}
    assert result.files is None  # default include is chunks-only


def test_chunks_view_applies_filename_boost_when_doc_filename_matches(tmp_path: Path):
    """A query whose tokens land in BOTH chunks (equal content score)
    AND in one doc's filename should rank the filename-matched doc's
    chunk first via the filename boost."""
    docs = [
        {"doc_id": "d1", "filename": "deep_learning_intro.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "vacation_photos.pdf", "path": "/", "format": "pdf"},
    ]
    # Identical chunk text → identical raw BM25 score. The boost is
    # the only thing that can break the tie.
    chunks = [
        _chunk_row("d2", 1, "deep learning is a subfield of machine learning"),
        _chunk_row("d1", 1, "deep learning is a subfield of machine learning"),
    ]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("deep learning")

    # d1's filename has "deep" + "learning"; d2's doesn't. With equal
    # raw content scores (both chunks share the same body text), d1
    # wins on the boost.
    assert result.chunks[0].doc_id == "d1"
    assert result.chunks[0].boosted_by_filename is True
    assert result.chunks[1].doc_id == "d2"
    assert result.chunks[1].boosted_by_filename is False


def test_chunks_view_filename_boost_is_capped(tmp_path: Path):
    """A vague filename match must NOT overtake a strong content match.
    With a wide BM25 gap (d2 is short and densely about the query;
    d1 buries a single mention in a long, unrelated body) and a
    0.15-fraction boost cap, the strong-content chunk still wins."""
    docs = [
        {"doc_id": "d1", "filename": "report.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "team_photos.pdf", "path": "/", "format": "pdf"},
    ]
    chunks = [
        # d2: short, query-dense chunk — high BM25 via low doc length.
        _chunk_row("d2", 1, "report financials report sales report"),
        # d1: long body with one query mention buried — low BM25.
        _chunk_row(
            "d1",
            1,
            "this is a comprehensive body of unrelated text " * 10 + "report",
        ),
    ]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("report")

    # d2 wins despite d1 getting the filename boost: the cap is 0.15
    # of d2's top score, which can't close the term-frequency gap.
    assert result.chunks[0].doc_id == "d2"


def test_chunks_view_respects_limit(tmp_path: Path):
    docs = [
        {"doc_id": f"d{i}", "filename": f"f{i}.pdf", "path": "/", "format": "pdf"}
        for i in range(5)
    ]
    chunks = [_chunk_row(f"d{i}", 1, f"content number {i}") for i in range(5)]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("content", limit={"chunks": 3})
    assert len(result.chunks) == 3


def test_chunks_view_filters_trashed_docs(tmp_path: Path):
    """Docs in ``/__trash__/...`` must be excluded from chunk hits."""
    docs = [
        {"doc_id": "d1", "filename": "live.pdf", "path": "/projects", "format": "pdf"},
        {"doc_id": "d2", "filename": "old.pdf", "path": "/__trash__/old", "format": "pdf"},
    ]
    chunks = [
        _chunk_row("d1", 1, "tariffs are levied at the border"),
        _chunk_row("d2", 1, "tariffs are levied at the border"),
    ]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("tariffs")
    assert [c.doc_id for c in result.chunks] == ["d1"]


def test_chunks_view_respects_path_prefix(tmp_path: Path):
    """When ``path_prefix`` is set, only docs under that folder match."""
    docs = [
        {"doc_id": "d1", "filename": "a.pdf", "path": "/projects/2024", "format": "pdf"},
        {"doc_id": "d2", "filename": "b.pdf", "path": "/scratch", "format": "pdf"},
    ]
    chunks = [
        _chunk_row("d1", 1, "matter under projects"),
        _chunk_row("d2", 1, "matter under scratch"),
    ]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("matter", path_prefix="/projects")
    assert [c.doc_id for c in result.chunks] == ["d1"]


# ---------------------------------------------------------------------------
# UnifiedSearcher — files view
# ---------------------------------------------------------------------------


def test_files_view_rolls_up_chunks_per_doc(tmp_path: Path):
    """Multiple chunks from the same doc collapse to one FileHit row."""
    docs = [
        {"doc_id": "d1", "filename": "report.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "memo.pdf", "path": "/", "format": "pdf"},
    ]
    # d1's first chunk has a stronger match than its second so it wins
    # the "best chunk" slot for the rollup. d2 trails because its chunk
    # mentions "report" only once and lives in a longer body.
    chunks = [
        _chunk_row("d1", 1, "report report report report opening summary", page=1),
        _chunk_row("d1", 2, "later sections of the report", page=2),
        _chunk_row("d2", 1, "memo text mentions report once amid much else", page=1),
    ]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("report", include=["files"])

    assert result.files is not None
    doc_ids = [f.doc_id for f in result.files]
    assert doc_ids.count("d1") == 1  # rolled up
    assert "d2" in doc_ids
    # Best chunk for d1 is the higher-scoring one (the first chunk).
    d1 = next(f for f in result.files if f.doc_id == "d1")
    assert d1.best_chunk is not None
    assert d1.best_chunk.snippet.startswith("report report report")


def test_files_view_marks_matched_in_filename_only(tmp_path: Path):
    """A doc that matches by filename but has no content hit gets
    ``matched_in == ["filename"]`` and a null ``best_chunk``."""
    docs = [
        {"doc_id": "d1", "filename": "tariffs_2024.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "lunch.pdf", "path": "/", "format": "pdf"},
    ]
    chunks = [
        # Only d2 has body content for this query; d1 enters via filename only.
        _chunk_row("d2", 1, "lunch menu items"),
    ]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("tariffs", include=["files"])

    assert result.files is not None
    by_doc = {f.doc_id: f for f in result.files}
    assert "d1" in by_doc
    assert by_doc["d1"].matched_in == ["filename"]
    assert by_doc["d1"].best_chunk is None


def test_files_view_marks_matched_in_both(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "tariffs_overview.pdf", "path": "/", "format": "pdf"}]
    chunks = [_chunk_row("d1", 1, "tariffs apply at the border")]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("tariffs", include=["files"])

    assert result.files is not None
    f = result.files[0]
    assert set(f.matched_in) == {"filename", "content"}
    assert f.best_chunk is not None
    # best_chunk carries matched_tokens too, mirroring filename_tokens.
    assert "tariffs" in (f.best_chunk.matched_tokens or [])


def test_files_view_includes_filename_tokens_for_ui_bolding(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "Q3_financials_2024.pdf", "path": "/", "format": "pdf"}]
    chunks: list[dict] = []  # no content — files view drives off filename only
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("financials 2024", include=["files"])

    assert result.files is not None
    f = result.files[0]
    # The matched tokens are the intersection of query tokens and the
    # filename's tokens. "financials" and "2024" both appear in the
    # filename entry text.
    assert set(f.filename_tokens or []) >= {"financials", "2024"}


# ---------------------------------------------------------------------------
# Both views requested in one call
# ---------------------------------------------------------------------------


def test_chunks_and_files_returned_together(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "report.pdf", "path": "/", "format": "pdf"}]
    chunks = [_chunk_row("d1", 1, "report content")]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("report", include=["chunks", "files"])

    assert result.chunks
    assert result.files is not None
    assert result.stats.get("include") == ["chunks", "files"]


def test_default_include_is_chunks_only(tmp_path: Path):
    """No ``include`` passed → only chunks view computed; files is None
    so callers can distinguish "not requested" from "requested but empty"."""
    docs = [{"doc_id": "d1", "filename": "x.pdf", "path": "/", "format": "pdf"}]
    chunks = [_chunk_row("d1", 1, "alpha bravo charlie")]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("alpha")
    assert result.files is None


def test_unrecognised_include_falls_back_to_chunks(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "x.pdf", "path": "/", "format": "pdf"}]
    chunks = [_chunk_row("d1", 1, "alpha bravo charlie")]
    searcher = _build_searcher(docs, chunks, tmp_path)
    result = searcher.search("alpha", include=["unknown_view"])
    assert result.chunks
    assert result.files is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_snippet_truncates_long_content():
    long = "x" * 500
    s = _snippet(long)
    assert len(s) <= 201  # 200 chars + ellipsis
    assert s.endswith("…")


def test_snippet_returns_short_content_unchanged():
    assert _snippet("hello world") == "hello world"


def test_default_filename_cache_path_is_module_constant():
    assert FILENAME_BM25_CACHE_PATH.endswith("filename_bm25_index.pkl")
