"""
In-memory BM25 index with disk persistence.

Supports save/load via pickle so the index survives restarts
without a full rebuild from the relational store. Incremental
add is supported: after ingesting new documents, call add() +
finalize() + save() instead of rebuilding everything.

Zero external dependencies. Good up to ~100K chunks. For larger
corpora, swap in DB-native full-text (Postgres tsvector, SQLite
FTS5) behind the same search_chunks / search_docs interface.

Also exposes ``build_bm25_index`` — a convenience builder that
loads from disk cache when fresh, falls back to scanning the
relational store. Used by ``api.state.AppState`` at startup and
by the ingestion pipeline to refresh after a doc is added. Lives
here (not in a separate ``builder.py``) so the index class and
its lifecycle stay in one file.
"""

from __future__ import annotations

import logging
import math
import os
import pickle
import re
import time
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from config import BM25Config

log = logging.getLogger(__name__)


# Default on-disk path for the persisted BM25 index. Caller can
# override per-instance via the ``cache_path`` arg of
# ``build_bm25_index`` / ``InMemoryBM25Index.save``.
BM25_CACHE_PATH = "./storage/bm25_index.pkl"

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


class InMemoryBM25Index:
    def __init__(self, cfg: BM25Config):
        self.k1 = cfg.k1
        self.b = cfg.b
        self.chunk_ids: list[str] = []
        self.doc_ids: list[str] = []
        self.token_counts: list[Counter] = []
        self.doc_lens: list[int] = []
        self._df: Counter = Counter()
        self.avgdl: float = 0.0
        self.idf: dict[str, float] = {}
        self._finalized = False
        self._chunk_set: set[str] = set()  # for O(1) dedup check

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def add(self, chunk_id: str, doc_id: str, text: str) -> bool:
        """Add a chunk. Returns False if already present (dedup)."""
        if chunk_id in self._chunk_set:
            return False
        tokens = tokenize(text)
        self.chunk_ids.append(chunk_id)
        self.doc_ids.append(doc_id)
        c = Counter(tokens)
        self.token_counts.append(c)
        self.doc_lens.append(len(tokens))
        self._df.update(c.keys())
        self._chunk_set.add(chunk_id)
        self._finalized = False
        return True

    def add_many(self, items: Iterable[tuple[str, str, str]]) -> int:
        added = 0
        for chunk_id, doc_id, text in items:
            if self.add(chunk_id, doc_id, text):
                added += 1
        return added

    def remove_doc(self, doc_id: str) -> int:
        """Remove all chunks of a document. Returns count removed."""
        indices = [i for i, d in enumerate(self.doc_ids) if d == doc_id]
        if not indices:
            return 0
        # Remove in reverse order to preserve indices
        for i in reversed(indices):
            cid = self.chunk_ids[i]
            # Decrement df for this chunk's terms
            for term in self.token_counts[i]:
                self._df[term] -= 1
                if self._df[term] <= 0:
                    del self._df[term]
            self.chunk_ids.pop(i)
            self.doc_ids.pop(i)
            self.token_counts.pop(i)
            self.doc_lens.pop(i)
            self._chunk_set.discard(cid)
        self._finalized = False
        return len(indices)

    def finalize(self) -> None:
        n = len(self.chunk_ids)
        if n == 0:
            self.avgdl = 0.0
            self.idf = {}
            self._finalized = True
            return
        self.avgdl = sum(self.doc_lens) / n
        self.idf = {term: math.log((n - df + 0.5) / (df + 0.5) + 1.0) for term, df in self._df.items()}
        self._finalized = True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Pickle the index to disk."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        data = {
            "k1": self.k1,
            "b": self.b,
            "chunk_ids": self.chunk_ids,
            "doc_ids": self.doc_ids,
            "token_counts": self.token_counts,
            "doc_lens": self.doc_lens,
            "df": self._df,
            "avgdl": self.avgdl,
            "idf": self.idf,
            "chunk_set": self._chunk_set,
            "finalized": self._finalized,
        }
        with open(tmp, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
        log.info("BM25 index saved: %d chunks → %s", len(self), p)

    @classmethod
    def load(cls, path: str | Path, cfg: BM25Config) -> InMemoryBM25Index | None:
        """Load from disk. Returns None if file missing or corrupt."""
        p = Path(path)
        if not p.exists():
            return None
        try:
            with open(p, "rb") as f:
                data = pickle.load(f)
            idx = cls(cfg)
            idx.chunk_ids = data["chunk_ids"]
            idx.doc_ids = data["doc_ids"]
            idx.token_counts = data["token_counts"]
            idx.doc_lens = data["doc_lens"]
            idx._df = data["df"]
            idx.avgdl = data["avgdl"]
            idx.idf = data["idf"]
            idx._chunk_set = data.get("chunk_set", set(idx.chunk_ids))
            idx._finalized = data.get("finalized", True)
            log.info("BM25 index loaded: %d chunks ← %s", len(idx), p)
            return idx
        except Exception as e:
            log.warning("BM25 index load failed (%s), will rebuild: %s", p, e)
            return None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _score_chunk(self, i: int, q_tokens: list[str]) -> float:
        tc = self.token_counts[i]
        dl = self.doc_lens[i]
        if dl == 0:
            return 0.0
        score = 0.0
        norm = self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
        for q in q_tokens:
            tf = tc.get(q, 0)
            if tf == 0:
                continue
            idf = self.idf.get(q, 0.0)
            if idf <= 0:
                continue
            score += idf * (tf * (self.k1 + 1)) / (tf + norm)
        return score

    def search_chunks(
        self,
        query: str,
        top_k: int,
        *,
        allowed_doc_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """
        Score every chunk in the index, optionally limiting candidates to
        those whose doc_id is in ``allowed_doc_ids`` (pre-filter). When
        ``allowed_doc_ids`` is None, no scoping is applied.
        """
        if not self._finalized:
            self.finalize()
        q_tokens = tokenize(query)
        if not q_tokens or not self.chunk_ids:
            return []
        scoped = allowed_doc_ids is not None
        scored: list[tuple[int, float]] = []
        for i in range(len(self.chunk_ids)):
            if scoped and self.doc_ids[i] not in allowed_doc_ids:
                continue
            s = self._score_chunk(i, q_tokens)
            if s > 0:
                scored.append((i, s))
        scored.sort(key=lambda kv: -kv[1])
        return [(self.chunk_ids[i], s) for i, s in scored[:top_k]]

    def search_docs(
        self,
        query: str,
        top_k: int,
        *,
        allowed_doc_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return top doc_ids by max chunk BM25 within each doc, optionally
        restricted to a whitelist of doc_ids."""
        if not self._finalized:
            self.finalize()
        q_tokens = tokenize(query)
        if not q_tokens or not self.chunk_ids:
            return []
        scoped = allowed_doc_ids is not None
        best: dict[str, float] = {}
        for i in range(len(self.chunk_ids)):
            did = self.doc_ids[i]
            if scoped and did not in allowed_doc_ids:
                continue
            s = self._score_chunk(i, q_tokens)
            if s <= 0:
                continue
            if s > best.get(did, 0.0):
                best[did] = s
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        return ranked[:top_k]

    def __len__(self) -> int:
        return len(self.chunk_ids)

    def __contains__(self, chunk_id: str) -> bool:
        return chunk_id in self._chunk_set


# ---------------------------------------------------------------------------
# Builder (relocated from retrieval/pipeline.py during agent cutover)
# ---------------------------------------------------------------------------


def build_bm25_index(
    relational,
    cfg: BM25Config,
    *,
    doc_ids: list[str] | None = None,
    cache_path: str = BM25_CACHE_PATH,
    force_rebuild: bool = False,
) -> InMemoryBM25Index:
    """Build or load the on-disk BM25 index.

    1. Try loading from disk cache (fast, <50ms for 10K chunks).
    2. If cache is missing/corrupt or ``force_rebuild`` is True:
       full rebuild from the relational store's ``chunks`` table.
    3. Save to disk for next startup.

    Incremental updates: after ingesting a new doc, call
    ``index.add(...) + index.finalize() + index.save(cache_path)``
    instead of a full rebuild.

    Used by ``api.state.AppState`` at startup and by ingestion
    to refresh after each parse-version commit. Originally lived
    in ``retrieval/pipeline.py``; relocated here so the agent
    path doesn't depend on the (now-deleted) RetrievalPipeline.
    """
    # Try cache first.
    if not force_rebuild and cache_path:
        cached = InMemoryBM25Index.load(cache_path, cfg)
        if cached is not None and len(cached) > 0:
            return cached

    # Full rebuild.
    t0 = time.time()
    index = InMemoryBM25Index(cfg)

    if doc_ids is None:
        doc_ids = _list_all_doc_ids(relational)

    for doc_id in doc_ids:
        row = relational.get_document(doc_id)
        if not row:
            continue
        pv = row["active_parse_version"]
        chunks = relational.get_chunks(doc_id, pv)
        for c in chunks:
            section = " ".join(c.get("section_path") or [])
            text = c.get("content") or ""
            if section:
                text = section + "\n" + text
            index.add(
                chunk_id=c["chunk_id"],
                doc_id=c["doc_id"],
                text=text,
            )
    index.finalize()
    elapsed = int((time.time() - t0) * 1000)
    log.info(
        "BM25 index built: %d chunks, %d docs, %dms",
        len(index),
        len(set(index.doc_ids)),
        elapsed,
    )

    if cache_path:
        try:
            index.save(cache_path)
        except Exception as e:
            log.warning("BM25 cache save failed: %s", e)

    return index


def _list_all_doc_ids(relational) -> list[str]:
    """All doc_ids in the relational store. Used by ``build_bm25_index``
    when the caller didn't pass an explicit list.

    Stores that don't expose ``list_document_ids`` (older test
    doubles) get an empty list — caller passes an explicit doc_ids
    arg in that case.
    """
    lister = getattr(relational, "list_document_ids", None)
    if callable(lister):
        return list(lister())
    return []
