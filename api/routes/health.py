"""GET /api/v1/health and /api/v1/health/components"""

from fastapi import APIRouter, Depends

from ..deps import get_state
from ..health_registry import get_registry
from ..schemas import HealthResponse
from ..state import AppState

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(state: AppState = Depends(get_state)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        components={
            "relational": state.store.backend,
            "vector": state.vector.backend,
            "blob": state.blob.mode,
            "embedder": state.embedder.backend,
        },
        counts={
            "documents": state.store.count_documents(),
            "files": state.store.count_files(),
        },
    )


@router.get("/health/components")
def health_components(state: AppState = Depends(get_state)) -> dict:
    """
    Per-component health snapshot for the UI architecture dashboard.

    Merges two sources:
      1. The live health registry (last-known status of each pipeline
         component, populated as calls happen).
      2. Static config-derived state for components that haven't been
         called yet — we can still tell whether they are enabled.
    """
    reg_snap = get_registry().snapshot()

    # Config-derived state: lets UI show "disabled" / "unknown" even
    # for components that have never been invoked since server start.
    cfg = state.cfg
    r = cfg.retrieval

    def _enabled_or_disabled(enabled: bool) -> str:
        return "unknown" if enabled else "disabled"

    # KG paths are gated on graph_store presence (not on a cfg toggle).
    has_graph = state.graph_store is not None
    components = {
        "reranker": {"status": "unknown"},
        "embedder": {"status": "unknown"},
        "vector_path": {"status": _enabled_or_disabled(r.vector.enabled)},
        "bm25_path": {"status": _enabled_or_disabled(r.bm25.enabled)},
        "tree_path": {"status": _enabled_or_disabled(r.tree_path.enabled)},
        "kg_path": {"status": "unknown" if has_graph else "disabled"},
        "kg_extraction": {"status": "unknown" if has_graph else "disabled"},
        "query_understanding": {"status": "unknown"},
        "tree_navigator": {"status": _enabled_or_disabled(r.tree_path.enabled and r.tree_path.llm_nav_enabled)},
        "answer_generator": {"status": "unknown"},
    }

    # Overlay live data (healthy / error / degraded trumps config default)
    for name, live in reg_snap.items():
        # Don't let live data downgrade 'disabled' — if feature is off,
        # stale 'error' from a prior session shouldn't light up red.
        existing = components.get(name, {})
        if existing.get("status") == "disabled":
            components[name] = existing
            continue
        components[name] = live

    return {"components": components}
