"""Unit tests for the unified search layer (`retrieval/file_search.py`).

Covers two layers:

  1. The filename BM25 index — doc-keyed, reuses the same
     ``InMemoryBM25Index`` class as the content path. Verifies
     tokenisation of `filename + path + format` is intuitive
     (substring/path-segment/extension all match), persistence
     round-trips, and `update_filename_index_for_doc` is idempotent.

  2. ``UnifiedSearcher`` — orchestrates pipeline + filename index +
     dual-view aggregation. Uses a fake pipeline + tiny fake store
     so the test is fast and doesn't require the full ingestion
     chain.

LLM / embedder / vector-store calls are entirely avoided here; the
fake pipeline returns deterministic ``MergedChunk`` lists and the
test asserts on the `SearchResult` shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import BM25Config
from parser.schema import Chunk
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
from retrieval.types import MergedChunk

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal Store stand-in: just enough for filename-index build,
    UnifiedSearcher's hydrations, and incremental update calls."""

    def __init__(self, docs: list[dict]):
        self._docs = {d["doc_id"]: d for d in docs}

    def list_document_ids(self) -> list[str]:
        return list(self._docs)

    def get_document(self, doc_id: str) -> dict | None:
        return self._docs.get(doc_id)


@dataclass
class _FakeRetrievalResult:
    """Mimics ``retrieval.types.RetrievalResult`` for the searcher."""

    merged: list[MergedChunk]
    citations: list = None  # unused
    vector_hits: list = None
    tree_hits: list = None
    stats: dict = None
    query_plan = None
    kg_context = None


class _FakePipeline:
    """Returns a fixed list of ``MergedChunk`` for any query.

    The fixed list lets us deterministically assert how the searcher
    projects MergedChunk → ScoredChunkHit and how it rolls up into
    the files view.
    """

    def __init__(self, merged: list[MergedChunk]):
        self._merged = merged
        self.calls: list[tuple] = []

    def retrieve(self, query, *, filter=None, overrides=None, **_):
        self.calls.append((query, filter, overrides))
        return _FakeRetrievalResult(merged=list(self._merged))


def _chunk(doc_id: str, seq: int, content: str, *, page: int = 1) -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}:1:c{seq}",
        doc_id=doc_id,
        parse_version=1,
        node_id=f"node-{seq}",
        block_ids=[f"{doc_id}:1:1:0"],
        content=content,
        content_type="text",
        page_start=page,
        page_end=page,
        token_count=len(content) // 4,
    )


def _merged(chunk: Chunk, score: float, source: str = "vector") -> MergedChunk:
    return MergedChunk(
        chunk_id=chunk.chunk_id,
        rrf_score=score,
        sources={source},
        chunk=chunk,
    )


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


def _build_searcher(docs: list[dict], merged: list[MergedChunk], tmp_path: Path) -> UnifiedSearcher:
    store = _FakeStore(docs)
    fn_idx = build_filename_bm25_index(store, BM25Config(), cache_path=str(tmp_path / "fn.pkl"))
    pipeline = _FakePipeline(merged)
    return UnifiedSearcher(pipeline=pipeline, filename_index=fn_idx, rel=store)


def test_chunks_view_default_returns_pipeline_results(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "alpha.pdf", "path": "/", "format": "pdf"}]
    merged = [_merged(_chunk("d1", 1, "talks about machine learning"), score=0.5)]
    searcher = _build_searcher(docs, merged, tmp_path)

    result = searcher.search("machine learning")
    assert len(result.chunks) == 1
    hit = result.chunks[0]
    assert hit.doc_id == "d1"
    assert hit.filename == "alpha.pdf"
    assert hit.path == "/"
    assert hit.snippet == "talks about machine learning"
    assert hit.score == 0.5
    assert hit.boosted_by_filename is False
    assert result.files is None  # default include is chunks-only


def test_chunks_view_applies_filename_boost_when_doc_filename_matches(tmp_path: Path):
    """A query that matches a doc's FILENAME should boost that doc's
    chunks. With equal raw RRF scores, the filename-matched doc's chunk
    must come out on top of an unboosted chunk."""
    docs = [
        {"doc_id": "d1", "filename": "deep_learning_intro.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "vacation_photos.pdf", "path": "/", "format": "pdf"},
    ]
    merged = [
        # Same raw RRF — without the filename boost, order is preserved.
        _merged(_chunk("d2", 1, "this chunk mentions neural networks"), score=0.4),
        _merged(_chunk("d1", 1, "this chunk mentions neural networks"), score=0.4),
    ]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("deep learning")

    # d1 is boosted because filename matches "deep" + "learning".
    assert result.chunks[0].doc_id == "d1"
    assert result.chunks[0].boosted_by_filename is True
    assert result.chunks[1].doc_id == "d2"
    assert result.chunks[1].boosted_by_filename is False


def test_chunks_view_filename_boost_is_capped(tmp_path: Path):
    """A vague filename match must NOT overtake a strong content match.
    With a wide score gap (1.0 vs 0.1) and a 0.15-fraction boost cap,
    the strong-content chunk should still rank first even if the
    weaker chunk's doc has a filename match.
    """
    docs = [
        {"doc_id": "d1", "filename": "report.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "team_photos.pdf", "path": "/", "format": "pdf"},
    ]
    merged = [
        _merged(_chunk("d2", 1, "deep technical content"), score=1.0),  # strong content
        _merged(_chunk("d1", 1, "weak match"), score=0.1),               # weak content + filename hit
    ]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("report")

    # d2 still wins because the boost on d1 is capped to 0.15 * 1.0 = 0.15;
    # 0.1 + 0.15 = 0.25 < 1.0.
    assert result.chunks[0].doc_id == "d2"


def test_chunks_view_respects_limit(tmp_path: Path):
    docs = [{"doc_id": f"d{i}", "filename": f"f{i}.pdf", "path": "/", "format": "pdf"} for i in range(5)]
    merged = [_merged(_chunk(f"d{i}", 1, f"content {i}"), score=0.5 - i * 0.05) for i in range(5)]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("content", limit={"chunks": 3})
    assert len(result.chunks) == 3


# ---------------------------------------------------------------------------
# UnifiedSearcher — files view
# ---------------------------------------------------------------------------


def test_files_view_rolls_up_chunks_per_doc(tmp_path: Path):
    """Multiple chunks from the same doc collapse to one FileHit row."""
    docs = [
        {"doc_id": "d1", "filename": "report.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "memo.pdf", "path": "/", "format": "pdf"},
    ]
    merged = [
        _merged(_chunk("d1", 1, "first chunk of report", page=1), score=0.9),
        _merged(_chunk("d1", 2, "second chunk of report", page=2), score=0.7),
        _merged(_chunk("d2", 1, "memo text"), score=0.6),
    ]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("report", include=["files"])

    assert result.files is not None
    doc_ids = [f.doc_id for f in result.files]
    assert doc_ids.count("d1") == 1  # rolled up
    assert "d2" in doc_ids
    # Best chunk for d1 is the first (highest-rank) one.
    d1 = next(f for f in result.files if f.doc_id == "d1")
    assert d1.best_chunk is not None
    assert d1.best_chunk.snippet == "first chunk of report"


def test_files_view_marks_matched_in_filename_only(tmp_path: Path):
    """A doc that matches by filename but isn't in the content hits gets
    ``matched_in == ["filename"]`` and a null ``best_chunk``."""
    docs = [
        {"doc_id": "d1", "filename": "tariffs_2024.pdf", "path": "/", "format": "pdf"},
        {"doc_id": "d2", "filename": "lunch.pdf", "path": "/", "format": "pdf"},
    ]
    merged = [
        # Pipeline returns a chunk only for d2; d1 gets in via filename match.
        _merged(_chunk("d2", 1, "lunch menu items"), score=0.5),
    ]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("tariffs", include=["files"])

    assert result.files is not None
    by_doc = {f.doc_id: f for f in result.files}
    assert "d1" in by_doc
    assert by_doc["d1"].matched_in == ["filename"]
    assert by_doc["d1"].best_chunk is None


def test_files_view_marks_matched_in_both(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "tariffs_overview.pdf", "path": "/", "format": "pdf"}]
    merged = [_merged(_chunk("d1", 1, "tariffs apply at the border"), score=0.7)]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("tariffs", include=["files"])

    assert result.files is not None
    f = result.files[0]
    assert set(f.matched_in) == {"filename", "content"}
    assert f.best_chunk is not None


def test_files_view_includes_filename_tokens_for_ui_bolding(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "Q3_financials_2024.pdf", "path": "/", "format": "pdf"}]
    merged = []  # no content hits — files view drives entirely off filename
    searcher = _build_searcher(docs, merged, tmp_path)
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
    merged = [_merged(_chunk("d1", 1, "report content"), score=0.5)]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("report", include=["chunks", "files"])

    assert result.chunks
    assert result.files is not None
    assert result.stats.get("include") == ["chunks", "files"]


def test_pipeline_called_only_once_per_search(tmp_path: Path):
    """When both views are requested, the pipeline shouldn't be hit
    twice — the searcher reuses the chunks-view pipeline hits to build
    the files rollup."""
    docs = [{"doc_id": "d1", "filename": "report.pdf", "path": "/", "format": "pdf"}]
    merged = [_merged(_chunk("d1", 1, "content"), score=0.5)]
    searcher = _build_searcher(docs, merged, tmp_path)
    searcher.search("report", include=["chunks", "files"])

    assert len(searcher.pipeline.calls) == 1


def test_default_include_is_chunks_only(tmp_path: Path):
    """No ``include`` passed → only chunks view computed; files is None
    so callers can distinguish "not requested" from "requested but empty"."""
    docs = [{"doc_id": "d1", "filename": "x.pdf", "path": "/", "format": "pdf"}]
    merged = [_merged(_chunk("d1", 1, "x"), score=0.5)]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("x")
    assert result.files is None


def test_unrecognised_include_falls_back_to_chunks(tmp_path: Path):
    docs = [{"doc_id": "d1", "filename": "x.pdf", "path": "/", "format": "pdf"}]
    merged = [_merged(_chunk("d1", 1, "x"), score=0.5)]
    searcher = _build_searcher(docs, merged, tmp_path)
    result = searcher.search("x", include=["unknown_view"])
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
