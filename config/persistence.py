"""
Persistence configuration.

ForgeRAG production runtime requires PostgreSQL.  SQLite remains
available for the test suite only (see TESTING_ALLOW_SQLITE env var);
MySQL has been removed entirely.

    relational.backend:  "postgres"  (production)
    vector.backend:      "pgvector" | "chromadb" | "qdrant" | "milvus" | "weaviate"

Credentials: prefer postgres.password_env over plaintext password.
The store factory reads the env var at connect time.

NOTE: The SQLiteConfig class is retained so pytest fixtures can still
construct ephemeral in-memory DBs for fast unit tests, but the main
config validator rejects sqlite in normal runtime.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Relational
# ---------------------------------------------------------------------------


class PostgresConfig(BaseModel):
    host: str = "localhost"
    port: int = 5432
    database: str = "forgerag"
    user: str = "forgerag"
    password: str = ""
    password_env: str | None = None
    pool_min: int = 2
    pool_max: int = 10
    connect_timeout: int = 10


class SQLiteConfig(BaseModel):
    path: str = "./storage/forgerag.db"
    # sqlite3 connection kwargs
    timeout: float = 10.0
    # WAL mode gives much better concurrency
    journal_mode: Literal["delete", "truncate", "wal", "memory"] = "wal"
    synchronous: Literal["off", "normal", "full"] = "normal"


import os


class RelationalConfig(BaseModel):
    """
    Production: postgres only.

    sqlite is still accepted inside the test-suite (env var
    TESTING_ALLOW_SQLITE=1 set by pytest fixtures); anywhere else it
    raises. MySQL support has been removed.
    """

    backend: Literal["postgres", "sqlite"] = "postgres"
    postgres: PostgresConfig | None = Field(default_factory=PostgresConfig)
    sqlite: SQLiteConfig | None = None
    schema_auto_init: bool = True

    @model_validator(mode="after")
    def _check_section(self) -> RelationalConfig:
        if self.backend == "postgres" and self.postgres is None:
            self.postgres = PostgresConfig()
        if self.backend == "sqlite":
            # Test-only escape hatch; reject in production environments.
            if os.environ.get("TESTING_ALLOW_SQLITE") != "1":
                raise ValueError(
                    "relational.backend=sqlite is test-only. Production "
                    "ForgeRAG runs on PostgreSQL. Set TESTING_ALLOW_SQLITE=1 "
                    "to use SQLite from a pytest fixture."
                )
            if self.sqlite is None:
                self.sqlite = SQLiteConfig()
        return self


# ---------------------------------------------------------------------------
# Vector
# ---------------------------------------------------------------------------


class PgvectorConfig(BaseModel):
    dimension: int = 1536
    index_type: Literal["hnsw", "ivfflat", "none"] = "hnsw"
    distance: Literal["cosine", "l2", "ip"] = "cosine"
    # HNSW tuning
    hnsw_m: int = 16
    hnsw_ef_construction: int = 64


class ChromaConfig(BaseModel):
    mode: Literal["persistent", "http"] = "persistent"
    persist_directory: str = "./storage/chroma"
    http_host: str = "localhost"
    http_port: int = 8001
    collection_name: str = "forgerag_chunks"
    dimension: int = 1536
    distance: Literal["cosine", "l2", "ip"] = "cosine"


class QdrantConfig(BaseModel):
    url: str = "http://localhost:6333"
    api_key: str | None = None
    collection_name: str = "forgerag_chunks"
    dimension: int = 1536
    distance: Literal["cosine", "l2", "ip"] = "cosine"
    prefer_grpc: bool = False
    timeout: int = 30


class MilvusConfig(BaseModel):
    uri: str = "http://localhost:19530"
    token: str | None = None
    collection_name: str = "forgerag_chunks"
    dimension: int = 1536
    distance: Literal["cosine", "l2", "ip"] = "cosine"
    index_type: Literal["HNSW", "IVF_FLAT", "FLAT"] = "HNSW"


class WeaviateConfig(BaseModel):
    url: str = "http://localhost:8080"
    api_key: str | None = None
    collection_name: str = "ForgeragChunks"
    dimension: int = 1536
    distance: Literal["cosine", "l2", "dot"] = "cosine"


class VectorConfig(BaseModel):
    backend: Literal["pgvector", "chromadb", "qdrant", "milvus", "weaviate"] = "pgvector"
    pgvector: PgvectorConfig | None = Field(default_factory=PgvectorConfig)
    chromadb: ChromaConfig | None = None
    qdrant: QdrantConfig | None = None
    milvus: MilvusConfig | None = None
    weaviate: WeaviateConfig | None = None

    @model_validator(mode="after")
    def _check_section(self) -> VectorConfig:
        if self.backend == "pgvector" and self.pgvector is None:
            self.pgvector = PgvectorConfig()
        if self.backend == "chromadb" and self.chromadb is None:
            raise ValueError("vector.backend=chromadb but vector.chromadb section missing")
        if self.backend == "qdrant" and self.qdrant is None:
            self.qdrant = QdrantConfig()
        if self.backend == "milvus" and self.milvus is None:
            self.milvus = MilvusConfig()
        if self.backend == "weaviate" and self.weaviate is None:
            self.weaviate = WeaviateConfig()
        return self


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


class PersistenceConfig(BaseModel):
    relational: RelationalConfig = Field(default_factory=RelationalConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)

    @model_validator(mode="after")
    def _validate_combo(self) -> PersistenceConfig:
        if self.vector.backend == "pgvector" and self.relational.backend != "postgres":
            raise ValueError(
                f"Invalid combination: {self.relational.backend} + pgvector. "
                "Only postgres has pgvector; use vector.backend=chromadb instead."
            )
        return self
