"""
FastAPI application factory.

    python main.py                              # auto-detect config
    uvicorn api.app:app --host 0.0.0.0 --port 8000

All routes are under /api/v1/. Swagger UI at /docs, ReDoc at /redoc.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import AppConfig, load_config

# ---------------------------------------------------------------------------
# LiteLLM: surface errors with full context instead of generic advice
# ---------------------------------------------------------------------------
try:
    import traceback as _tb

    import litellm

    def _litellm_failure_hook(kwargs, exception, start_time, end_time):
        """Global hook: log every litellm failure with model + caller info."""
        model = kwargs.get("model", "?")
        api_base = kwargs.get("api_base", "?")
        caller = "unknown"
        for frame in _tb.extract_stack():
            if "site-packages" not in frame.filename and frame.filename != __file__:
                caller = f"{frame.filename}:{frame.lineno} in {frame.name}"
        logging.getLogger("forgerag.litellm").error(
            "LiteLLM FAILED | model=%s | api_base=%s | error=%s | caller=%s",
            model,
            api_base,
            exception,
            caller,
        )

    litellm.failure_callback = [_litellm_failure_hook]
    litellm.suppress_debug_info = True
    # Suppress noisy "model not mapped" cost-calculation warnings
    litellm.drop_params = True
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
except Exception:
    pass

from .routes import benchmark as benchmark_routes
from .routes import chunks as chunk_routes
from .routes import conversations as conversation_routes
from .routes import documents as document_routes
from .routes import files as file_routes
from .routes import folders as folder_routes
from .routes import graph as graph_routes
from .routes import health as health_routes
from .routes import metrics as metrics_routes
from .routes import query as query_routes
from .routes import settings as settings_routes
from .routes import system as system_routes
from .routes import traces as trace_routes
from .routes import trash as trash_routes
from .state import AppState

log = logging.getLogger(__name__)


def _run_startup_probes(state: AppState) -> None:
    """
    Call probe() on each pipeline component that has one. Results are
    recorded in the health registry; failures are logged but don't
    abort server startup — users should still be able to fix config
    via the UI even if one provider is down.

    Also runs trash auto-purge once on startup so items older than the
    retention window are cleaned without requiring a cron task.
    """
    try:
        from retrieval.rerank import make_reranker

        rr = make_reranker(state.cfg.retrieval.rerank)
        probe = getattr(rr, "probe", None)
        if callable(probe):
            try:
                probe()
                log.info("reranker probe OK (backend=%s)", state.cfg.retrieval.rerank.backend)
            except Exception as e:
                log.warning("reranker probe FAILED: %s", e)
    except Exception as e:
        log.warning("skipping reranker probe — cannot construct reranker: %s", e)

    # Startup trash auto-purge. Retention read from config or defaults to 30.
    try:
        from persistence.trash_service import TrashService

        retention = int(getattr(getattr(state.cfg, "trash", object()), "retention_days", 30))
        TrashService(state).auto_purge(retention_days=retention)
    except Exception as e:
        log.warning("trash auto-purge on startup failed: %s", e)


def create_app(
    cfg: AppConfig | None = None,
    *,
    state: AppState | None = None,
) -> FastAPI:
    if cfg is not None and state is not None:
        raise ValueError("pass either cfg or state, not both")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state is not None:
            # Logging may already be set up by main.py; ensure it is.
            from config.logging import setup_logging

            setup_logging(state.cfg.logging)
            app.state.app = state
            try:
                yield
            finally:
                pass
            return
        resolved = cfg or load_config(os.environ.get("FORGERAG_CONFIG"))
        # Ensure logging is initialised (idempotent — safe if main.py
        # already called setup_logging with the same config).
        from config.logging import setup_logging

        setup_logging(resolved.logging)

        # Observability (OTel) — must happen BEFORE AppState is built so
        # SQLAlchemy / httpx auto-instrumentation is in place when the
        # relational store and any outbound HTTP clients come up.
        from config.observability import bootstrap, instrument_app

        bootstrap(resolved.observability)
        instrument_app(app)

        built = AppState(resolved)
        app.state.app = built

        # Auth: auto-provision the first admin when enabled + DB is empty.
        # Safe no-op when auth.enabled=false or a user already exists.
        try:
            from .auth import bootstrap_if_empty

            bootstrap_if_empty(resolved, built.store)
        except Exception:
            logging.getLogger(__name__).exception("auth bootstrap failed")

        # Component probes: surface configuration errors on startup instead
        # of waiting for the first user query. Health registry records the
        # result so the Architecture UI can light up red dots immediately.
        _run_startup_probes(built)
        try:
            yield
        finally:
            built.shutdown()

    app = FastAPI(
        title="ForgeRAG",
        description="Structure-aware RAG with precise bbox citations.",
        version="0.2.3",
        lifespan=lifespan,
    )

    # CORS — configurable via yaml `cors:` section.
    # Config is not yet loaded in the lifespan, so we eagerly load it here
    # for CORS only. If no config is available, we fall back to permissive
    # defaults (origins=["*"], credentials=False).
    cors_origins = ["*"]
    cors_methods = ["*"]
    cors_headers = ["*"]
    cors_credentials = False  # must be False when origins=["*"]
    _cors_src = cfg or (state.cfg if state else None)
    if _cors_src is None:
        try:
            from config.loader import load_config as _load_cfg

            _cors_src = _load_cfg(os.environ.get("FORGERAG_CONFIG"))
        except Exception:
            _cors_src = None
    if _cors_src and hasattr(_cors_src, "cors"):
        cors_origins = _cors_src.cors.allow_origins
        cors_methods = _cors_src.cors.allow_methods
        cors_headers = _cors_src.cors.allow_headers
        cors_credentials = _cors_src.cors.allow_credentials
    # Spec requires credentials=False when origins=["*"]
    if cors_origins == ["*"]:
        cors_credentials = False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_credentials,
        allow_methods=cors_methods,
        allow_headers=cors_headers,
    )

    # ------------------------------------------------------------------
    # Read-only maintenance mode.
    #
    # During the nightly maintenance window the server may be placed in
    # read-only mode so the pending_folder_ops queue can drain without
    # new writes racing against a cross-store rename. Enable with:
    #   FORGERAG_READONLY=1
    # Queries / GETs still work; mutating HTTP methods return 503.
    # ------------------------------------------------------------------
    from fastapi import Request
    from fastapi.responses import JSONResponse

    _READONLY_ALLOW_PATHS = (
        "/api/v1/health",
        "/api/v1/system/readonly",  # toggle endpoint, if added later
        "/docs",
        "/redoc",
        "/openapi.json",
    )
    _WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    @app.middleware("http")
    async def _readonly_gate(request: Request, call_next):
        if os.environ.get("FORGERAG_READONLY") == "1":
            if request.method in _WRITE_METHODS and not any(
                request.url.path.startswith(p) for p in _READONLY_ALLOW_PATHS
            ):
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "read_only_mode",
                        "message": ("Server is in read-only maintenance mode. Writes are temporarily disabled."),
                    },
                )
        return await call_next(request)

    # Register all route modules
    app.include_router(health_routes.router)
    app.include_router(file_routes.router)
    app.include_router(document_routes.router)
    app.include_router(chunk_routes.router)
    app.include_router(conversation_routes.router)
    app.include_router(query_routes.router)
    app.include_router(system_routes.router)
    app.include_router(trace_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(graph_routes.router)
    app.include_router(benchmark_routes.router)
    app.include_router(folder_routes.router)
    app.include_router(trash_routes.router)
    app.include_router(metrics_routes.router)
    from .routes import auth as auth_routes

    app.include_router(auth_routes.router)

    # ── Auth middleware (no-op when auth.enabled=false) ──────────────
    # Installed LAST so it wraps all routes above. The middleware reads
    # AppState via app.state.app which is populated in the lifespan; it
    # gracefully lets requests through until that appears.
    from .auth import AuthMiddleware

    def _state_getter(request):
        return getattr(request.app.state, "app", None)

    app.add_middleware(AuthMiddleware, state_getter=_state_getter)

    # ------------------------------------------------------------------
    # Serve frontend static files if web/ directory exists.
    # Place your frontend build output into web/ (with index.html
    # and assets/ sub-directory). SPA fallback: any path not matched
    # by /api/v1/* returns index.html for client-side routing.
    # ------------------------------------------------------------------
    # Serve frontend build from web/dist/. The web/ directory holds
    # the frontend project source; web/dist/ holds the build output.
    _dist_dir = os.path.join(os.path.dirname(__file__), "..", "web", "dist")
    if os.path.isdir(_dist_dir):
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        _assets_dir = os.path.join(_dist_dir, "assets")
        if os.path.isdir(_assets_dir):
            app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

        _index = os.path.join(_dist_dir, "index.html")
        if os.path.isfile(_index):

            @app.get("/{full_path:path}", include_in_schema=False)
            async def _spa_fallback(full_path: str):
                if full_path.startswith(("api/", "docs", "redoc", "openapi")):
                    from fastapi import HTTPException

                    raise HTTPException(404)
                file_path = os.path.join(_dist_dir, full_path)
                # Prevent path traversal outside the dist directory
                real = os.path.realpath(file_path)
                if not real.startswith(os.path.realpath(_dist_dir)):
                    from fastapi import HTTPException

                    raise HTTPException(status_code=404)
                if full_path and os.path.isfile(file_path):
                    return FileResponse(file_path)
                return FileResponse(_index)

    return app


# Module-level app for `uvicorn api.app:app`
app = create_app()
