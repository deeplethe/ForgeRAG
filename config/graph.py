"""Graph database configuration for Knowledge Graph storage.

ForgeRAG production runtime requires **Neo4j 5.11+**. NetworkX is
retained for the test suite only (fast in-memory fixture). Production
use of NetworkX is refused by the config validator because:

  - It does not support concurrent writes (multi-worker FastAPI
    deployments race on the JSON dump file).
  - It has no native vector index; similarity search must be done
    in Python which doesn't scale past ~10k entities.
  - Cypher-based path-scoped queries (Phase 1 pre-filter) are
    impossible to express cleanly without the real engine.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class NetworkXConfig(BaseModel):
    path: str = "./storage/kg.json"


class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""
    password_env: str | None = "NEO4J_PASSWORD"
    database: str = "neo4j"


class EntityDisambiguationConfig(BaseModel):
    """Embedding-based entity deduplication at upsert time."""

    enabled: bool = False
    similarity_threshold: float = 0.85
    candidate_top_k: int = 10


class GraphConfig(BaseModel):
    # Neo4j is the production default. `networkx` is only accepted when
    # TESTING_ALLOW_NETWORKX=1 (set by pytest conftest.py).
    backend: Literal["neo4j", "networkx"] = "neo4j"
    networkx: NetworkXConfig = Field(default_factory=NetworkXConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    entity_disambiguation: EntityDisambiguationConfig = Field(default_factory=EntityDisambiguationConfig)

    @model_validator(mode="after")
    def _reject_networkx_in_production(self) -> GraphConfig:
        if self.backend == "networkx" and os.environ.get("TESTING_ALLOW_NETWORKX") != "1":
            raise ValueError(
                "graph.backend=networkx is test-only. Production ForgeRAG "
                "requires Neo4j 5.11+ (multi-worker safety + native vector "
                "index + Cypher for path-scoped KG retrieval). "
                "Set TESTING_ALLOW_NETWORKX=1 to use NetworkX from a "
                "pytest fixture, or switch backend to 'neo4j'."
            )
        return self
