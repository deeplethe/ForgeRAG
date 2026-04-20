"""Config-level tests for the persistence layer (no DB required)."""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from config import PersistenceConfig, RelationalConfig, VectorConfig
from config.persistence import ChromaConfig, SQLiteConfig


@contextmanager
def _allow_sqlite_for_tests():
    """Test helper: toggle the TESTING_ALLOW_SQLITE flag so the config
    validator accepts sqlite inside pytest. Production code always rejects."""
    original = os.environ.get("TESTING_ALLOW_SQLITE")
    os.environ["TESTING_ALLOW_SQLITE"] = "1"
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TESTING_ALLOW_SQLITE", None)
        else:
            os.environ["TESTING_ALLOW_SQLITE"] = original


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

    def test_sqlite_under_test_env(self):
        """sqlite is only allowed when TESTING_ALLOW_SQLITE=1 is set."""
        with _allow_sqlite_for_tests():
            cfg = RelationalConfig(backend="sqlite", sqlite=SQLiteConfig())
            assert cfg.backend == "sqlite"
            assert cfg.sqlite is not None


class TestInvalidCombinations:
    def test_sqlite_rejected_in_production(self):
        """Without TESTING_ALLOW_SQLITE the config validator must reject sqlite."""
        original = os.environ.pop("TESTING_ALLOW_SQLITE", None)
        try:
            with pytest.raises(ValueError, match="test-only"):
                RelationalConfig(backend="sqlite", sqlite=SQLiteConfig())
        finally:
            if original is not None:
                os.environ["TESTING_ALLOW_SQLITE"] = original

    def test_chromadb_without_section_rejected(self):
        with pytest.raises(ValueError, match="chromadb section missing"):
            VectorConfig(backend="chromadb", chromadb=None)
