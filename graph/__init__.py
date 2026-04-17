"""
Knowledge Graph storage layer.

Provides a pluggable graph store abstraction for entity-relation
storage and retrieval, inspired by LightRAG.

Backends: NetworkX (dev/lightweight), Neo4j (production).
"""

from .base import Entity, GraphStore, Relation
from .factory import make_graph_store

__all__ = ["Entity", "GraphStore", "Relation", "make_graph_store"]
