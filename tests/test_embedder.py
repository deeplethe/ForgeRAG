"""Embedder tests. No network or model downloads -- uses fakes."""

from __future__ import annotations

import pytest

from config import (
    AppConfig,
    ChromaConfig,
    EmbedderConfig,
    LiteLLMEmbedderConfig,
    PersistenceConfig,
    PgvectorConfig,
    RelationalConfig,
    VectorConfig,
)
from embedder.base import chunk_to_embedding_text, make_embedder
from parser.schema import Chunk


def _chunk(chunk_id: str, content: str, content_type: str = "text") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="doc",
        parse_version=1,
        node_id="doc:1:n1",
        block_ids=[f"doc:1:1:{chunk_id[-1]}"],
        content=content,
        content_type=content_type,  # type: ignore[arg-type]
        page_start=1,
        page_end=1,
        token_count=10,
    )


# ---------------------------------------------------------------------------
# chunk_to_embedding_text
# ---------------------------------------------------------------------------


class TestChunkToEmbeddingText:
    def test_text_passthrough(self):
        c = _chunk("c1", "hello world", "text")
        assert chunk_to_embedding_text(c) == "hello world"

    def test_image_prefix(self):
        c = _chunk("c2", "system architecture diagram", "image")
        assert chunk_to_embedding_text(c).startswith("[image] ")

    def test_formula_prefix(self):
        c = _chunk("c3", "E = mc^2", "formula")
        assert chunk_to_embedding_text(c).startswith("[formula] ")

    def test_code_prefix(self):
        c = _chunk("c5", "def foo(): return 1", "code")
        assert chunk_to_embedding_text(c).startswith("[code] ")

    def test_empty_returns_empty(self):
        c = _chunk("c4", "   ", "text")
        assert chunk_to_embedding_text(c) == ""


# ---------------------------------------------------------------------------
# Cross-dimension validation at AppConfig level
# ---------------------------------------------------------------------------


class TestCrossDimensionValidation:
    def test_matching_dimensions_ok(self):
        # defaults are all 1536 after the bump
        cfg = AppConfig()
        assert cfg.embedder.dimension == 1536
        assert cfg.persistence.vector.pgvector.dimension == 1536

    def test_mismatch_rejected(self):
        with pytest.raises(ValueError, match="dimension"):
            AppConfig(
                embedder=EmbedderConfig(dimension=1024),
                persistence=PersistenceConfig(
                    relational=RelationalConfig(backend="postgres"),
                    vector=VectorConfig(
                        backend="pgvector",
                        pgvector=PgvectorConfig(dimension=1536),
                    ),
                ),
            )

    def test_chromadb_mismatch_rejected(self):
        with pytest.raises(ValueError, match="dimension"):
            AppConfig(
                embedder=EmbedderConfig(dimension=1024),
                persistence=PersistenceConfig(
                    relational=RelationalConfig(backend="postgres"),
                    vector=VectorConfig(
                        backend="chromadb",
                        chromadb=ChromaConfig(dimension=1536),
                    ),
                ),
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_make_litellm(self):
        cfg = EmbedderConfig(backend="litellm")
        emb = make_embedder(cfg)
        assert emb.backend == "litellm"
        assert emb.dimension == 1536
        assert emb.batch_size == 32

    def test_unknown_backend_raises(self):
        with pytest.raises(Exception):
            EmbedderConfig(backend="nope")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LiteLLM embedder with a mocked litellm module
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, vectors):
        self.data = [{"embedding": v} for v in vectors]


class TestLiteLLMEmbedder:
    def test_embed_texts_batches_and_validates_dim(self, monkeypatch):
        from embedder.litellm import LiteLLMEmbedder

        cfg = EmbedderConfig(
            backend="litellm",
            dimension=4,
            batch_size=2,
            litellm=LiteLLMEmbedderConfig(model="openai/fake"),
        )
        # Align persistence dim too so AppConfig would be valid, but we
        # instantiate the embedder directly here so that's not strictly needed.
        emb = LiteLLMEmbedder(cfg)

        calls: list[list[str]] = []

        class FakeLiteLLM:
            @staticmethod
            def embedding(**kwargs):
                batch = kwargs["input"]
                calls.append(list(batch))
                return _FakeResponse([[0.1, 0.2, 0.3, 0.4] for _ in batch])

        emb._litellm = FakeLiteLLM
        vectors = emb.embed_texts(["a", "b", "c", "d", "e"])
        assert len(vectors) == 5
        assert all(len(v) == 4 for v in vectors)
        # batch_size=2 -> 3 calls
        assert len(calls) == 3

    def test_embed_chunks_skips_empty(self):
        from embedder.litellm import LiteLLMEmbedder

        cfg = EmbedderConfig(
            backend="litellm",
            dimension=3,
            litellm=LiteLLMEmbedderConfig(model="openai/fake"),
        )
        emb = LiteLLMEmbedder(cfg)

        class FakeLiteLLM:
            @staticmethod
            def embedding(**kwargs):
                return _FakeResponse([[1.0, 2.0, 3.0] for _ in kwargs["input"]])

        emb._litellm = FakeLiteLLM

        chunks = [
            _chunk("c1", "hello"),
            _chunk("c2", ""),  # empty -> skipped
            _chunk("c3", "world"),
        ]
        result = emb.embed_chunks(chunks)
        assert set(result.keys()) == {"c1", "c3"}

    def test_dimension_mismatch_raises(self):
        from embedder.litellm import LiteLLMEmbedder

        cfg = EmbedderConfig(
            backend="litellm",
            dimension=4,
            litellm=LiteLLMEmbedderConfig(model="openai/fake"),
        )
        emb = LiteLLMEmbedder(cfg)

        class FakeLiteLLM:
            @staticmethod
            def embedding(**kwargs):
                return _FakeResponse([[0.1, 0.2]])  # dim=2, mismatch

        emb._litellm = FakeLiteLLM
        with pytest.raises(RuntimeError, match="dim="):
            emb.embed_texts(["a"])
