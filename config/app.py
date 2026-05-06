"""
Top-level AppConfig.

Add new sections here as the system grows (retrieval, db, api, ...).
Each section is an independent pydantic model so unrelated modules
do not import each other's config types.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from .answering import AnsweringSection, CORSConfig
from .auth_config import AuthConfig
from .benchmark import BenchmarkConfig
from .cache import CacheConfig
from .embedder import EmbedderConfig
from .files import FilesConfig
from .graph import GraphConfig
from .images import ImageEnrichmentConfig
from .logging import LoggingConfig
from .observability import ObservabilityConfig
from .parser import ParserSection
from .persistence import PersistenceConfig
from .retrieval import RetrievalSection
from .search import SearchConfig
from .storage import StorageModel
from .tables import TableEnrichmentConfig
from .web_search import WebSearchConfig


class AppConfig(BaseModel):
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    parser: ParserSection = Field(default_factory=ParserSection)
    storage: StorageModel = Field(default_factory=StorageModel)
    files: FilesConfig = Field(default_factory=FilesConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    retrieval: RetrievalSection = Field(default_factory=RetrievalSection)
    answering: AnsweringSection = Field(default_factory=AnsweringSection)
    image_enrichment: ImageEnrichmentConfig = Field(default_factory=ImageEnrichmentConfig)
    table_enrichment: TableEnrichmentConfig = Field(default_factory=TableEnrichmentConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    benchmark: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)

    @model_validator(mode="after")
    def _validate_dimensions(self) -> AppConfig:
        """Embedder dimension must match whichever vector store is active."""
        emb_dim = self.embedder.dimension
        vec = self.persistence.vector
        if vec.backend == "pgvector" and vec.pgvector is not None and vec.pgvector.dimension != emb_dim:
            raise ValueError(
                f"embedder.dimension ({emb_dim}) != persistence.vector.pgvector.dimension ({vec.pgvector.dimension})"
            )
        if vec.backend == "chromadb" and vec.chromadb is not None and vec.chromadb.dimension != emb_dim:
            raise ValueError(
                f"embedder.dimension ({emb_dim}) != persistence.vector.chromadb.dimension ({vec.chromadb.dimension})"
            )
        return self
