"""
Unified search: the retrieval primitive exposed standalone.

Pure-lexical, BM25-only. No vector / KG / tree / rerank — those live
on ``/query`` (the answering pipeline). ``/search`` is meant to be the
fast "find me things by keyword" surface, with two views:

  * **chunks view** (always) — top BM25 chunk hits, hydrated from the
    relational store, with the per-hit list of query tokens that
    actually appear in the chunk so the UI can highlight them.

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

Trashed docs are filtered out, and an optional ``path_prefix`` limits
the candidate set by folder. Both filters are applied post-BM25 with
3× over-fetch headroom so the per-view caps still come out full when
hits are evenly spread.
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
    matched_tokens: list[str] | None = None


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
    matched_tokens: list[str] | None = None


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


@dataclass
class _ContentHit:
    """Internal: one BM25 content hit, hydrated from the relational store.

    Holds the raw per-chunk BM25 score (not yet boosted by filename) plus
    the matched query tokens for UI highlighting. ``page_no`` defaults
    to 0 when the chunk row has no page (e.g. plain-text imports)."""

    chunk_id: str
    doc_id: str
    score: float
    content: str
    page_no: int
    matched_tokens: list[str]


class UnifiedSearcher:
    """Run /search — pure BM25, two views.

    Holds no per-request state. The content + filename BM25 indices and
    the relational store are passed in so callers (and tests) can swap
    them for fakes.
    """

    def __init__(
        self,
        *,
        bm25_index: InMemoryBM25Index,
        filename_index: InMemoryBM25Index,
        rel: RelationalStore,
    ):
        self.bm25 = bm25_index
        self.filename_index = filename_index
        self.rel = rel

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        include: list[str] | None = None,
        limit: dict[str, int] | None = None,
        filter: dict | None = None,            # noqa: ARG002 — kept for API compat
        path_prefix: str | None = None,
        overrides: object | None = None,        # noqa: ARG002 — kept for API compat
    ) -> SearchResult:
        """Run unified search.

        ``include`` controls which views are computed:
          * ``["chunks"]`` (default) — chunks only
          * ``["files"]`` — files only
          * ``["chunks", "files"]`` — both
        Empty / unrecognised entries silently fall back to the default.

        ``limit`` is a per-view cap dict, ``{"chunks": 30, "files": 10}``.
        Missing keys use the module defaults.

        ``path_prefix`` limits results to documents under that folder.
        ``filter`` / ``overrides`` are accepted for shape compatibility
        with the previous pipeline-backed signature but are ignored —
        BM25-only search has no rerank / per-call knobs.
        """
        t0 = time.time()
        wanted = set(include or ["chunks"])
        if not wanted & {"chunks", "files"}:
            wanted = {"chunks"}
        cap_chunks = (limit or {}).get("chunks", _DEFAULT_LIMIT_CHUNKS)
        cap_files = (limit or {}).get("files", _DEFAULT_LIMIT_FILES)

        stats: dict = {"include": sorted(wanted)}
        q_tokens = tokenize(query)

        # ── filename BM25 — cheap, runs once even if both views asked ──
        filename_hits = self._search_filenames(query, top_k=max(cap_files, 50))
        stats["filename_hits"] = len(filename_hits)
        filename_token_map: dict[str, list[str]] = {
            doc_id: self._matched_filename_tokens(doc_id, q_tokens)
            for doc_id, _ in filename_hits
        }

        # ── content BM25 — fetch with 3× headroom for trash / path filter ──
        # We size to whichever view needs more candidates: chunks wants
        # ``cap_chunks`` after filtering; files needs several chunks
        # per doc to roll up, so over-fetch by ``cap_files * 5``.
        content_top_k = 0
        if "chunks" in wanted:
            content_top_k = max(content_top_k, cap_chunks)
        if "files" in wanted:
            content_top_k = max(content_top_k, cap_files * 5)
        content_hits = self._search_content(
            query,
            q_tokens=q_tokens,
            top_k=content_top_k * 3 if content_top_k else 0,
            path_prefix=path_prefix,
        )
        stats["content_hits"] = len(content_hits)

        # ── chunks view ────────────────────────────────────────────────
        chunks_view: list[ScoredChunkHit] = []
        if "chunks" in wanted:
            chunks_view = self._build_chunks_view(
                content_hits=content_hits,
                filename_score_by_doc=dict(filename_hits),
                cap=cap_chunks,
            )
        stats["chunk_hits"] = len(chunks_view)

        # ── files view ─────────────────────────────────────────────────
        files_view: list[FileHit] | None = None
        if "files" in wanted:
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
    # Content signal — pure BM25, no pipeline
    # ------------------------------------------------------------------

    def _search_content(
        self,
        query: str,
        *,
        q_tokens: list[str],
        top_k: int,
        path_prefix: str | None,
    ) -> list[_ContentHit]:
        """Run BM25 against the content index, hydrate hits, drop trashed
        and out-of-scope docs. Returns at most ``top_k / 3`` filtered
        hits — caller already over-fetched 3×.

        Filtering pass:
          * trashed docs (path under ``/__trash__``)
          * path_prefix mismatch (when set)

        Both filters require knowing the chunk's doc_id and the doc's
        path, so we batch-fetch chunk rows + doc rows once per query.
        """
        if top_k <= 0 or self.bm25 is None or len(self.bm25) == 0:
            return []

        raw_hits = self.bm25.search_chunks(query, top_k=top_k)
        if not raw_hits:
            return []

        chunk_ids = [c for c, _ in raw_hits]
        score_by_chunk = dict(raw_hits)

        # Batch fetch chunks → content + doc_id + page
        chunk_rows: list[dict] = []
        getter = getattr(self.rel, "get_chunks_by_ids", None)
        if callable(getter):
            chunk_rows = getter(chunk_ids)
        else:
            for cid in chunk_ids:
                row = self.rel.get_chunk(cid)
                if row:
                    chunk_rows.append(row)
        rows_by_chunk = {r["chunk_id"]: r for r in chunk_rows}

        # Per-chunk matched-tokens — single pass over the BM25 index
        # restricted to the hit set.
        token_map = self._matched_content_tokens_batch(chunk_ids, q_tokens)

        # Resolve scope: trashed doc set + path-prefix doc set.
        doc_ids = {
            r.get("doc_id") for r in chunk_rows if r.get("doc_id")
        }
        trashed, path_doc_set = self._resolve_doc_scope(doc_ids, path_prefix)

        budget = max(1, top_k // 3)  # the post-filter cap per caller's request
        out: list[_ContentHit] = []
        for cid in chunk_ids:
            row = rows_by_chunk.get(cid)
            if not row:
                continue
            doc_id = row.get("doc_id")
            if not doc_id or doc_id in trashed:
                continue
            if path_doc_set is not None and doc_id not in path_doc_set:
                continue
            out.append(
                _ContentHit(
                    chunk_id=cid,
                    doc_id=doc_id,
                    score=float(score_by_chunk.get(cid, 0.0)),
                    content=row.get("content") or "",
                    page_no=int(row.get("page_start") or 0),
                    matched_tokens=token_map.get(cid, []),
                )
            )
            if len(out) >= budget:
                break
        return out

    def _matched_content_tokens_batch(
        self, chunk_ids: list[str], q_tokens: list[str]
    ) -> dict[str, list[str]]:
        """Build matched-tokens map for a batch of content chunks in
        one pass over the BM25 index.

        ``InMemoryBM25Index.chunk_ids`` is a list, so ``index()`` per
        chunk would be O(N × M). We instead scan the index once and
        only inspect entries whose chunk_id is in the hit set.
        """
        if not chunk_ids or not q_tokens:
            return {}
        # Dedup query tokens but keep ordering — UI shows them in
        # the same order the user typed where possible.
        q_seen: dict[str, bool] = {}
        for t in q_tokens:
            q_seen.setdefault(t, True)
        q_unique = list(q_seen.keys())

        hit_set = set(chunk_ids)
        positions: dict[str, int] = {}
        for i, cid in enumerate(self.bm25.chunk_ids):
            if cid in hit_set:
                positions[cid] = i
                if len(positions) == len(hit_set):
                    break
        out: dict[str, list[str]] = {}
        for cid, i in positions.items():
            present = set(self.bm25.token_counts[i].keys())
            out[cid] = [t for t in q_unique if t in present]
        return out

    def _resolve_doc_scope(
        self, doc_ids: set[str], path_prefix: str | None
    ) -> tuple[set[str], set[str] | None]:
        """Return ``(trashed_set, path_doc_set)``.

        ``trashed_set`` is the subset of ``doc_ids`` whose document is
        under ``/__trash__``. ``path_doc_set`` is the subset that lives
        under ``path_prefix`` — or ``None`` when no prefix was given,
        meaning "no scope filter".

        Implementation note: we'd love a batched ``get_documents`` but
        the store doesn't expose one yet. ``cap`` ≤ 30 keeps the
        per-doc round trip cheap; if this becomes hot we can add a
        batch fetch on ``Store``.
        """
        if not doc_ids:
            return set(), (set() if path_prefix else None)

        try:
            from persistence.folder_service import TRASH_PATH
        except ImportError:
            TRASH_PATH = "/__trash__"

        prefix = (path_prefix or "").rstrip("/")
        path_doc_set: set[str] | None = set() if path_prefix else None
        trashed: set[str] = set()

        for did in doc_ids:
            if not did:
                continue
            row = self.rel.get_document(did)
            if not row:
                continue
            doc_path = row.get("path") or ""
            if doc_path.startswith(TRASH_PATH + "/") or doc_path == TRASH_PATH:
                trashed.add(did)
                continue
            if path_doc_set is not None:
                if not prefix or doc_path == prefix or doc_path.startswith(prefix + "/"):
                    path_doc_set.add(did)
        return trashed, path_doc_set

    # ------------------------------------------------------------------
    # Chunks view — projects _ContentHit into the API shape
    # ------------------------------------------------------------------

    def _build_chunks_view(
        self,
        *,
        content_hits: list[_ContentHit],
        filename_score_by_doc: dict[str, float],
        cap: int,
    ) -> list[ScoredChunkHit]:
        """Project ``_ContentHit`` rows into ``ScoredChunkHit`` rows,
        applying the filename boost and hydrating filename / path from
        the relational store in a single batched call.

        The boost is additive and capped at ``α`` × the top BM25 score —
        a filename match nudges the order but never overrides a strong
        content match.
        """
        if not content_hits:
            return []

        top_score = max(h.score for h in content_hits) or 0.0
        boost_cap = top_score * _FILENAME_BOOST_ALPHA
        max_fn_score = max(filename_score_by_doc.values()) if filename_score_by_doc else 0.0

        doc_ids = {h.doc_id for h in content_hits if h.doc_id}
        doc_meta = self._batch_fetch_docs(doc_ids)

        out: list[ScoredChunkHit] = []
        for h in content_hits:
            base_score = h.score
            fn_raw = filename_score_by_doc.get(h.doc_id, 0.0)
            boosted = False
            if fn_raw > 0 and max_fn_score > 0 and boost_cap > 0:
                base_score += boost_cap * (fn_raw / max_fn_score)
                boosted = True

            meta = doc_meta.get(h.doc_id, {})
            out.append(
                ScoredChunkHit(
                    chunk_id=h.chunk_id,
                    doc_id=h.doc_id,
                    filename=meta.get("filename") or "",
                    path=meta.get("path") or "",
                    page_no=h.page_no,
                    snippet=_snippet(h.content),
                    score=base_score,
                    bbox=None,  # /search omits bbox; clients use /query for highlights
                    boosted_by_filename=boosted,
                    matched_tokens=h.matched_tokens or None,
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
        content_hits: list[_ContentHit],
        filename_token_map: dict[str, list[str]],
        cap: int,
    ) -> list[FileHit]:
        """Per-doc rollup of content + RRF fusion with filename hits.

        For each doc that appears in either the filename hits or the
        content hits, compute the file's RRF score from the two ranked
        lists. Snippet comes from the doc's best content chunk (which
        is the highest-rank chunk per doc — content_hits is already in
        BM25-rank order).
        """
        # Roll up content hits by doc_id (best chunk wins, i.e. first
        # chunk for that doc in rank order).
        best_content_chunk: dict[str, _ContentHit] = {}
        content_rank_by_doc: dict[str, int] = {}
        rank = 0
        for h in content_hits:
            if not h.doc_id or h.doc_id in content_rank_by_doc:
                continue
            rank += 1
            content_rank_by_doc[h.doc_id] = rank
            best_content_chunk[h.doc_id] = h

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
                chunk_match = ChunkMatch(
                    chunk_id=ch.chunk_id,
                    snippet=_snippet(ch.content),
                    page_no=ch.page_no,
                    score=ch.score,
                    matched_tokens=ch.matched_tokens or None,
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
