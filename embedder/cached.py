"""
Cached embedder wrapper.

Wraps any Embedder and adds a persistent on-disk cache keyed by
a hash of the input text. Avoids re-computing embeddings for
unchanged chunks during re-ingestion.

Cache is a JSON dict: { text_hash: vector }.
Invalidation: content changes -> different hash -> cache miss.
No TTL needed (embeddings are deterministic for a given model).

Storage: ~12 bytes per dimension x N entries. For 10K chunks at
3072-dim, that's ~300MB -- acceptable for disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path

from parser.schema import Chunk

from .base import Embedder, chunk_to_embedding_text

log = logging.getLogger(__name__)

CACHE_PATH = "./storage/embedding_cache.json"


class CachedEmbedder:
    """
    Transparent caching wrapper around any Embedder.

    Usage:
        base = make_embedder(cfg)
        embedder = CachedEmbedder(base)
        # Same API as Embedder:
        embedder.embed_texts(["hello"])
        embedder.embed_chunks(chunks)
    """

    def __init__(
        self,
        inner: Embedder,
        cache_path: str = CACHE_PATH,
        enabled: bool = True,
        max_entries: int = 100_000,
    ):
        self.inner = inner
        self.backend = inner.backend
        self._cache_path = cache_path
        self._enabled = enabled
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._dirty = False
        self._unsaved_count = 0
        self._max_entries = max_entries
        if enabled:
            self._load()

    @property
    def dimension(self):
        return self.inner.dimension

    @property
    def batch_size(self):
        return self.inner.batch_size

    # ------------------------------------------------------------------
    def _hash(self, text: str) -> str:
        return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()

    def _load(self) -> None:
        p = Path(self._cache_path)
        if not p.exists():
            return
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            self._cache = OrderedDict(data)
            log.info("embedding cache loaded: %d entries", len(self._cache))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            # Backward compat: try legacy pickle format
            try:
                import pickle

                with open(p, "rb") as f:
                    legacy = pickle.load(f)
                self._cache = OrderedDict(legacy)
                self._dirty = True  # re-save as JSON on next save
                log.info("embedding cache loaded (legacy pickle): %d entries", len(self._cache))
            except Exception as e2:
                log.warning("embedding cache load failed (pickle fallback): %s", e2)
                self._cache = OrderedDict()
        except Exception as e:
            log.warning("embedding cache load failed: %s", e)
            self._cache = OrderedDict()

    def save(self) -> None:
        if not self._dirty or not self._enabled:
            return
        p = Path(self._cache_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(self._cache), f)
            os.replace(tmp, p)
            self._dirty = False
            self._unsaved_count = 0
            log.info("embedding cache saved: %d entries", len(self._cache))
        except Exception as e:
            log.warning("embedding cache save failed: %s", e)

    # ------------------------------------------------------------------
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self._enabled:
            return self.inner.embed_texts(texts)

        results: list[list[float] | None] = [None] * len(texts)
        misses: list[tuple[int, str]] = []  # (original_index, text)

        for i, t in enumerate(texts):
            h = self._hash(t)
            cached = self._cache.get(h)
            if cached is not None:
                results[i] = cached
            else:
                misses.append((i, t))

        if misses:
            miss_texts = [t for _, t in misses]
            new_vecs = self.inner.embed_texts(miss_texts)
            if len(new_vecs) != len(misses):
                log.error(
                    "embed_texts returned %d vectors for %d inputs — possible silent truncation; padding with zeros",
                    len(new_vecs),
                    len(misses),
                )
            for (orig_i, t), vec in zip(misses, new_vecs, strict=True):
                h = self._hash(t)
                self._cache[h] = vec
                results[orig_i] = vec
                self._dirty = True
                self._unsaved_count += 1

            # Evict oldest entries if cache exceeds max size
            if len(self._cache) > self._max_entries:
                excess = len(self._cache) - self._max_entries
                keys_to_remove = list(self._cache.keys())[:excess]
                for k in keys_to_remove:
                    del self._cache[k]

        hits = len(texts) - len(misses)
        if misses:
            log.debug("embed_texts: %d hit, %d miss, %d total", hits, len(misses), len(texts))

        return results  # type: ignore

    def embed_chunks(self, chunks: list[Chunk]) -> dict[str, list[float]]:
        items: list[tuple[str, str]] = []
        for c in chunks:
            text = chunk_to_embedding_text(c)
            if text:
                items.append((c.chunk_id, text))
        if not items:
            return {}

        ids = [cid for cid, _ in items]
        texts = [t for _, t in items]
        vectors = self.embed_texts(texts)

        # Auto-save periodically
        if self._unsaved_count >= 500:
            self.save()

        return dict(zip(ids, vectors, strict=False))
