"""Graph database configuration for Knowledge Graph storage.

Two backends, full feature parity at the ``GraphStore`` protocol level:

  * **neo4j**    — production default. Multi-worker safe (no file
                   contention), native vector index, Cypher prefix
                   queries for path-scoped retrieval. Recommended when
                   running with ``--workers > 1`` or KG > 10K entities.
  * **networkx** — single-process JSON-persisted graph. Fully feature
                   complete (search_entities_by_embedding, update_paths,
                   path-prefix filtering, etc. — all implemented in
                   Python). Suitable for **single-worker** deployments,
                   demos, dev, and the test suite. Not safe with
                   multiple uvicorn workers writing concurrently — they
                   race on the JSON dump file. NOT enforced by the
                   schema; deploy responsibly.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

# Module-level flag so the multi-worker warning fires once per process,
# not once per config reload (validators re-run on every load).
_networkx_warned = False


class NetworkXConfig(BaseModel):
    path: str = "./storage/kg.json"


class Neo4jConfig(BaseModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: str = ""
    password_env: str | None = "NEO4J_PASSWORD"
    database: str = "neo4j"


class EntityDisambiguationConfig(BaseModel):
    """Embedding-based entity deduplication at upsert time.

    No ``enabled`` toggle: when a ``graph_store`` is configured, the
    KG path always wraps it in ``DisambiguatingGraphStore``. Tune the
    threshold lower to merge less aggressively if false-positives appear.
    """

    similarity_threshold: float = 0.85
    candidate_top_k: int = 10


class GraphConfig(BaseModel):
    # Neo4j is the production default; networkx is allowed for
    # single-worker deployments (see module docstring for the
    # operational caveat).
    backend: Literal["neo4j", "networkx"] = "neo4j"
    networkx: NetworkXConfig = Field(default_factory=NetworkXConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)
    entity_disambiguation: EntityDisambiguationConfig = Field(default_factory=EntityDisambiguationConfig)

    @model_validator(mode="after")
    def _warn_on_networkx(self) -> GraphConfig:
        if self.backend == "networkx":
            global _networkx_warned
            if not _networkx_warned:
                _networkx_warned = True
                log.warning(
                    "graph.backend=networkx selected. This backend is single-"
                    "worker only - running uvicorn with --workers > 1 will "
                    "corrupt the JSON dump on concurrent writes. Switch to "
                    "Neo4j for multi-worker production deployments."
                )
        return self
