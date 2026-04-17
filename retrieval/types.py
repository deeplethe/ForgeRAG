"""Shared dataclasses for the retrieval pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from parser.schema import Chunk, Citation


@dataclass
class ScoredChunk:
    """A single candidate produced by one of the retrieval paths."""

    chunk_id: str
    score: float  # raw score from the path (not comparable across paths)
    source: str  # "vector" | "tree"


@dataclass
class MergedChunk:
    """
    A candidate after RRF fusion and (optionally) context expansion.

    sources   is a set of labels that contributed this chunk.
              A chunk pulled in by both vector and tree has two entries.
              A chunk pulled in via sibling expansion has
              {"expansion:sibling"} etc.

    parent_of indicates "this chunk was pulled in because chunk_id X
              was a merge candidate" -- useful for debugging and for
              rerank to show provenance.

    chunk     is filled in after rehydration from the relational store.
    """

    chunk_id: str
    rrf_score: float
    sources: set[str] = field(default_factory=set)
    original_scores: dict[str, float] = field(default_factory=dict)
    parent_of: str | None = None
    chunk: Chunk | None = None


@dataclass
class KGContext:
    """Synthesized knowledge from the knowledge graph.

    Inspired by LightRAG's context assembly: the LLM sees not just raw
    text chunks but also *distilled* entity descriptions and relation
    descriptions produced during KG extraction.

    This "synthesized knowledge layer" provides high-level thematic
    understanding that raw chunks alone cannot convey — e.g. an entity
    mentioned across 10 chunks gets a single consolidated description.
    """

    entities: list[dict] = field(default_factory=list)
    """[{name, type, description}, ...]  — consolidated entity profiles."""

    relations: list[dict] = field(default_factory=list)
    """[{source, target, keywords, description}, ...]  — relation descriptions."""

    @property
    def is_empty(self) -> bool:
        return not self.entities and not self.relations


@dataclass
class RetrievalResult:
    query: str
    merged: list[MergedChunk]
    citations: list[Citation]
    vector_hits: list[ScoredChunk]
    tree_hits: list[ScoredChunk]
    stats: dict[str, Any] = field(default_factory=dict)
    query_plan: Any = None  # Optional[QueryPlan] from query understanding
    kg_context: KGContext | None = None  # synthesized KG knowledge for prompt injection
