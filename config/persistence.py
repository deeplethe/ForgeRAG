"""
Persistence configuration.

Two relational backends, both fully supported by the unified SQLAlchemy
2.0 store layer:

  * **postgres** — production default. Multi-worker safe, full SQL
                   feature set, eligible for pgvector co-location.
  * **sqlite**   — single-process WAL-mode SQLite. Good for dev / demo /
                   the test suite. Multi-worker uvicorn deployments
                   should prefer postgres because SQLite serialises all
                   writes (WAL helps reads but writers still queue).
                   Incompatible with ``vector.backend=pgvector`` —
                   pgvector lives inside postgres.

Credentials: prefer postgres.password_env over plaintext password.
The store factory reads the env var at connect time.

MySQL support was removed.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

# Module-level flag so we warn at most once per process. Without this
# every config reload (and every uvicorn worker re-import) re-emits the
# same banner — turning the startup log into noise.
_sqlite_warned = False

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
    # sqlite3 connection kwargs.
    # ``timeout`` is the SQLite busy_timeout (seconds) — how long a
    # waiting writer sleeps before giving up with "database is locked".
    # 30s is generous but matches the worst-case contention we observed
    # under 12-way parallel ingestion (each worker commits status
    # updates after parse / structure / chunk / embed / KG, and SQLite
    # serializes writers via WAL; with N workers you can briefly stack
    # N×commit-time before the queue drains). Bumping to 30 absorbed
    # those bursts without any visible queueing on the user side.
    timeout: float = 30.0
    # WAL mode gives much better concurrency
    journal_mode: Literal["delete", "truncate", "wal", "memory"] = "wal"
    synchronous: Literal["off", "normal", "full"] = "normal"


class RelationalConfig(BaseModel):
    """
    Postgres for multi-worker production; SQLite for single-process
    deployments / dev / tests. The ``Store`` (SQLAlchemy 2.0) speaks
    to both with the same code path.
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
            if self.sqlite is None:
                self.sqlite = SQLiteConfig()
            global _sqlite_warned
            if not _sqlite_warned:
                _sqlite_warned = True
                log.warning(
                    "relational.backend=sqlite selected. SQLite serialises all "
                    "writes - running uvicorn with --workers > 1 will queue writers "
                    "behind one another. Switch to PostgreSQL for multi-worker "
                    "production deployments."
                )
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
