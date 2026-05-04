"""
ForgeRAG top-level configuration package.

Config is intentionally kept outside any single module because it is
consumed by many layers: parser, retrieval, storage, API, etc.

Typical usage:

    from config import load_config
    cfg = load_config("opencraig.yaml")
    cfg.parser.backend                  # "pymupdf" | "mineru" | "mineru-vlm"
    cfg.storage.mode
"""

from .answering import AnsweringSection, GeneratorConfig
from .app import AppConfig
from .benchmark import BenchmarkConfig
from .embedder import (
    EmbedderConfig,
    LiteLLMEmbedderConfig,
    SentenceTransformersConfig,
)
from .files import FilesConfig
from .graph import GraphConfig, Neo4jConfig, NetworkXConfig
from .loader import load_config
from .logging import LoggingConfig, setup_logging
from .parser import (
    BackendsConfig,
    ChunkerConfig,
    MinerUConfig,
    NormalizeConfig,
    ParserSection,
    PyMuPDFConfig,
    TreeBuilderConfig,
)
from .persistence import (
    ChromaConfig,
    MilvusConfig,
    PersistenceConfig,
    PgvectorConfig,
    PostgresConfig,
    QdrantConfig,
    RelationalConfig,
    SQLiteConfig,
    VectorConfig,
    WeaviateConfig,
)
from .retrieval import (
    BM25Config,
    CitationsConfig,
    KGExtractionConfig,
    KGPathConfig,
    MergeConfig,
    RerankConfig,
    RetrievalSection,
    TreePathConfig,
    VectorSearchConfig,
)
from .storage import (
    LocalStorageModel,
    OSSStorageModel,
    S3StorageModel,
    StorageModel,
)
from .web_search import (
    BraveConfig,
    TavilyConfig,
    WebSearchCacheConfig,
    WebSearchConfig,
    WebSearchCostConfig,
)

__all__ = [
    "AnsweringSection",
    "AppConfig",
    "BM25Config",
    "BackendsConfig",
    "BenchmarkConfig",
    "ChromaConfig",
    "ChunkerConfig",
    "CitationsConfig",
    "EmbedderConfig",
    "FilesConfig",
    "GeneratorConfig",
    "GraphConfig",
    "KGExtractionConfig",
    "KGPathConfig",
    "LiteLLMEmbedderConfig",
    "LocalStorageModel",
    "LoggingConfig",
    "MergeConfig",
    "MilvusConfig",
    "MinerUConfig",
    "Neo4jConfig",
    "NetworkXConfig",
    "NormalizeConfig",
    "OSSStorageModel",
    "ParserSection",
    "PersistenceConfig",
    "PgvectorConfig",
    "PostgresConfig",
    "PyMuPDFConfig",
    "QdrantConfig",
    "RelationalConfig",
    "RerankConfig",
    "RetrievalSection",
    "S3StorageModel",
    "SQLiteConfig",
    "SentenceTransformersConfig",
    "StorageModel",
    "TreeBuilderConfig",
    "TreePathConfig",
    "VectorConfig",
    "VectorSearchConfig",
    "WeaviateConfig",
    "BraveConfig",
    "TavilyConfig",
    "WebSearchCacheConfig",
    "WebSearchConfig",
    "WebSearchCostConfig",
    "load_config",
    "setup_logging",
]
