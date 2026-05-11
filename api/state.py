"""
Application state container.

Owns the long-lived collaborators (stores, pipelines, index) and
exposes them through a single attribute on the FastAPI app.

The builder path is parameterized so tests can inject fakes for
parser/tree_builder/chunker/embedder/vector_store without duplicating
wiring logic.
"""

from __future__ import annotations

# ── Privacy: turn off bundled-dependency telemetry early ─────────────
# OpenCraig is self-hosted; data should not leave the operator's
# network unless explicitly configured (LLM API calls). Several of
# our dependencies ship with anonymised telemetry on by default; we
# opt out here, before any of them get imported. ``setdefault`` means
# an operator who genuinely WANTS to enable a vendor's telemetry can
# still do so via the environment.
import os as _os
_os.environ.setdefault("LITELLM_TELEMETRY", "False")              # litellm gateway
_os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")            # ChromaDB
_os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "False")        # ChromaDB legacy
_os.environ.setdefault("DO_NOT_TRACK", "1")                        # universal opt-out
_os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")            # HuggingFace
_os.environ.setdefault("DISABLE_TELEMETRY", "1")                   # generic catch-all

import contextlib
import logging
import threading

from config import AppConfig
from embedder.base import Embedder, make_embedder
from ingestion import IngestionPipeline
from ingestion.kg_queue import KGQueue
from ingestion.queue import IngestionQueue
from parser.blob_store import BlobStore, make_blob_store
from parser.chunker import Chunker
from parser.pipeline import ParserPipeline
from parser.tree_builder import TreeBuilder
from persistence.files import FileStore
from persistence.store import Store
from persistence.vector.base import VectorStore, make_vector_store
from retrieval.bm25 import build_bm25_index
from retrieval.file_search import (
    UnifiedSearcher,
    build_filename_bm25_index,
    filename_index_path,
    remove_filename_index_for_doc,
    update_filename_index_for_doc,
)

log = logging.getLogger(__name__)


def _backfill_chroma_paths_from_sql(rel, vec) -> None:
    """Re-derive vector chunk path metadata from SQL ``Document.path``.

    Fixes a long-standing bug where ``ingestion_writer`` didn't include
    path in the chunk metadata it sent to the vector store, so every
    chunk got the ChromaStore default fallback ``"/"`` — making
    folder-scoped queries return nothing. This runs once at startup,
    walks all docs in SQL, and bulk-updates the vector store with
    correct paths via ``vec.update_paths``.

    Idempotent: if any chunk's path metadata is already a list (= a
    migration ran in a previous startup or the chunk was upserted by
    the post-fix code path), we skip. Cheap probe — single ``get(1)``.

    Currently only Chroma needs this; pgvector denormalizes path via
    its own SQL view, qdrant/etc. would each need their own version.
    """
    if vec.backend != "chromadb":
        return

    # Probe: already migrated?
    try:
        col = vec._ensure_collection()
        sample = col.get(limit=1, include=["metadatas"])
        metas = sample.get("metadatas") or []
        if metas and isinstance((metas[0] or {}).get("path"), list):
            return
    except Exception as e:
        log.warning("vector backfill probe failed: %s", e)
        return

    # Pull all chunk_id → doc.path pairs from SQL in one query.
    from sqlalchemy import select

    from persistence.models import ChunkRow, Document

    chunk_to_path: dict[str, str] = {}
    with rel.transaction() as sess:
        rows = sess.execute(
            select(ChunkRow.chunk_id, Document.path).join(Document, Document.doc_id == ChunkRow.doc_id)
        ).all()
        for r in rows:
            if r.path:
                chunk_to_path[r.chunk_id] = r.path

    if not chunk_to_path:
        return

    vec.update_paths(chunk_to_path)
    log.info(
        "vector backfill: re-derived path metadata for %d chunks from SQL",
        len(chunk_to_path),
    )


class AppState:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        parser: ParserPipeline | None = None,
        tree_builder: TreeBuilder | None = None,
        chunker: Chunker | None = None,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
        blob_store: BlobStore | None = None,
    ):
        self.cfg = cfg

        # LLM response cache (ingest-side only). Installed early so
        # every downstream collaborator that calls ``cached_completion``
        # picks up the global cache automatically. Skipped when the
        # config flag is off; falls back to plain ``litellm.completion``.
        from opencraig import llm_cache as _llm_cache_mod

        _llm_cache_mod.install(cfg.cache.llm)

        # Relational store (authoritative metadata)
        self.store = Store(cfg.persistence.relational)
        self.store.connect()
        self.store.ensure_schema()

        # yaml is the single source of truth; the DB carries a one-way
        # mirror so read-only admin views can see the effective state.
        # Each module already owns its model/api_key/api_base inline —
        # no provider-id indirection layer to resolve.
        from config.settings_manager import snapshot_to_db

        # Mirror yaml → DB (backup only, never read back at runtime).
        snapshot_to_db(cfg, self.store)

        # Blob store for figures + uploaded files
        self.blob: BlobStore = blob_store or make_blob_store(cfg.storage.to_dataclass())

        # File ingestion (files table + blob dedup)
        self.file_store = FileStore(cfg.files, self.blob, self.store)

        # Parser stack (injectable so tests can supply in-memory fakes)
        self.parser: ParserPipeline = parser or ParserPipeline.from_config(cfg)
        self.tree_builder: TreeBuilder = tree_builder or TreeBuilder(cfg.parser.tree_builder)
        self.chunker: Chunker = chunker or Chunker(cfg.parser.chunker)

        # Embedder (with optional disk cache) + vector store
        base_embedder = embedder or make_embedder(cfg.embedder)
        if embedder is None and cfg.cache.embedding_cache:
            from embedder.cached import CachedEmbedder

            self.embedder: Embedder = CachedEmbedder(
                base_embedder,
                cache_path=cfg.cache.embedding_path,
            )
        else:
            self.embedder = base_embedder
        if vector_store is not None:
            self.vector: VectorStore = vector_store
        else:
            self.vector = make_vector_store(cfg.persistence.vector, relational_store=self.store)
            self.vector.connect()
            self.vector.ensure_schema()

        # One-shot path backfill: legacy chunks were upserted without
        # path metadata (the old ``ingestion_writer`` didn't include it,
        # so the ChromaStore default fallback ``"/"`` filled in for every
        # chunk → folder-scoped queries returned nothing). Re-derive
        # each chunk's path from SQL ``Document.path`` and write it to
        # the vector store via ``update_paths``. Idempotent: probes one
        # chunk's metadata; skips if already in list-form (= migrated).
        try:
            _backfill_chroma_paths_from_sql(self.store, self.vector)
        except Exception as e:
            log.warning("vector path backfill from SQL failed: %s", e)

        # Graph store (Knowledge Graph — optional)
        self.graph_store = None
        try:
            from graph.factory import make_graph_store

            self.graph_store = make_graph_store(cfg.graph)
            log.info("graph store initialized: %s", cfg.graph.backend)

            # Always wrap with entity disambiguation (no cfg toggle anymore —
            # tune via entity_disambiguation.similarity_threshold instead).
            try:
                from graph.disambiguator import DisambiguatingGraphStore, EntityDisambiguator

                disambiguator = EntityDisambiguator(
                    embedder=self.embedder,
                    threshold=cfg.graph.entity_disambiguation.similarity_threshold,
                    candidate_top_k=cfg.graph.entity_disambiguation.candidate_top_k,
                )
                existing = self.graph_store.get_all_entities()
                disambiguator.load_existing(existing)
                self.graph_store = DisambiguatingGraphStore(self.graph_store, disambiguator)
                log.info(
                    "entity disambiguation enabled (threshold=%.2f, cached=%d)",
                    cfg.graph.entity_disambiguation.similarity_threshold,
                    len(existing),
                )
            except Exception as e:
                log.warning("entity disambiguation init failed: %s", e)
        except Exception as e:
            log.warning("graph store not available: %s", e, exc_info=True)

        # Ingestion orchestrator. ``kg_submit`` is wired below after we
        # construct the dedicated KG worker pool.
        self.ingestion = IngestionPipeline(
            file_store=self.file_store,
            parser=self.parser,
            tree_builder=self.tree_builder,
            chunker=self.chunker,
            relational_store=self.store,
            vector_store=self.vector,
            embedder=self.embedder,
            graph_store=self.graph_store,
            kg_extraction_cfg=cfg.retrieval.kg_extraction,
        )

        # SQLite is a single-writer engine; even with WAL + busy_timeout
        # we observed ``database is locked`` failures under N-way parallel
        # ingestion (each worker fires multiple status / chunk / block
        # writes per pipeline stage, and 12 workers easily stack past the
        # 30s busy window). Clamp both pools to a single worker when the
        # relational backend is SQLite — Postgres keeps the configured
        # parallelism. This trades wall-clock for reliability; users who
        # want concurrency should switch to the PG backend.
        is_sqlite = cfg.persistence.relational.backend == "sqlite"
        ingest_workers = 1 if is_sqlite else cfg.parser.ingest_max_workers
        kg_workers = 1 if is_sqlite else cfg.parser.kg_max_workers
        if is_sqlite and (
            cfg.parser.ingest_max_workers > 1 or cfg.parser.kg_max_workers > 1
        ):
            log.info(
                "SQLite backend: clamping ingest workers %d→%d, KG workers %d→%d "
                "(switch to backend=postgres for parallel ingestion)",
                cfg.parser.ingest_max_workers, ingest_workers,
                cfg.parser.kg_max_workers, kg_workers,
            )

        # KG-extraction worker pool — separate from the parse/embed pool
        # so long-running KG jobs (minutes per doc) can't starve incoming
        # parse jobs. Created before ingest_queue so we can wire its
        # submit callback into the pipeline.
        self.kg_queue = KGQueue(
            self.ingestion.run_kg_for_doc,
            max_workers=kg_workers,
        )
        self.kg_queue.start()
        self.ingestion.kg_submit = self.kg_queue.submit

        # Background parse/embed queue
        self.ingest_queue = IngestionQueue(
            self.ingestion,
            max_workers=ingest_workers,
            on_complete=self._on_ingest_complete,
        )
        self.ingest_queue.start()

        # Re-queue documents that were stuck mid-ingestion when the
        # process last exited (crash, restart, worker recycled by uvicorn).
        self.ingest_queue.recover_stuck(self.store)

        # Same for KG jobs that were in flight or merely queued — those
        # also can't survive a process restart but their docs already
        # passed parse+embed so we resubmit only to the KG pool.
        try:
            kg_recovered = self.store.recover_stuck_kg()
            for doc_id in kg_recovered:
                self.kg_queue.submit(doc_id)
            if kg_recovered:
                log.info("re-queued %d stuck KG job(s)", len(kg_recovered))
        except Exception:
            log.exception("KG recovery failed")

        # BM25 index is lazy -- built on first chat / search call.
        self._init_lock = threading.RLock()
        self._bm25 = None
        self._filename_bm25 = None
        self._unified_search: UnifiedSearcher | None = None
        # Search-page query translator (LRU-cached small-model
        # cross-lingual expansion). Lazy: first /search call wires
        # it up. None when ``cfg.search.translation.enabled`` is
        # false — callers must None-check.
        self._query_translator = None

        # Reranker — eager since the agent's ``rerank`` tool reads
        # ``state.reranker`` directly. Cheap to construct (just stores
        # config); first call to .rerank() does the actual work.
        try:
            from retrieval.rerank import make_reranker

            self.reranker = make_reranker(cfg.retrieval.rerank)
        except Exception as e:
            log.warning("reranker init failed: %s — rerank tool will return error", e)
            self.reranker = None

        # Web search providers + cache — opt-in, multi-provider.
        # ``web_search_providers`` is a dict keyed by provider name
        # (``tavily`` / ``brave``); empty when no provider is
        # configured. Each one is attempted independently so a
        # deployment with both tavily AND brave keys gets BOTH
        # providers usable in parallel — the agent picks one per
        # call (or compares via separate MCP tool entries).
        #
        # ``web_search_provider`` stays as a back-compat alias
        # pointing at the default-named entry (or any one if the
        # default isn't configured). Callers that haven't been
        # updated for multi-provider still see a single provider.
        self.web_search_providers: dict[str, Any] = {}
        self.web_search_provider = None
        self.web_search_cache = None
        try:
            from retrieval.web_search import (
                WebSearchCache,
                make_web_search_provider,
            )

            ws_cfg = getattr(cfg, "web_search", None)
            if ws_cfg is not None and getattr(ws_cfg, "enabled", False):
                # Try to build every provider that has a config section
                # populated. Per-provider failures (missing key /
                # section) get logged but don't disable the others.
                candidates: list[str] = []
                if getattr(ws_cfg, "tavily", None) is not None:
                    candidates.append("tavily")
                if getattr(ws_cfg, "brave", None) is not None:
                    candidates.append("brave")
                for name in candidates:
                    try:
                        self.web_search_providers[name] = (
                            make_web_search_provider(ws_cfg, provider=name)
                        )
                    except Exception as e:
                        log.info(
                            "web_search: %s provider not configured (%s); skipping",
                            name, e,
                        )
                # Back-compat single-provider alias: default if present,
                # else any one. ``None`` keeps the existing "not
                # configured" branch in single-provider callers.
                default_name = getattr(ws_cfg, "default_provider", "tavily")
                self.web_search_provider = (
                    self.web_search_providers.get(default_name)
                    or next(iter(self.web_search_providers.values()), None)
                )
                if self.web_search_providers:
                    self.web_search_cache = WebSearchCache(
                        max_entries=getattr(ws_cfg, "cache_size", 256),
                        ttl_seconds=getattr(ws_cfg, "cache_ttl_seconds", 300),
                    )
                    log.info(
                        "web_search providers initialized: %s (default=%s)",
                        list(self.web_search_providers.keys()),
                        getattr(self.web_search_provider, "name", None),
                    )
        except Exception as e:
            log.warning("web_search init failed: %s", e)

        # Multi-user authorization. Stateless once constructed; the
        # store reference is enough. Built eagerly because ``can()`` is
        # cheap and request-time creation would race the auth
        # middleware on the first request.
        from .auth.authz import AuthorizationService

        self.authz = AuthorizationService(self.store)

        # ── Phase 2 agent sandbox ──
        # ``sandbox`` is the per-user Docker container manager
        # (``persistence.sandbox_manager.SandboxManager``). When
        # available, the chat route dispatches the Claude SDK runtime
        # in-container (agent has full filesystem tools operating on
        # the bind-mounted user workdir); when ``None``, the route
        # falls back to in-process Claude SDK with built-in toolsets
        # disabled (degraded but functional Q&A).
        #
        # We try to construct it eagerly here. Failures (docker SDK
        # not installed, daemon unreachable, image missing) leave
        # ``sandbox = None`` and the chat route picks up the
        # fallback automatically — the deployment can run without
        # Docker; the Workspace UX is just degraded.
        #
        # Future: this layer is sandbox-agnostic. Swapping Docker for
        # microVM (Firecracker / gVisor / Kata) only requires a new
        # backend class with the same SandboxManager surface — the
        # AgentTaskHandle layer above does not know the sandbox kind.
        self.sandbox = self._try_init_sandbox()

        # ── Long-task / HITL: active agent run registry ──
        # In-memory map of run_id → AgentTaskHandle for currently-active
        # agent runs (status in {running, approval_wait, ask_human_wait,
        # paused}). Populated by the /send route, drained by close() on
        # terminal state. The /stream route uses this to find the live
        # event source for a conversation; /feedback uses it to deliver
        # approval/answer/interrupt to the right run.
        #
        # NOT persistent — backend restart wipes this; the lifespan
        # reconcile pass scans agent_runs for orphans and marks them
        # crashed. Type is forward-imported as Any so this file doesn't
        # depend on api.agent.task_handle (avoids a circular import
        # path when the runtime imports state).
        self.active_runs: dict[str, Any] = {}

        # Provider-agnostic per-LLM-call usage hook. LiteLLM normalises
        # ``ModelResponse.usage`` across Anthropic / DeepSeek / OpenAI /
        # Bedrock / vLLM / ollama, so a single success_callback fed
        # back into ``handle.add_usage`` lets budget enforcement land
        # mid-run regardless of which provider is configured. See
        # ``api/agent/llm_usage_callback.py``.
        try:
            from api.agent.llm_usage_callback import (
                init_litellm_usage_callback,
            )
            init_litellm_usage_callback(self)
        except Exception:
            log.exception("AppState: litellm usage callback init failed")

    # ------------------------------------------------------------------
    def _try_init_sandbox(self):
        """Best-effort SandboxManager construction.

        Returns a live SandboxManager when:
          * the docker SDK is importable AND
          * the daemon is reachable AND
          * the configured sandbox image exists (we don't pull
            it — the operator did ``scripts/build-sandbox.sh``)

        Returns ``None`` otherwise. The chat route checks this and
        falls back to in-process Claude SDK (toolsets disabled, MCP
        domain-tools only) so the deployment stays functional even
        without Docker.

        Logs at INFO when sandbox is up, at WARNING when we'd have
        liked to use Docker but couldn't — operators see in startup
        logs which mode they're in.
        """
        import logging as _logging

        _log = _logging.getLogger(__name__)

        try:
            import docker
        except ImportError:
            _log.info(
                "sandbox: docker SDK not installed — the SDK will run "
                "in-process (Workbench agent tools degraded; install "
                "the docker package + run scripts/build-sandbox.sh "
                "for the full experience)"
            )
            return None

        # Defence against the ``./docker/`` namespace-package shadow:
        # when the real SDK isn't installed, PEP 420 lets Python treat
        # the local Dockerfile-material folder as the ``docker`` module
        # and the bare ``import docker`` above succeeds — but the
        # module is empty. Distinguish that case from a real install
        # so the operator's log says "SDK missing" not "daemon
        # unreachable" (the misleading message that masked this same
        # bug for hours when the venv's docker package got cleared).
        if not hasattr(docker, "from_env"):
            _log.info(
                "sandbox: docker SDK not installed (an empty ``./docker/`` "
                "namespace package was loaded instead) — the SDK will run "
                "in-process. Install the docker package: "
                "``pip install docker`` (already in requirements.txt; "
                "rerun ``pip install -r requirements.txt`` if your venv "
                "lost it)."
            )
            return None

        try:
            from persistence.sandbox_manager import (
                DEFAULT_SANDBOX_IMAGE,
                DockerBackend,
                SandboxManager,
            )
        except Exception:
            _log.exception("sandbox: SandboxManager import failed")
            return None

        try:
            client = docker.from_env()
            client.ping()  # cheap reachability check
        except Exception as e:
            _log.info(
                "sandbox: docker daemon unreachable (%s: %s) — "
                "in-process fallback active. Make sure Docker Desktop "
                "is running, or set DOCKER_HOST=tcp://your-host:2375 "
                "for a remote daemon.",
                type(e).__name__,
                e,
            )
            return None

        # Image presence check — skip the manager construction if the
        # operator hasn't built the sandbox image yet. Pulling it on
        # demand would be a 2 GB surprise; better to be loud about
        # the missing-image state.
        image = DEFAULT_SANDBOX_IMAGE
        try:
            client.images.get(image)
        except Exception:
            _log.warning(
                "sandbox: image %r not found locally — Workspace agent "
                "tools degraded. Run ``scripts/build-sandbox.sh`` to "
                "build it; the chat route will pick up the change on "
                "next backend restart.",
                image,
            )
            return None

        try:
            user_workdirs_root = (
                getattr(self.cfg.agent, "user_workdirs_root", None)
                or "./storage/user-workdirs"
            )
            mgr = SandboxManager(
                backend=DockerBackend(client),
                image=image,
                projects_root=getattr(
                    self.cfg.agent, "projects_root", "./storage/projects"
                ),
                user_envs_root="./storage/user-envs",
                user_workdirs_root=user_workdirs_root,
            )
        except Exception:
            _log.exception("sandbox: SandboxManager construction failed")
            return None

        _log.info(
            "sandbox: agent-in-container path active (image=%s, "
            "user_workdirs_root=%s)",
            image,
            user_workdirs_root,
        )
        return mgr

    # ------------------------------------------------------------------
    def _ensure_indices(self) -> None:
        """Build the BM25 + filename BM25 indices on first need.

        Replaces the old ``_ensure_retrieval`` (which also built the
        defunct RetrievalPipeline). The agent path reads
        ``state._bm25`` directly via the ``search_bm25`` tool, and
        ``state.unified_search`` consumes both indices for the
        BM25-only ``/search`` endpoint.
        """
        if self._bm25 is not None and self._filename_bm25 is not None:
            return
        with self._init_lock:
            if self._bm25 is not None and self._filename_bm25 is not None:
                return
            cache_path = (
                self.cfg.cache.bm25_path if self.cfg.cache.bm25_persistence else ""
            )
            self._bm25 = build_bm25_index(
                self.store,
                self.cfg.retrieval.bm25,
                cache_path=cache_path,
            )
            self._filename_bm25 = build_filename_bm25_index(
                self.store,
                self.cfg.retrieval.bm25,
                cache_path=filename_index_path(self.cfg),
            )

    # ------------------------------------------------------------------
    @property
    def unified_search(self) -> UnifiedSearcher:
        """Lazy-init the ``/search`` searcher.

        ``/search`` is BM25-only (no vector / KG / tree / rerank), so
        we only need the content + filename BM25 indices.
        Constructed once per process — searchers hold no per-request
        state.
        """
        if self._unified_search is not None:
            return self._unified_search
        with self._init_lock:
            if self._unified_search is not None:
                return self._unified_search
            self._ensure_indices()
            assert self._bm25 is not None
            assert self._filename_bm25 is not None
            self._unified_search = UnifiedSearcher(
                bm25_index=self._bm25,
                filename_index=self._filename_bm25,
                rel=self.store,
            )
            return self._unified_search

    # ------------------------------------------------------------------
    @property
    def query_translator(self):
        """Lazy-init the Search-page query translator.

        Returns None when translation is disabled in config — the
        Search route falls back to the original query alone in
        that case (BM25 still runs, just no cross-lingual
        expansion). One translator instance per process; the
        LRU cache lives inside it.
        """
        tcfg = getattr(getattr(self.cfg, "search", None), "translation", None)
        if tcfg is None or not tcfg.enabled:
            return None
        if self._query_translator is not None:
            return self._query_translator
        with self._init_lock:
            if self._query_translator is not None:
                return self._query_translator
            from .search.translation import QueryTranslator

            self._query_translator = QueryTranslator(tcfg)
            return self._query_translator

    # ------------------------------------------------------------------
    def refresh_bm25(self, *, force_rebuild: bool = True) -> None:
        """Rebuild content + filename BM25 indices and persist them.

        Called from:
          * fallback after a per-doc incremental update fails
          * trash service permanent-delete path
          * /system maintenance routes
          * /documents reset / re-ingest
        Both indices are kept in sync — every callsite that needed a
        content-index rebuild also needs the filename-index to reflect
        the same delete / rename / reset.
        """
        cache_path = self.cfg.cache.bm25_path if self.cfg.cache.bm25_persistence else None
        new_bm25 = build_bm25_index(
            self.store,
            self.cfg.retrieval.bm25,
            force_rebuild=force_rebuild,
            cache_path=cache_path or "",
        )
        new_filename = build_filename_bm25_index(
            self.store,
            self.cfg.retrieval.bm25,
            force_rebuild=force_rebuild,
            cache_path=filename_index_path(self.cfg),
        )
        with self._init_lock:
            self._bm25 = new_bm25
            self._filename_bm25 = new_filename
            if self._unified_search is not None:
                self._unified_search.bm25 = new_bm25
                self._unified_search.filename_index = new_filename

    # ------------------------------------------------------------------
    def _on_ingest_complete(self, doc_id: str, error: Exception | None) -> None:
        """Called from worker thread after each ingestion job finishes."""
        if error is not None:
            return
        try:
            self._update_bm25_for_doc(doc_id)
        except Exception:
            log.exception(
                "incremental bm25 update failed for %s; falling back to full rebuild",
                doc_id,
            )
            try:
                self.refresh_bm25()
            except Exception:
                log.exception("post-ingest bm25 refresh failed")
        # Filename index update is independent — never block a content-
        # index update on a filename-index hiccup, and vice versa.
        try:
            self._update_filename_bm25_for_doc(doc_id)
        except Exception:
            log.exception(
                "incremental filename-bm25 update failed for %s; will rebuild on next restart",
                doc_id,
            )

    def _update_filename_bm25_for_doc(self, doc_id: str) -> None:
        """Replace one doc's entry in the filename index in place.

        Same lock pattern as the content index. On doc deletion the
        store returns no row and we drop the entry.
        """
        if self._filename_bm25 is None:
            return  # not built yet — first /search call will trigger a build

        doc_row = self.store.get_document(doc_id)
        with self._init_lock:
            idx = self._filename_bm25
            if idx is None:
                return
            if not doc_row:
                remove_filename_index_for_doc(idx, doc_id)
            else:
                update_filename_index_for_doc(
                    idx,
                    doc_id=doc_id,
                    filename=doc_row.get("filename") or "",
                    path=doc_row.get("path") or "",
                    format=doc_row.get("format") or "",
                )
            idx.finalize()
            self._persist_filename_bm25(idx)

    def _update_bm25_for_doc(self, doc_id: str) -> None:
        """
        Add (or replace) one document's chunks in the BM25 index without
        rescanning the rest of the corpus. ``_init_lock`` serializes index
        mutation across worker threads.
        """
        if self._bm25 is None:
            # Cold start — build the whole index once.
            self.refresh_bm25(force_rebuild=True)
            return

        doc_row = self.store.get_document(doc_id)
        if not doc_row:
            # Doc was deleted between ingest_complete and now — drop stale entries.
            with self._init_lock:
                bm25 = self._bm25
                if bm25 is None:
                    return
                bm25.remove_doc(doc_id)
                bm25.finalize()
                self._persist_bm25(bm25)
            return

        pv = doc_row["active_parse_version"]
        chunks = self.store.get_chunks(doc_id, pv)

        with self._init_lock:
            bm25 = self._bm25
            if bm25 is None:
                # Lost the index between the early check and now (concurrent
                # full rebuild). Skip — that rebuild already covers this doc.
                return
            # Replace prior chunks for this doc (re-ingest with bumped
            # parse_version leaves the old chunk_ids stale).
            bm25.remove_doc(doc_id)
            for c in chunks:
                section = " ".join(c.get("section_path") or [])
                text = c.get("content") or ""
                if section:
                    text = section + "\n" + text
                bm25.add(chunk_id=c["chunk_id"], doc_id=c["doc_id"], text=text)
            bm25.finalize()
            self._persist_bm25(bm25)
            total = len(bm25)
        log.info("bm25 incremental: doc=%s chunks=%d total=%d", doc_id, len(chunks), total)

    def _persist_bm25(self, bm25) -> None:
        cache_path = self.cfg.cache.bm25_path if self.cfg.cache.bm25_persistence else None
        if not cache_path:
            return
        try:
            bm25.save(cache_path)
        except Exception as e:
            log.warning("bm25 cache save failed: %s", e)

    def _persist_filename_bm25(self, idx) -> None:
        cache_path = filename_index_path(self.cfg)
        if not cache_path:
            return
        try:
            idx.save(cache_path)
        except Exception as e:
            log.warning("filename bm25 cache save failed: %s", e)

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        # Stop the parse/embed queue first — this prevents new KG jobs
        # from being submitted while we drain the KG queue below.
        try:
            self.ingest_queue.shutdown()
        except Exception:
            log.exception("ingestion queue shutdown failed")
        # Stop KG queue (long-running jobs; longer drain timeout).
        try:
            self.kg_queue.shutdown(timeout=60.0)
        except Exception:
            log.exception("KG queue shutdown failed")
        # Save embedding cache
        if hasattr(self.embedder, "save"):
            with contextlib.suppress(Exception):
                self.embedder.save()
        # Save BM25 cache
        if self._bm25 is not None:
            try:
                cache_path = self.cfg.cache.bm25_path if self.cfg.cache.bm25_persistence else None
                if cache_path:
                    self._bm25.save(cache_path)
            except Exception:
                pass
        # Save filename BM25 cache (sibling of the content index)
        if self._filename_bm25 is not None:
            try:
                fp = filename_index_path(self.cfg)
                if fp:
                    self._filename_bm25.save(fp)
            except Exception:
                pass
        # Close graph store
        if self.graph_store is not None:
            try:
                self.graph_store.close()
            except Exception:
                log.exception("graph store close failed")
        try:
            self.store.close()
        except Exception:
            log.exception("store close failed")
        try:
            self.vector.close()
        except Exception:
            log.exception("vector close failed")
