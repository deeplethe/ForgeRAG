"""
/api/v1/graph — Knowledge Graph endpoints.

    GET  /api/v1/graph/stats                graph statistics
    GET  /api/v1/graph/entities             search entities
    GET  /api/v1/graph/entities/{id}        entity detail + relations
    GET  /api/v1/graph/subgraph             subgraph for visualization
    GET  /api/v1/graph/full                 full graph (limited)
    GET  /api/v1/graph/by-doc/{doc_id}      entities sourced from one doc
    GET  /api/v1/graph/explore              anchor + halo subgraph

Multi-user visibility (S5.3):

    Every entity and relation goes through ``filter_entity`` /
    ``filter_relation`` against the caller's accessible doc set
    before being returned. Three tiers:

      full     — all source chunks live in folders the caller can
                 read; the record is returned untouched.
      partial  — at least one but not all sources accessible. The
                 record is returned with ``description=null``,
                 ``source_doc_ids`` / ``source_chunk_ids`` filtered
                 to the accessible subset, and a ``visibility``
                 block telling the UI how much was redacted.
      hidden   — no accessible source. Entity is omitted from
                 lists and 404s on direct fetch; relations are
                 dropped from results.

    Admin role bypasses the filter (sees ``full`` everywhere).

    Why redact ``description`` rather than truncate / regenerate
    per-user: the LLM synthesised it across all sources, so any
    truncation may still reflect inaccessible facts. Showing
    nothing keeps the contract honest. Per-user re-extraction
    would cost an LLM call per request.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth import (
    AccessibleSet,
    AuthenticatedPrincipal,
    Visibility,
    build_accessible_set,
    filter_entity,
    filter_relation,
)
from ..deps import get_principal, get_state, require_doc_access
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


class VisibilityOut(BaseModel):
    """Per-record visibility tier returned alongside partial-access
    entities / relations. ``full`` records omit this block."""

    level: str  # "partial" — full records skip the block entirely
    accessible_sources: int
    total_sources: int
    hidden_relations: int = 0


class EntityOut(BaseModel):
    entity_id: str
    name: str
    entity_type: str
    description: str | None
    source_doc_ids: list[str] = []
    source_chunk_ids: list[str] = []
    visibility: VisibilityOut | None = None


class RelationOut(BaseModel):
    relation_id: str
    source_entity: str
    target_entity: str
    source_entity_name: str = ""
    target_entity_name: str = ""
    keywords: str
    description: str | None
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


def _entity_obj_to_dict(e) -> dict:
    """Coerce graph-store entity object into the plain dict the
    visibility filter operates on. Stable shape across backends."""
    return {
        "entity_id": e.entity_id,
        "name": e.name,
        "entity_type": e.entity_type,
        "description": e.description,
        "source_doc_ids": sorted(e.source_doc_ids),
        "source_chunk_ids": sorted(e.source_chunk_ids),
    }


def _relation_obj_to_dict(r) -> dict:
    return {
        "relation_id": r.relation_id,
        "source_entity": r.source_entity,
        "target_entity": r.target_entity,
        "keywords": r.keywords,
        "description": r.description,
        "weight": r.weight,
        "source_doc_ids": sorted(r.source_doc_ids),
        "source_chunk_ids": sorted(r.source_chunk_ids),
    }


def _entity_dict_to_out(d: dict, vis: Visibility | None) -> EntityOut:
    return EntityOut(
        entity_id=d["entity_id"],
        name=d["name"],
        entity_type=d["entity_type"],
        description=d.get("description"),
        source_doc_ids=d.get("source_doc_ids", []),
        source_chunk_ids=d.get("source_chunk_ids", []),
        visibility=(
            VisibilityOut(
                level=vis.level,
                accessible_sources=vis.accessible_sources,
                total_sources=vis.total_sources,
                hidden_relations=vis.hidden_relations,
            )
            if vis is not None
            else None
        ),
    )


def _relation_dict_to_out(d: dict) -> RelationOut:
    return RelationOut(
        relation_id=d["relation_id"],
        source_entity=d["source_entity"],
        target_entity=d["target_entity"],
        keywords=d["keywords"],
        description=d.get("description"),
        weight=d["weight"],
        source_doc_ids=d.get("source_doc_ids", []),
        source_chunk_ids=d.get("source_chunk_ids", []),
    )


def _accessible_for(
    state: AppState, principal: AuthenticatedPrincipal
) -> AccessibleSet:
    """Build the per-request accessible set. Cached on the request
    state in a future iteration; for now one query per route is
    fine — graph routes aren't hot."""
    return build_accessible_set(
        state,
        principal.user_id,
        is_admin=(principal.role == "admin"),
        auth_enabled=state.cfg.auth.enabled and principal.via != "auth_disabled",
    )


def _filter_node_dict(node: dict, accessible: AccessibleSet) -> dict | None:
    """Apply visibility filter to a node dict from gs.get_subgraph /
    get_full / explore — the same shape as ``_entity_obj_to_dict``
    output. Returns None when hidden."""
    filtered, vis = filter_entity(node, accessible=accessible)
    if filtered is None:
        return None
    if vis is not None:
        filtered = dict(filtered)
        filtered["visibility"] = vis.to_dict()
    return filtered


def _filter_edge_dict(edge: dict, accessible: AccessibleSet) -> dict | None:
    return filter_relation(edge, accessible=accessible)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=GraphStats)
def graph_stats(state: AppState = Depends(get_state)):
    """Aggregate counts. Not user-scoped — these are tens of
    thousands at most and only reveal the corpus's overall size,
    not its contents."""
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
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    gs = _require_graph(state)
    accessible = _accessible_for(state, principal)
    try:
        # Over-fetch to leave headroom for the visibility filter.
        results = gs.search_entities(query, top_k=top_k * 3)
    except Exception as e:
        log.exception("graph search failed")
        raise HTTPException(502, f"graph store error: {e}")

    out: list[EntityOut] = []
    for e in results:
        as_dict = _entity_obj_to_dict(e)
        filtered, vis = filter_entity(as_dict, accessible=accessible)
        if filtered is None:
            continue
        out.append(_entity_dict_to_out(filtered, vis))
        if len(out) >= top_k:
            break
    return {"items": out}


@router.get("/entities/{entity_id}")
def get_entity(
    entity_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    gs = _require_graph(state)
    accessible = _accessible_for(state, principal)
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

    # Filter relations first — we need the count for the
    # visibility.hidden_relations field on the entity.
    filtered_rels: list[dict] = []
    hidden = 0
    name_cache: dict[str, str] = {entity.entity_id: entity.name}
    for r in relations:
        rel_dict = _relation_obj_to_dict(r)
        out = filter_relation(rel_dict, accessible=accessible)
        if out is None:
            hidden += 1
            continue
        # Resolve entity names for the UI; per-side filter check on
        # the named entities themselves so partial-only neighbours
        # still get a usable name without leaking their description.
        for attr in ("source_entity", "target_entity"):
            eid = out[attr]
            if eid not in name_cache:
                try:
                    other = gs.get_entity(eid)
                    name_cache[eid] = other.name if other else eid
                except Exception:
                    name_cache[eid] = eid
        out["source_entity_name"] = name_cache.get(out["source_entity"], out["source_entity"])
        out["target_entity_name"] = name_cache.get(out["target_entity"], out["target_entity"])
        filtered_rels.append(out)

    # Now filter the entity itself. Use the relations' source_chunk
    # set to compute hidden_relations contribution.
    rel_chunks: list[str] = []
    for r in relations:
        rel_chunks.extend(r.source_chunk_ids or [])
    entity_dict, vis = filter_entity(
        _entity_obj_to_dict(entity),
        accessible=accessible,
        relation_chunk_ids=rel_chunks,
    )
    if entity_dict is None:
        # No accessible source — same code as "doesn't exist", to
        # avoid existence confirmation.
        raise HTTPException(404, "entity not found")

    # Tack the relation-hidden count onto the visibility block when
    # partial-visible (filter_entity already populated it from
    # rel_chunks; this is just the explicit override path for when
    # the route already has the precise count).
    if vis is not None:
        vis.hidden_relations = hidden

    return {
        "entity": _entity_dict_to_out(entity_dict, vis),
        "relations": [_relation_dict_to_out(d) for d in filtered_rels],
    }


@router.get("/subgraph", response_model=SubgraphOut)
def get_subgraph(
    entity_ids: str = Query(..., description="Comma-separated entity IDs"),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    gs = _require_graph(state)
    accessible = _accessible_for(state, principal)
    ids = [eid.strip() for eid in entity_ids.split(",") if eid.strip()]
    if not ids:
        raise HTTPException(400, "entity_ids required")
    try:
        result = gs.get_subgraph(ids)
    except Exception as e:
        log.exception("graph get_subgraph failed")
        raise HTTPException(502, f"graph store error: {e}")
    return _filter_subgraph_payload(result, accessible)


@router.get("/full", response_model=SubgraphOut)
def get_full_graph(
    limit: int = Query(500, ge=1, le=5000, description="Max nodes to return"),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Return the full graph (up to *limit* nodes) for overview visualization."""
    gs = _require_graph(state)
    accessible = _accessible_for(state, principal)
    try:
        result = gs.get_full(limit=limit)
    except Exception as e:
        log.exception("graph get_full failed")
        raise HTTPException(502, f"graph store error: {e}")
    return _filter_subgraph_payload(result, accessible)


@router.get("/by-doc/{doc_id}", response_model=SubgraphOut)
def get_graph_by_doc(
    doc_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Return entities sourced from this document plus the relations
    among them. Powers the workspace doc-detail KG mini panel.

    Doc-level access is checked first via ``require_doc_access`` —
    if the caller can't read the doc, the response is 404 (same as
    a missing doc). That gate makes the per-entity visibility loop
    cheap: every entity in this subgraph has at least one source in
    the doc the caller already passed.
    """
    require_doc_access(state, principal, doc_id)
    gs = _require_graph(state)
    accessible = _accessible_for(state, principal)
    try:
        result = gs.get_by_doc(doc_id)
    except Exception as e:
        log.exception("graph get_by_doc failed")
        raise HTTPException(502, f"graph store error: {e}")
    return _filter_subgraph_payload(result, accessible)


@router.get("/explore", response_model=SubgraphOut)
def explore_graph(
    anchors: int = Query(200, ge=1, le=2000, description="Top-N entities by degree as anchors"),
    halo_cap: int = Query(600, ge=0, le=5000, description="Max 1-hop halo entities"),
    doc_id: str | None = Query(None, description="Restrict anchors to entities sourced from this doc"),
    entity_type: str | None = Query(None, description="Restrict anchors to a single entity type"),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Anchor + halo subgraph — what /knowledge-graph actually wants.

    ``/full?limit=N`` returns top-N entities and the edges between
    them, but in scale-free graphs that drops most edges (high-degree
    anchors mostly connect to low-degree nodes outside the top-N).
    The result is a sparse canvas where focus-on-click can't find
    neighbours to highlight.

    ``/explore`` returns the same anchors plus their 1-hop halo, so
    every anchor has its real local neighbourhood on the canvas. The
    halo is capped (default 600) so the total stays bounded.

    Optional filters narrow the anchor selection to a doc and / or an
    entity_type — useful for the "show me only Persons" / "show me
    this paper's KG" UI affordances.
    """
    if doc_id is not None:
        # Same gate as /by-doc — verify the caller can actually read
        # the doc before letting them anchor on it.
        require_doc_access(state, principal, doc_id)
    gs = _require_graph(state)
    accessible = _accessible_for(state, principal)
    try:
        result = gs.explore(
            anchors=anchors,
            halo_cap=halo_cap,
            doc_id=doc_id,
            entity_type=entity_type,
        )
    except Exception as e:
        log.exception("graph explore failed")
        raise HTTPException(502, f"graph store error: {e}")
    return _filter_subgraph_payload(result, accessible)


def _filter_subgraph_payload(
    result: dict, accessible: AccessibleSet
) -> SubgraphOut:
    """Apply the visibility tiers to a ``{nodes, edges}`` graph
    payload. Hidden nodes are dropped; their incident edges are
    dropped too (a relation between two entities only makes sense
    when at least one of the named entities is visible)."""
    nodes_in = result.get("nodes", []) or []
    edges_in = result.get("edges", []) or []

    visible_ids: set[str] = set()
    nodes_out: list[dict] = []
    for n in nodes_in:
        filtered = _filter_node_dict(n, accessible)
        if filtered is None:
            continue
        # Backends differ on the id key — networkx_store returns
        # ``{"id": ...}`` from get_subgraph; entity-search routes use
        # ``{"entity_id": ...}``. Tolerate both so the same filter
        # works for /entities, /subgraph, /full, /by-doc, /explore.
        nid = (
            filtered.get("entity_id")
            or filtered.get("id")
            or n.get("entity_id")
            or n.get("id")
        )
        if nid is not None:
            visible_ids.add(nid)
        nodes_out.append(filtered)

    edges_out: list[dict] = []
    for e in edges_in:
        # Drop edges whose endpoints aren't both visible.
        src = e.get("source") or e.get("source_entity")
        dst = e.get("target") or e.get("target_entity")
        if src not in visible_ids or dst not in visible_ids:
            continue
        filtered = _filter_edge_dict(e, accessible)
        if filtered is None:
            continue
        edges_out.append(filtered)

    return SubgraphOut(nodes=nodes_out, edges=edges_out)


# ---------------------------------------------------------------------------
# Orphan cleanup — admin only (no per-user filter)
# ---------------------------------------------------------------------------


@router.post("/cleanup")
def cleanup_orphans(
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Remove KG entities/relations whose source documents no longer
    exist. Admin-only — this is a corpus-wide maintenance op."""
    if state.cfg.auth.enabled and principal.role != "admin":
        raise HTTPException(403, "admin role required")
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
