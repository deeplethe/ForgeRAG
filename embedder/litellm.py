"""
LiteLLM-based embedder.

LiteLLM (https://github.com/BerriAI/litellm) provides a unified
function signature for dozens of embedding APIs: OpenAI, Azure,
Cohere, Voyage, Bedrock, Vertex, Ollama, HuggingFace Inference
Endpoints, and more. Model selection is just a string:

    "openai/text-embedding-3-small"
    "cohere/embed-multilingual-v3.0"
    "ollama/bge-m3"
    "huggingface/BAAI/bge-large-en-v1.5"

This keeps OpenCraig provider-agnostic.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from config import EmbedderConfig
from parser.schema import Chunk

from .base import chunk_to_embedding_text

log = logging.getLogger(__name__)


class LiteLLMEmbedder:
    backend = "litellm"

    def __init__(self, cfg: EmbedderConfig):
        self.cfg = cfg
        assert cfg.litellm is not None
        self.inner = cfg.litellm
        self._litellm = None  # lazy import

    @property
    def dimension(self):
        return self.cfg.dimension

    @property
    def batch_size(self):
        return self.cfg.batch_size

    # ------------------------------------------------------------------
    def _ensure_litellm(self):
        if self._litellm is not None:
            return self._litellm
        try:
            import litellm
        except ImportError as e:
            raise RuntimeError("LiteLLMEmbedder requires litellm: pip install litellm") from e
        self._litellm = litellm
        return litellm

    def _resolve_api_key(self):
        from config.auth import resolve_api_key

        return resolve_api_key(
            api_key=self.inner.api_key,
            api_key_env=self.inner.api_key_env,
            required=False,
            context="embedder.litellm",
        )

    # ------------------------------------------------------------------
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        litellm = self._ensure_litellm()

        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.cfg.batch_size):
            batch = texts[i : i + self.cfg.batch_size]
            vectors.extend(self._call_with_retry(litellm, batch))
        self._validate_dimensions(vectors)
        return vectors

    # ------------------------------------------------------------------
    def embed_chunks(self, chunks: list[Chunk]) -> dict[str, list[float]]:
        # Map chunk_id -> text, skip empty
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
        return dict(zip(ids, vectors, strict=False))

    # ==================================================================
    # Internals
    # ==================================================================

    def _call_with_retry(self, litellm, batch: list[str]) -> list[list[float]]:
        kwargs: dict[str, Any] = dict(
            model=self.inner.model,
            input=batch,
            timeout=self.inner.timeout,
            encoding_format="float",
        )
        api_key = self._resolve_api_key()
        if api_key:
            kwargs["api_key"] = api_key
        if self.inner.api_base:
            kwargs["api_base"] = self.inner.api_base
        if self.inner.requested_dimensions:
            kwargs["dimensions"] = self.inner.requested_dimensions

        last_error: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            try:
                resp = litellm.embedding(**kwargs)
                return _extract_vectors(resp)
            except Exception as e:
                last_error = e
                delay = self.cfg.retry_base_delay * (2**attempt)
                log.warning(
                    "litellm embedding attempt %d/%d failed: %s; retry in %.1fs",
                    attempt + 1,
                    self.cfg.max_retries,
                    e,
                    delay,
                )
                time.sleep(delay)
        raise RuntimeError(f"litellm embedding failed after {self.cfg.max_retries} retries: {last_error}")

    def _validate_dimensions(self, vectors: list[list[float]]) -> None:
        for v in vectors:
            if len(v) != self.cfg.dimension:
                raise RuntimeError(
                    f"embedder produced dim={len(v)} but config.dimension={self.cfg.dimension}. "
                    f"Model: {self.inner.model}"
                )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _extract_vectors(resp: Any) -> list[list[float]]:
    """
    litellm.embedding() returns an object with .data, each item
    with .embedding. Also tolerates plain dicts for provider oddities.
    """
    data = getattr(resp, "data", None)
    if data is None and isinstance(resp, dict):
        data = resp.get("data")
    if data is None:
        raise RuntimeError(f"unexpected litellm response shape: {resp!r}")
    out: list[list[float]] = []
    for item in data:
        emb = getattr(item, "embedding", None)
        if emb is None and isinstance(item, dict):
            emb = item.get("embedding")
        if emb is None:
            raise RuntimeError(f"missing 'embedding' field in response item: {item!r}")
        out.append(list(emb))
    return out
