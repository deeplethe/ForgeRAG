"""
Embedder protocol and factory.

The Embedder interface is intentionally small:

    embed_texts(texts) -> list[list[float]]
    embed_chunks(chunks) -> dict[chunk_id, vector]

Batching, retry, and backend-specific quirks live inside each
implementation. Callers only see a pure-function-shaped API.

Chunk-level embedding rules:
    - For content_type in {"image", "formula", "code"}, we prefix the
      content with a short tag ("[image] ", "[formula] ", "[code] ")
      so the vector captures the modality signal; this is a cheap
      trick that helps cross-type retrieval.
    - content_type == "table" uses the markdown payload as-is.
    - Empty or whitespace-only chunks are skipped (embedding would
      be noise); the returned dict will simply not contain them.
"""

from __future__ import annotations

from typing import Protocol

from config import EmbedderConfig
from parser.schema import Chunk


class Embedder(Protocol):
    backend: str
    dimension: int
    batch_size: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    def embed_chunks(self, chunks: list[Chunk]) -> dict[str, list[float]]:
        """
        Convenience wrapper: extract text per chunk_id, batch
        through embed_texts, return {chunk_id: vector}.
        """
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_embedder(cfg: EmbedderConfig) -> Embedder:
    if cfg.backend == "litellm":
        from .litellm import LiteLLMEmbedder

        assert cfg.litellm is not None
        return LiteLLMEmbedder(cfg)
    if cfg.backend == "sentence_transformers":
        from .sentence_transformers import SentenceTransformersEmbedder

        assert cfg.sentence_transformers is not None
        return SentenceTransformersEmbedder(cfg)
    raise ValueError(f"unknown embedder backend: {cfg.backend!r}")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def chunk_to_embedding_text(chunk: Chunk) -> str:
    """
    Turn a Chunk into the text we actually embed. Keeps the rule in
    one place so every backend stays consistent.
    """
    content = (chunk.content or "").strip()
    if not content:
        return ""
    if chunk.content_type == "image":
        return "[image] " + content
    if chunk.content_type == "formula":
        return "[formula] " + content
    if chunk.content_type == "code":
        return "[code] " + content
    return content
