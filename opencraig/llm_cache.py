"""
Disk-backed LLM response cache for INGEST-side LLM calls.

Scope deliberately narrow: only the bulk, deterministic-prompt calls
that fire at ingest time benefit from caching — KG entity/relation
extraction, tree builder (page-group / from-LLM strategies), tree-node
summary enrichment, image-enrichment VLM. These are:

    * **expensive in aggregate** (a 1000-page corpus → 20K+ extractions)
    * **deterministic given (model, messages)** at temperature ≤ 0.2
    * **commonly re-run**: ingestion crashes, prompt-tuning iterations,
      reparse on chunker config changes

Query-side calls (query_understanding, rerank, generation, tree-nav)
deliberately bypass the cache. Their inputs vary per request (user
query + retrieved context + chat history), so the hit rate would be
near zero — and a stale cached answer leaking back into a live answer
is the kind of bug you only catch in production.

Usage at call sites:

    from opencraig.llm_cache import cached_completion
    resp = cached_completion(model=..., messages=..., temperature=0.1)

If no cache is installed (config disabled, or import-time bypass for
tests), ``cached_completion`` falls through to ``litellm.completion``
with the same kwargs — same return shape, same exceptions.

Lifecycle:

    install(cfg) — call once at app startup. Idempotent; reinstalling
    with a different directory swaps caches.
    stats()       — observability snapshot {hits, misses, size}.
    clear()       — wipe the cache (returns count of evicted entries).
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

import litellm

log = logging.getLogger(__name__)


class LLMCache:
    """Thread-safe disk cache. ``set`` / ``get`` lock the diskcache
    Index database transparently — diskcache uses sqlite under the
    hood, but the diskcache.Cache instance itself is process-local
    so we still guard the in-process counters with a Lock."""

    def __init__(self, directory: str, *, size_limit_gb: float | None = None) -> None:
        # Lazy import so a CacheConfig parsed in tests doesn't drag
        # diskcache in until we actually need on-disk storage.
        import diskcache

        size_bytes = int(size_limit_gb * 1e9) if size_limit_gb and size_limit_gb > 0 else 0
        self._cache = diskcache.Cache(directory, size_limit=size_bytes or 0)
        self._directory = directory
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    def completion(self, **kwargs: Any) -> Any:
        """Drop-in replacement for ``litellm.completion`` that consults
        the disk cache first. Returns the live or cached response.

        Cache key is hashed only over fields that *change the model
        output* (model, messages, temperature, max_tokens,
        response_format). Transport / auth fields (api_key, api_base,
        timeout) are intentionally excluded so rotating an API key
        doesn't invalidate the cache.
        """
        key = self._key(kwargs)
        cached = self._cache.get(key)
        if cached is not None:
            with self._lock:
                self._hits += 1
            return cached
        with self._lock:
            self._misses += 1
        resp = litellm.completion(**kwargs)
        # diskcache pickles values. litellm.ModelResponse pickles cleanly
        # in current releases; if a future release breaks that we degrade
        # to "cache miss next time" rather than poisoning the live call.
        try:
            self._cache.set(key, resp)
        except Exception as e:
            log.warning("LLM cache write skipped: %s", e)
        return resp

    @staticmethod
    def _key(kwargs: dict) -> str:
        sig = {
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages"),
            "temperature": kwargs.get("temperature", 1.0),
            "max_tokens": kwargs.get("max_tokens"),
            "response_format": kwargs.get("response_format"),
            # ``extra_body`` carries model-specific structured params
            # (e.g. DeepSeek thinking on/off) that DO change the output.
            "extra_body": kwargs.get("extra_body"),
        }
        blob = json.dumps(sig, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "entries": len(self._cache),
                "directory": self._directory,
            }

    def clear(self) -> int:
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            return n

    def close(self) -> None:
        try:
            self._cache.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level singleton — wired up at app startup, used by ingest-side modules
# ---------------------------------------------------------------------------

_GLOBAL: LLMCache | None = None


def install(cfg) -> None:
    """Install the global cache from a ``LLMCacheSubconfig``.

    Idempotent: re-calling closes the old cache and installs a new one
    (useful in tests). Call with ``cfg.enabled = False`` to leave the
    global at None — ``cached_completion`` then falls through.
    """
    global _GLOBAL
    if _GLOBAL is not None:
        _GLOBAL.close()
        _GLOBAL = None
    if not getattr(cfg, "enabled", False):
        log.info("LLM cache disabled by config")
        return
    try:
        _GLOBAL = LLMCache(directory=cfg.directory, size_limit_gb=cfg.size_limit_gb)
        log.info(
            "LLM cache installed: dir=%s size_limit_gb=%s",
            cfg.directory,
            cfg.size_limit_gb or "unlimited",
        )
    except Exception as e:
        # Don't kill startup if diskcache is missing — just degrade
        # silently to no-cache. Logged loud enough to spot in dev.
        log.warning("LLM cache install failed (%s); falling back to no-cache", e)
        _GLOBAL = None


def cached_completion(**kwargs: Any) -> Any:
    """Ingest-side LLM call. Routes through the global cache if
    installed; otherwise behaves identically to ``litellm.completion``."""
    if _GLOBAL is None:
        return litellm.completion(**kwargs)
    return _GLOBAL.completion(**kwargs)


def stats() -> dict[str, Any] | None:
    return _GLOBAL.stats() if _GLOBAL is not None else None


def clear() -> int:
    return _GLOBAL.clear() if _GLOBAL is not None else 0
