"""
Unified search: the retrieval primitive exposed standalone.

Sits next to ``retrieval.pipeline.RetrievalPipeline`` (which produces
chunks + LLM rerank + answer-ready output) and exposes a cheaper, no-
LLM "find me things" surface that returns:

  * **chunks view** (always) — ranked chunks from the existing
    retrieval pipeline. Same shape that ``/query`` consumes internally
    BEFORE answer synthesis. By default we skip rerank to keep the
    primitive cheap; callers wanting rerank pass an override.

  * **files view** (opt-in) — file-level rollup of the same query.
    The filename BM25 hits and per-doc rollups of the content hits are
    fused via RRF, one row per file with snippet + matched_in badge.

Filename signal feeds both views asymmetrically:

  * chunks: a small additive boost on chunks of filename-matched docs
    (capped fraction of the top content score) — surfaces files whose
    filename mentions the query without letting an irrelevant body
    win.
  * files: full RRF fusion at file granularity — parameter-free, the
    same algo the main retrieval merge uses elsewhere.

See ``docs/roadmaps/retrieval-evolution.md`` for the design rationale
and rejected alternatives.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import BM25Config
from persistence.store import Store as RelationalStore

from .bm25 import InMemoryBM25Index, tokenize

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filename index — doc-keyed BM25 (one entry per Document)
# ---------------------------------------------------------------------------


FILENAME_BM25_CACHE_PATH = "./storage/filename_bm25_index.pkl"


def _filename_index_text(filename: str, path: str, format: str) -> str:
    """Compose the searchable text for one doc's filename-index entry.

    Tokenises ``filename`` + ``path`` + ``format`` together so a single
    BM25 query matches against any of them. The path is treated as
    space-separated segments (the tokenizer's regex already splits on
    non-alnum), so ``/legal/2024/contracts/`` contributes
    ``["legal", "2024", "contracts"]``. ``format`` is the lowercase
    extension (``"pdf"`` / ``"xlsx"`` / ``"docx"``) so type-intent
    queries ("find a pdf about ...") get a lexical anchor too.
    """
    parts = [filename or "", path or "", (format or "").lower()]
    return " ".join(p for p in parts if p)


def build_filename_bm25_index(
    rel: RelationalStore,
    cfg: BM25Config,
    *,
    cache_path: str = FILENAME_BM25_CACHE_PATH,
    force_rebuild: bool = False,
) -> InMemoryBM25Index:
    """Build or load the filename BM25 index.

    Same shape as ``retrieval.pipeline.build_bm25_index`` but the
    "chunk" granularity is one entry per Document. The index is
    keyed by ``doc_id`` in both the ``chunk_ids`` and ``doc_ids``
    slots — searches return ``[(doc_id, score), ...]``.
    """
    if not force_rebuild and cache_path:
        cached = InMemoryBM25Index.load(cache_path, cfg)
        if cached is not None and len(cached) > 0:
            return cached

    t0 = time.time()
    index = InMemoryBM25Index(cfg)

    doc_ids: list[str] = []
    lister = getattr(rel, "list_document_ids", None)
    if callable(lister):
        doc_ids = list(lister())

    for doc_id in doc_ids:
        row = rel.get_document(doc_id)
        if not row:
            continue
        text = _filename_index_text(
            filename=row.get("filename") or "",
            path=row.get("path") or "",
            format=row.get("format") or "",
        )
        if not text.strip():
            continue
        # chunk_id == doc_id: one entry per doc. Reusing the existing
        # InMemoryBM25Index keeps persistence + incremental update +
        # tokenizer behaviour identical to the content path.
        index.add(chunk_id=doc_id, doc_id=doc_id, text=text)
    index.finalize()
    elapsed = int((time.time() - t0) * 1000)
    log.info("filename BM25 index built: %d docs, %dms", len(index), elapsed)

    if cache_path:
        try:
            index.save(cache_path)
        except Exception as e:
            log.warning("filename BM25 cache save failed: %s", e)

    return index


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ChunkMatch:
    """The best content chunk for a file-level rollup row."""

    chunk_id: str
    snippet: str
    page_no: int
    score: float


@dataclass
class ScoredChunkHit:
    """One chunk hit in the chunks view."""

    chunk_id: str
    doc_id: str
    filename: str
    path: str
    page_no: int
    snippet: str
    score: float
    bbox: tuple[float, float, float, float] | None = None
    boosted_by_filename: bool = False


@dataclass
class FileHit:
    """One file row in the files view."""

    doc_id: str
    filename: str
    path: str
    format: str
    score: float
    matched_in: list[str] = field(default_factory=list)
    best_chunk: ChunkMatch | None = None
    filename_tokens: list[str] | None = None


@dataclass
class SearchResult:
    """Container for a unified search response."""

    chunks: list[ScoredChunkHit]
    files: list[FileHit] | None
    stats: dict


# ---------------------------------------------------------------------------
# UnifiedSearcher — the orchestrator
# ---------------------------------------------------------------------------


# Reciprocal-Rank-Fusion constant, matched to the rest of the pipeline
# (see ``retrieval.merge.RRFMerger`` / ``MergeConfig.rrf_k``). One knob,
# parameter-free, no per-corpus tuning.
_RRF_K = 60

# Filename-boost coefficient: chunks of filename-matched docs get an
# additive boost capped at this fraction of the top content score so a
# vague filename hit can't beat a strong content match. ``0.15`` is a
# starting default — tune from real query logs once we have any.
_FILENAME_BOOST_ALPHA = 0.15

# Default per-view limits when the request omits them.
_DEFAULT_LIMIT_CHUNKS = 30
_DEFAULT_LIMIT_FILES = 10


class UnifiedSearcher:
    """Run /search.

    Composes the existing retrieval pipeline (for the chunks view) with
    a doc-keyed filename BM25 (for the filename signal). Holds no
    state of its own — the pipeline + filename index are passed in,
    so callers can swap mocks for tests.
    """

    def __init__(
        self,
        *,
        pipeline,  # retrieval.pipeline.RetrievalPipeline
        filename_index: InMemoryBM25Index,
        rel: RelationalStore,
    ):
        self.pipeline = pipeline
        self.filename_index = filename_index
        self.rel = rel

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        include: list[str] | None = None,
        limit: dict[str, int] | None = None,
        filter: dict | None = None,
        path_prefix: str | None = None,
        overrides: dict | None = None,
    ) -> SearchResult:
        """Run unified search.

        ``include`` controls which views are computed:
          * ``["chunks"]`` (default) — chunks only
          * ``["files"]`` — files only
          * ``["chunks", "files"]`` — both
        Empty / unrecognised entries silently fall back to the default.

        ``limit`` is a per-view cap dict, ``{"chunks": 30, "files": 10}``.
        Missing keys use the module defaults.

        ``filter`` / ``path_prefix`` / ``overrides`` plumb through to
        the underlying retrieval pipeline (path scoping, per-call knobs).
        """
        t0 = time.time()
        wanted = set(include or ["chunks"])
        if not wanted & {"chunks", "files"}:
            wanted = {"chunks"}
        cap_chunks = (limit or {}).get("chunks", _DEFAULT_LIMIT_CHUNKS)
        cap_files = (limit or {}).get("files", _DEFAULT_LIMIT_FILES)

        stats: dict = {"include": sorted(wanted)}

        # ── filename BM25 — small, runs once even if both views asked ──
        filename_hits = self._search_filenames(query, top_k=max(cap_files, 50))
        stats["filename_hits"] = len(filename_hits)

        # Pre-compute the matched-token list per filename hit so the
        # files view can return them for UI bolding without re-tokenising.
        q_tokens = tokenize(query)
        filename_token_map: dict[str, list[str]] = {}
        for doc_id, _ in filename_hits:
            filename_token_map[doc_id] = self._matched_filename_tokens(doc_id, q_tokens)

        # ── chunks view ────────────────────────────────────────────────
        chunks_view: list[ScoredChunkHit] = []
        chunk_pipeline_hits = []
        if "chunks" in wanted:
            chunk_pipeline_hits = self._run_pipeline(
                query,
                top_k=cap_chunks * 2,  # over-fetch so the filename boost can re-rank
                filter=filter,
                path_prefix=path_prefix,
                overrides=overrides,
            )
            chunks_view = self._build_chunks_view(
                pipeline_hits=chunk_pipeline_hits,
                filename_score_by_doc=dict(filename_hits),
                cap=cap_chunks,
            )
        stats["chunk_hits"] = len(chunks_view)

        # ── files view ─────────────────────────────────────────────────
        files_view: list[FileHit] | None = None
        if "files" in wanted:
            # Reuse pipeline hits if we already have them; otherwise run
            # a smaller search just to get the per-doc content rollup.
            content_hits = chunk_pipeline_hits
            if not content_hits:
                content_hits = self._run_pipeline(
                    query,
                    top_k=cap_files * 5,  # need several chunks per doc to roll up
                    filter=filter,
                    path_prefix=path_prefix,
                    overrides=overrides,
                )
            files_view = self._build_files_view(
                filename_hits=filename_hits,
                content_hits=content_hits,
                filename_token_map=filename_token_map,
                cap=cap_files,
            )
            stats["file_hits"] = len(files_view)

        stats["elapsed_ms"] = int((time.time() - t0) * 1000)
        return SearchResult(chunks=chunks_view, files=files_view, stats=stats)

    # ------------------------------------------------------------------
    # Filename signal
    # ------------------------------------------------------------------

    def _search_filenames(self, query: str, *, top_k: int) -> list[tuple[str, float]]:
        """Return ``[(doc_id, score), ...]`` from the filename index.

        ``InMemoryBM25Index.search_chunks`` returns ``(chunk_id, score)``
        and our index uses ``chunk_id == doc_id`` so the shape is right
        without re-mapping.
        """
        if self.filename_index is None or len(self.filename_index) == 0:
            return []
        return self.filename_index.search_chunks(query, top_k=top_k)

    def _matched_filename_tokens(self, doc_id: str, q_tokens: list[str]) -> list[str]:
        """Pick out which query tokens actually appear in the filename
        entry. Used for UI bolding so the result row can highlight the
        matched portion of the displayed filename.
        """
        if not q_tokens:
            return []
        try:
            i = self.filename_index.chunk_ids.index(doc_id)
        except ValueError:
            return []
        present = set(self.filename_index.token_counts[i].keys())
        return [t for t in q_tokens if t in present]

    # ------------------------------------------------------------------
    # Chunks view
    # ------------------------------------------------------------------

    def _run_pipeline(
        self,
        query: str,
        *,
        top_k: int,
        filter: dict | None,
        path_prefix: str | None,
        overrides,  # dict | QueryOverrides | None
    ):
        """Call the existing retrieval pipeline and return its
        ``RetrievalResult.merged`` — the post-RRF list of ``MergedChunk``
        the answering layer normally consumes.

        Default behaviour skips rerank — the LLM call is the dominant
        cost and ``/search`` is supposed to be cheap. Callers wanting
        rerank pass ``overrides.rerank = True`` (or ``{"rerank": True}``
        which we adapt).
        """
        # Build a filter dict in the shape the pipeline expects.
        merged_filter: dict = dict(filter or {})
        if path_prefix:
            merged_filter["_path_filter"] = path_prefix

        # Adapt dict-shape overrides into the pipeline's QueryOverrides
        # without coupling this module to that pydantic model. If
        # ``overrides`` is already a QueryOverrides we pass it through.
        ov = overrides
        if isinstance(overrides, dict) or overrides is None:
            ov = self._build_overrides(overrides or {}, top_k=top_k)

        result = self.pipeline.retrieve(
            query,
            filter=merged_filter or None,
            overrides=ov,
        )
        # ``result.merged`` is the canonical post-RRF list of MergedChunk.
        # Older pipeline versions occasionally exposed this as
        # ``merged_chunks`` — be defensive so the searcher survives an
        # internal rename.
        return getattr(result, "merged", None) or getattr(result, "merged_chunks", []) or []

    @staticmethod
    def _build_overrides(d: dict, *, top_k: int):
        """Adapt a plain-dict overrides payload into a ``QueryOverrides``.

        ``QueryOverrides`` is the pydantic model the pipeline reads;
        our public surface accepts JSON-y dicts so the search route can
        forward request bodies without coupling clients to the model
        layout. We default ``rerank=False`` to keep ``/search`` cheap;
        callers that want rerank pass it explicitly.
        """
        from api.schemas import QueryOverrides

        payload = dict(d)
        payload.setdefault("rerank", False)
        # Cap rerank_top_k / candidate_limit at our requested top_k so
        # the pipeline doesn't waste work on chunks we won't return.
        payload.setdefault("candidate_limit", max(top_k, 60))
        return QueryOverrides(**payload)

    def _build_chunks_view(
        self,
        *,
        pipeline_hits,                              # list[MergedChunk]
        filename_score_by_doc: dict[str, float],
        cap: int,
    ) -> list[ScoredChunkHit]:
        """Project ``MergedChunk`` rows into ``ScoredChunkHit`` rows,
        applying the filename boost and hydrating filename / path from
        the relational store in a single batched call.

        The boost is additive and capped: it tops out at ``α`` × the
        top RRF score — a filename match nudges the order but never
        overrides a strong content match.
        """
        if not pipeline_hits:
            return []

        # Top content score → cap on the filename boost so it stays a
        # nudge, not a takeover. ``rrf_score`` is the canonical post-
        # fusion score on a ``MergedChunk``.
        top_score = max(getattr(h, "rrf_score", 0.0) for h in pipeline_hits) or 0.0
        boost_cap = top_score * _FILENAME_BOOST_ALPHA

        # Normalise filename scores to [0, 1] within the candidate set.
        max_fn_score = max(filename_score_by_doc.values()) if filename_score_by_doc else 0.0

        # Batch-fetch document metadata (filename, path, format) so we
        # don't issue one round-trip per hit.
        doc_ids = {
            getattr(h.chunk, "doc_id", None) or self._doc_id_of_chunk(getattr(h, "chunk_id", ""))
            for h in pipeline_hits
            if getattr(h, "chunk", None) is not None or getattr(h, "chunk_id", None)
        }
        doc_meta: dict[str, dict] = self._batch_fetch_docs(doc_ids)

        out: list[ScoredChunkHit] = []
        for h in pipeline_hits:
            chunk = getattr(h, "chunk", None)
            if chunk is None:
                # Hit without a rehydrated chunk — pipeline anomaly. Skip.
                continue
            doc_id = chunk.doc_id
            base_score = getattr(h, "rrf_score", 0.0)
            fn_raw = filename_score_by_doc.get(doc_id, 0.0)
            boosted = False
            if fn_raw > 0 and max_fn_score > 0 and boost_cap > 0:
                base_score += boost_cap * (fn_raw / max_fn_score)
                boosted = True

            meta = doc_meta.get(doc_id, {})
            out.append(
                ScoredChunkHit(
                    chunk_id=chunk.chunk_id,
                    doc_id=doc_id,
                    filename=meta.get("filename") or "",
                    path=meta.get("path") or "",
                    page_no=int(chunk.page_start or 0),
                    snippet=_snippet(chunk.content),
                    score=base_score,
                    bbox=None,  # /search omits bbox; clients use /query for highlights
                    boosted_by_filename=boosted,
                )
            )

        out.sort(key=lambda c: -c.score)
        return out[:cap]

    def _batch_fetch_docs(self, doc_ids) -> dict[str, dict]:
        """One ``get_document`` per id; if the store later grows a
        bulk variant we'll switch to it. At ``cap_chunks ≈ 30`` this is
        already fast, and trashed-doc filtering already runs upstream
        so we don't worry about wasted lookups."""
        out: dict[str, dict] = {}
        for did in doc_ids:
            if not did:
                continue
            row = self.rel.get_document(did)
            if row:
                out[did] = row
        return out

    @staticmethod
    def _doc_id_of_chunk(chunk_id: str) -> str:
        """Recover doc_id from a ``{doc_id}:{parse_version}:c{seq}`` id."""
        parts = chunk_id.rsplit(":", 2)
        return parts[0] if len(parts) == 3 else chunk_id

    # ------------------------------------------------------------------
    # Files view
    # ------------------------------------------------------------------

    def _build_files_view(
        self,
        *,
        filename_hits: list[tuple[str, float]],
        content_hits,                                # list[MergedChunk]
        filename_token_map: dict[str, list[str]],
        cap: int,
    ) -> list[FileHit]:
        """Per-doc rollup of content + RRF fusion with filename hits.

        For each doc that appears in either the filename hits or the
        content hits, compute the file's RRF score from the two ranked
        lists. Snippet comes from the doc's best content chunk (which
        is the highest-rank chunk per doc — content_hits is already in
        rank order coming out of the pipeline).
        """
        # Roll up content hits by doc_id (best chunk wins, i.e. first
        # chunk for that doc in rank order).
        best_content_chunk: dict[str, object] = {}
        content_rank_by_doc: dict[str, int] = {}
        rank = 0
        for h in content_hits:
            chunk = getattr(h, "chunk", None)
            doc_id = getattr(chunk, "doc_id", None) if chunk is not None else None
            if not doc_id:
                # Fallback for hits whose chunk wasn't rehydrated.
                doc_id = self._doc_id_of_chunk(getattr(h, "chunk_id", ""))
            if not doc_id or doc_id in content_rank_by_doc:
                continue
            rank += 1
            content_rank_by_doc[doc_id] = rank
            best_content_chunk[doc_id] = h

        filename_rank_by_doc: dict[str, int] = {}
        for i, (doc_id, _) in enumerate(filename_hits, start=1):
            filename_rank_by_doc[doc_id] = i

        all_docs = set(filename_rank_by_doc) | set(content_rank_by_doc)
        if not all_docs:
            return []

        # RRF fusion. Same algorithm as the main retrieval merge.
        scored: list[tuple[str, float]] = []
        for doc_id in all_docs:
            score = 0.0
            if doc_id in filename_rank_by_doc:
                score += 1.0 / (_RRF_K + filename_rank_by_doc[doc_id])
            if doc_id in content_rank_by_doc:
                score += 1.0 / (_RRF_K + content_rank_by_doc[doc_id])
            scored.append((doc_id, score))
        scored.sort(key=lambda kv: -kv[1])
        scored = scored[:cap]

        # Hydrate doc rows for the top entries. One round-trip per doc;
        # at ``cap`` ≤ 30 this is fine.
        out: list[FileHit] = []
        for doc_id, score in scored:
            row = self.rel.get_document(doc_id)
            if not row:
                continue
            matched_in: list[str] = []
            if doc_id in filename_rank_by_doc:
                matched_in.append("filename")
            if doc_id in content_rank_by_doc:
                matched_in.append("content")

            chunk_match: ChunkMatch | None = None
            if doc_id in best_content_chunk:
                ch = best_content_chunk[doc_id]
                inner = getattr(ch, "chunk", None)
                if inner is not None:
                    chunk_match = ChunkMatch(
                        chunk_id=inner.chunk_id,
                        snippet=_snippet(inner.content),
                        page_no=int(inner.page_start or 0),
                        score=getattr(ch, "rrf_score", 0.0),
                    )

            out.append(
                FileHit(
                    doc_id=doc_id,
                    filename=row.get("filename") or "",
                    path=row.get("path") or "",
                    format=row.get("format") or "",
                    score=score,
                    matched_in=matched_in,
                    best_chunk=chunk_match,
                    filename_tokens=filename_token_map.get(doc_id) or None,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Convenience helpers used by AppState for incremental updates
# ---------------------------------------------------------------------------


def update_filename_index_for_doc(
    index: InMemoryBM25Index,
    *,
    doc_id: str,
    filename: str,
    path: str,
    format: str,
) -> None:
    """Replace one doc's entry in the filename index in place.

    Idempotent: removes any existing entry for the doc, adds the fresh
    one. The caller is responsible for ``finalize()`` and ``save()``.
    """
    index.remove_doc(doc_id)
    text = _filename_index_text(filename=filename, path=path, format=format)
    if text.strip():
        index.add(chunk_id=doc_id, doc_id=doc_id, text=text)


def remove_filename_index_for_doc(index: InMemoryBM25Index, doc_id: str) -> None:
    """Drop a doc's entry from the filename index. Used on permanent delete."""
    index.remove_doc(doc_id)


def filename_index_path(cfg) -> str:
    """Resolve the configured cache path for the filename index.

    Returns ``""`` when persistence is disabled — the build helpers
    treat the empty string as "don't persist".
    """
    cache = getattr(cfg, "cache", None)
    if cache is None:
        return ""
    if not getattr(cache, "bm25_persistence", True):
        return ""
    custom = getattr(cache, "filename_bm25_path", "")
    return custom or _default_filename_path(getattr(cache, "bm25_path", ""))


_SNIPPET_CHARS = 200


def _snippet(content: str | None) -> str:
    """First ~200 chars of chunk content. /search snippets are simple —
    callers that want highlighted spans use ``/query`` (which has the
    full citation builder + bbox renderer)."""
    if not content:
        return ""
    s = content.strip()
    if len(s) <= _SNIPPET_CHARS:
        return s
    return s[:_SNIPPET_CHARS].rstrip() + "…"


def _default_filename_path(content_path: str) -> str:
    """Derive a sibling cache path next to the content BM25 cache.

    For ``./storage/bm25_index.pkl`` we yield
    ``./storage/filename_bm25_index.pkl``. Falls back to the module
    constant when no content path is configured.
    """
    if not content_path:
        return FILENAME_BM25_CACHE_PATH
    p = Path(content_path)
    return str(p.with_name(p.stem.replace("bm25", "filename_bm25") + p.suffix))
