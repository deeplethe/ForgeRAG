"""Config-level tests for the persistence layer (no DB required)."""

from __future__ import annotations

import pytest

from config import PersistenceConfig, RelationalConfig, VectorConfig
from config.persistence import ChromaConfig, SQLiteConfig


class TestDefaults:
    def test_default_is_postgres_pgvector(self):
        cfg = PersistenceConfig()
        assert cfg.relational.backend == "postgres"
        assert cfg.vector.backend == "pgvector"
        assert cfg.relational.postgres is not None
        assert cfg.vector.pgvector is not None


class TestValidCombinations:
    def test_postgres_chromadb(self):
        cfg = PersistenceConfig(
            relational=RelationalConfig(backend="postgres"),
            vector=VectorConfig(
                backend="chromadb",
                chromadb=ChromaConfig(persist_directory="/tmp/x"),
            ),
        )
        assert cfg.vector.backend == "chromadb"

    def test_sqlite_accepted_directly(self):
        """SQLite is a first-class backend now (single-worker deployments)."""
        cfg = RelationalConfig(backend="sqlite", sqlite=SQLiteConfig())
        assert cfg.backend == "sqlite"
        assert cfg.sqlite is not None

    def test_sqlite_autofills_section(self):
        """Mirror of the postgres autofill: backend=sqlite without a body
        gets the default SQLiteConfig populated by the validator."""
        cfg = RelationalConfig(backend="sqlite")
        assert cfg.sqlite is not None
        assert cfg.sqlite.path.endswith(".db")


class TestInvalidCombinations:
    def test_sqlite_with_pgvector_rejected(self):
        """pgvector is in-database, so it requires backend=postgres.
        SQLite users must pick chroma / qdrant / milvus / weaviate."""
        with pytest.raises(ValueError, match="pgvector"):
            PersistenceConfig(
                relational=RelationalConfig(backend="sqlite", sqlite=SQLiteConfig()),
                vector=VectorConfig(backend="pgvector"),
            )

    def test_chromadb_without_section_rejected(self):
        with pytest.raises(ValueError, match="chromadb section missing"):
            VectorConfig(backend="chromadb", chromadb=None)
