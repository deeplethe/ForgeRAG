"""
/api/v1/graph — Knowledge Graph endpoints.

    GET  /api/v1/graph/stats                graph statistics
    GET  /api/v1/graph/entities             search entities
    GET  /api/v1/graph/entities/{id}        entity detail + relations
    GET  /api/v1/graph/subgraph             subgraph for visualization
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import get_state
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/graph", tags=["graph"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class GraphStats(BaseModel):
    entities: int = 0
    relations: int = 0
    backend: str = ""


class EntityOut(BaseModel):
    entity_id: str
    name: str
    entity_type: str
    description: str
    source_doc_ids: list[str] = []
    source_chunk_ids: list[str] = []


class RelationOut(BaseModel):
    relation_id: str
    source_entity: str
    target_entity: str
    source_entity_name: str = ""
    target_entity_name: str = ""
    keywords: str
    description: str
    weight: float
    source_doc_ids: list[str] = []
    source_chunk_ids: list[str] = []


class SubgraphOut(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_graph(state: AppState):
    """Return graph_store or raise 404 if not configured."""
    gs = getattr(state, "graph_store", None)
    if gs is None:
        raise HTTPException(404, "Knowledge graph not configured")
    return gs


def _entity_to_out(e) -> EntityOut:
    return EntityOut(
        entity_id=e.entity_id,
        name=e.name,
        entity_type=e.entity_type,
        description=e.description,
        source_doc_ids=sorted(e.source_doc_ids),
        source_chunk_ids=sorted(e.source_chunk_ids),
    )


def _relation_to_out(r) -> RelationOut:
    return RelationOut(
        relation_id=r.relation_id,
        source_entity=r.source_entity,
        target_entity=r.target_entity,
        keywords=r.keywords,
        description=r.description,
        weight=r.weight,
        source_doc_ids=sorted(r.source_doc_ids),
        source_chunk_ids=sorted(r.source_chunk_ids),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=GraphStats)
def graph_stats(state: AppState = Depends(get_state)):
    gs = getattr(state, "graph_store", None)
    if gs is None:
        return GraphStats(backend="none")
    s = gs.stats()
    return GraphStats(
        entities=s.get("entities", 0),
        relations=s.get("relations", 0),
        backend=state.cfg.graph.backend,
    )


@router.get("/entities")
def search_entities(
    query: str = Query(..., min_length=1),
    top_k: int = Query(20, ge=1, le=100),
    state: AppState = Depends(get_state),
):
    gs = _require_graph(state)
    try:
        results = gs.search_entities(query, top_k=top_k)
    except Exception as e:
        log.exception("graph search failed")
        raise HTTPException(502, f"graph store error: {e}")
    return {"items": [_entity_to_out(e) for e in results]}


@router.get("/entities/{entity_id}")
def get_entity(entity_id: str, state: AppState = Depends(get_state)):
    gs = _require_graph(state)
    try:
        entity = gs.get_entity(entity_id)
    except Exception as e:
        log.exception("graph get_entity failed")
        raise HTTPException(502, f"graph store error: {e}")
    if entity is None:
        raise HTTPException(404, "entity not found")
    try:
        relations = gs.get_relations(entity_id)
    except Exception as e:
        log.exception("graph get_relations failed")
        raise HTTPException(502, f"graph store error: {e}")
    # Resolve entity names for relation endpoints
    rel_outs = []
    name_cache = {entity_id: entity.name}
    for r in relations:
        ro = _relation_to_out(r)
        for attr in ("source_entity", "target_entity"):
            eid = getattr(ro, attr)
            if eid not in name_cache:
                try:
                    e = gs.get_entity(eid)
                    name_cache[eid] = e.name if e else eid
                except Exception:
                    name_cache[eid] = eid
            setattr(ro, f"{attr}_name", name_cache[eid])
        rel_outs.append(ro)

    return {
        "entity": _entity_to_out(entity),
        "relations": rel_outs,
    }


@router.get("/subgraph", response_model=SubgraphOut)
def get_subgraph(
    entity_ids: str = Query(..., description="Comma-separated entity IDs"),
    state: AppState = Depends(get_state),
):
    gs = _require_graph(state)
    ids = [eid.strip() for eid in entity_ids.split(",") if eid.strip()]
    if not ids:
        raise HTTPException(400, "entity_ids required")
    try:
        result = gs.get_subgraph(ids)
    except Exception as e:
        log.exception("graph get_subgraph failed")
        raise HTTPException(502, f"graph store error: {e}")
    return SubgraphOut(
        nodes=result.get("nodes", []),
        edges=result.get("edges", []),
    )


@router.get("/full", response_model=SubgraphOut)
def get_full_graph(
    limit: int = Query(500, ge=1, le=5000, description="Max nodes to return"),
    state: AppState = Depends(get_state),
):
    """Return the full graph (up to *limit* nodes) for overview visualization."""
    gs = _require_graph(state)
    try:
        result = gs.get_full(limit=limit)
    except Exception as e:
        log.exception("graph get_full failed")
        raise HTTPException(502, f"graph store error: {e}")
    return SubgraphOut(
        nodes=result.get("nodes", []),
        edges=result.get("edges", []),
    )


@router.get("/by-doc/{doc_id}", response_model=SubgraphOut)
def get_graph_by_doc(doc_id: str, state: AppState = Depends(get_state)):
    """Return entities sourced from this document plus the relations
    among them. Powers the workspace doc-detail KG mini panel.
    """
    gs = _require_graph(state)
    try:
        result = gs.get_by_doc(doc_id)
    except Exception as e:
        log.exception("graph get_by_doc failed")
        raise HTTPException(502, f"graph store error: {e}")
    return SubgraphOut(
        nodes=result.get("nodes", []),
        edges=result.get("edges", []),
    )


# ---------------------------------------------------------------------------
# Orphan cleanup
# ---------------------------------------------------------------------------


@router.post("/cleanup")
def cleanup_orphans(state: AppState = Depends(get_state)):
    """Remove KG entities/relations whose source documents no longer exist."""
    gs = _require_graph(state)

    # Collect all valid doc_ids from the relational store
    all_docs = state.store.list_documents()
    valid_ids = {d["doc_id"] for d in all_docs}

    if not hasattr(gs, "cleanup_orphans"):
        raise HTTPException(501, "Graph backend does not support cleanup")

    result = gs.cleanup_orphans(valid_ids)
    log.info(
        "KG cleanup: removed %d entities, %d relations",
        result["removed_entities"],
        result["removed_relations"],
    )
    return result
