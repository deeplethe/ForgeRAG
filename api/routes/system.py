"""
/api/v1/system — operational endpoints for the frontend.

    POST /api/v1/system/rebuild-bm25       force rebuild BM25 index
    GET  /api/v1/system/retrieval-status    which paths are enabled
    POST /api/v1/system/test-connection     test LLM/embedding API
    GET  /api/v1/system/stats               global statistics
    POST /api/v1/system/restart             apply config + restart server
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import get_state
from ..state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/system", tags=["system"])


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


@router.post("/restart")
def restart_server(state: AppState = Depends(get_state)):
    """Apply DB settings then restart the server process.

    Works by sending SIGTERM to the current process after a short delay.
    Uvicorn's process manager (or Docker/systemd) will restart it.
    With ``--workers N``, only the handling worker dies and is respawned.
    """
    import os
    import signal
    import threading

    from config.settings_manager import apply_overrides, resolve_providers

    # Apply settings first so the restart picks them up
    count = apply_overrides(state.cfg, state.store)
    resolved = resolve_providers(state.cfg, state.store)
    log.info("restart: applied %d overrides, %d providers; sending SIGTERM in 1s", count, resolved)

    def _kill():
        import time

        time.sleep(1)  # let the HTTP response go out first
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_kill, daemon=True).start()
    return {"status": "restarting", "applied": count, "providers_resolved": resolved}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RetrievalStatus(BaseModel):
    vector_enabled: bool
    bm25_enabled: bool
    tree_enabled: bool
    tree_llm_nav_enabled: bool
    query_understanding_enabled: bool
    rerank_enabled: bool
    descendant_expansion_enabled: bool
    sibling_expansion_enabled: bool
    crossref_expansion_enabled: bool
    kg_enabled: bool
    kg_extraction_enabled: bool


class TestConnectionRequest(BaseModel):
    target: str = Field(
        ...,
        description="Which connection to test: 'embedder', 'generator', 'tree_nav'",
    )


class TestConnectionResponse(BaseModel):
    target: str
    success: bool
    latency_ms: int = 0
    detail: str = ""


class SystemStats(BaseModel):
    documents: int
    files: int
    chunks: int
    traces: int
    settings: int
    bm25_indexed: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/rebuild-bm25")
def rebuild_bm25(state: AppState = Depends(get_state)):
    t0 = time.time()
    state.refresh_bm25()
    ms = int((time.time() - t0) * 1000)
    n = len(state._bm25) if state._bm25 else 0
    return {"status": "ok", "chunks_indexed": n, "duration_ms": ms}


@router.get("/retrieval-status", response_model=RetrievalStatus)
def retrieval_status(state: AppState = Depends(get_state)):
    cfg = state.cfg.retrieval
    return RetrievalStatus(
        vector_enabled=cfg.vector.enabled,
        bm25_enabled=cfg.bm25.enabled,
        tree_enabled=cfg.tree_path.enabled,
        tree_llm_nav_enabled=cfg.tree_path.llm_nav_enabled,
        query_understanding_enabled=cfg.query_understanding.enabled,
        rerank_enabled=cfg.rerank.enabled,
        descendant_expansion_enabled=cfg.merge.descendant_expansion_enabled,
        sibling_expansion_enabled=cfg.merge.sibling_expansion_enabled,
        crossref_expansion_enabled=cfg.merge.crossref_expansion_enabled,
        kg_enabled=cfg.kg_path.enabled,
        kg_extraction_enabled=cfg.kg_extraction.enabled,
    )


@router.post("/test-connection", response_model=TestConnectionResponse)
def test_connection(
    req: TestConnectionRequest,
    state: AppState = Depends(get_state),
):
    target = req.target
    t0 = time.time()

    if target == "embedder":
        try:
            vecs = state.embedder.embed_texts(["test connection"])
            ms = int((time.time() - t0) * 1000)
            dim = len(vecs[0]) if vecs else 0
            return TestConnectionResponse(
                target=target,
                success=True,
                latency_ms=ms,
                detail=f"ok, dimension={dim}",
            )
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            return TestConnectionResponse(
                target=target,
                success=False,
                latency_ms=ms,
                detail=str(e),
            )

    if target == "generator":
        try:
            from answering.generator import make_generator

            gen = make_generator(state.cfg.answering.generator)
            result = gen.generate(
                [
                    {"role": "user", "content": "Say 'ok' in one word."},
                ]
            )
            ms = int((time.time() - t0) * 1000)
            return TestConnectionResponse(
                target=target,
                success=bool(result.get("text")),
                latency_ms=ms,
                detail=result.get("text", "")[:100],
            )
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            return TestConnectionResponse(
                target=target,
                success=False,
                latency_ms=ms,
                detail=str(e),
            )

    if target == "tree_nav":
        try:
            from retrieval.tree_navigator import LLMTreeNavigator

            tp = state.cfg.retrieval.tree_path
            nav = LLMTreeNavigator(
                model=tp.nav.model,
                api_key=tp.nav.api_key,
                api_key_env=tp.nav.api_key_env,
                api_base=tp.nav.api_base,
            )
            # Minimal test: navigate an empty tree
            nav.navigate("test", {"nodes": {}, "root_id": ""}, top_k=1)
            ms = int((time.time() - t0) * 1000)
            return TestConnectionResponse(
                target=target,
                success=True,
                latency_ms=ms,
                detail="ok",
            )
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            return TestConnectionResponse(
                target=target,
                success=False,
                latency_ms=ms,
                detail=str(e),
            )

    raise HTTPException(400, f"unknown target: {target}. Use: embedder, generator, tree_nav")


class InfrastructureInfo(BaseModel):
    storage_mode: str  # local / s3 / oss
    storage_root: str  # path or bucket
    relational_backend: str  # postgres (production); sqlite only in test fixtures
    relational_path: str  # host:port/db
    vector_backend: str  # pgvector / chromadb
    vector_detail: str  # collection or index info
    graph_backend: str = ""  # networkx / neo4j / none
    graph_detail: str = ""  # path or uri


@router.get("/infrastructure", response_model=InfrastructureInfo)
def infrastructure(state: AppState = Depends(get_state)):
    cfg = state.cfg
    # Storage
    s_mode = cfg.storage.mode
    s_root = ""
    if s_mode == "local" and cfg.storage.local:
        s_root = cfg.storage.local.root
    elif s_mode == "s3" and cfg.storage.s3:
        s_root = f"{cfg.storage.s3.bucket} ({cfg.storage.s3.endpoint})"
    elif s_mode == "oss" and cfg.storage.oss:
        s_root = f"{cfg.storage.oss.bucket} ({cfg.storage.oss.endpoint})"

    # Relational
    r_backend = cfg.persistence.relational.backend
    r_path = ""
    if r_backend == "sqlite" and cfg.persistence.relational.sqlite:
        r_path = cfg.persistence.relational.sqlite.path
    elif r_backend == "postgres" and cfg.persistence.relational.postgres:
        pg = cfg.persistence.relational.postgres
        r_path = f"{pg.host}:{pg.port}/{pg.database}"

    # Vector
    v_backend = cfg.persistence.vector.backend
    v_detail = ""
    if v_backend == "pgvector" and cfg.persistence.vector.pgvector:
        pv = cfg.persistence.vector.pgvector
        v_detail = f"dim={pv.dimension} index={pv.index_type}"
    elif v_backend == "chromadb" and cfg.persistence.vector.chromadb:
        ch = cfg.persistence.vector.chromadb
        v_detail = f"{ch.collection_name} ({ch.mode})"

    # Graph
    g_backend = cfg.graph.backend if hasattr(cfg, "graph") else "none"
    g_detail = ""
    if g_backend == "networkx" and cfg.graph.networkx:
        g_detail = cfg.graph.networkx.path
    elif g_backend == "neo4j" and cfg.graph.neo4j:
        neo = cfg.graph.neo4j
        g_detail = f"{neo.uri}/{neo.database}"

    # Stats from graph store (if available)
    gs = getattr(state, "graph_store", None)
    if gs is not None:
        try:
            gstats = gs.stats()
            g_detail += f" ({gstats.get('entities', 0)} entities, {gstats.get('relations', 0)} relations)"
        except Exception:
            pass

    return InfrastructureInfo(
        storage_mode=s_mode,
        storage_root=s_root,
        relational_backend=r_backend,
        relational_path=r_path,
        vector_backend=v_backend,
        vector_detail=v_detail,
        graph_backend=g_backend,
        graph_detail=g_detail,
    )


@router.get("/stats", response_model=SystemStats)
def system_stats(state: AppState = Depends(get_state)):
    return SystemStats(
        documents=state.store.count_documents(),
        files=state.store.count_files(),
        # Prefer a single bulk query if available; fall back to per-doc counting
        # capped at 500 documents to avoid excessive DB load (N+1).
        chunks=state.store.count_all_chunks()
        if hasattr(state.store, "count_all_chunks")
        else (
            sum(
                state.store.count_chunks(d, doc["active_parse_version"])
                for d in state.store.list_document_ids()[:500]
                if (doc := state.store.get_document(d))
            )
            if state.store.count_documents() <= 500
            else -1
        ),
        traces=state.store.count_traces(),
        settings=len(state.store.get_all_settings()),
        bm25_indexed=len(state._bm25) if state._bm25 else 0,
    )
