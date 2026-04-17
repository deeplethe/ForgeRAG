"""
Application state container.

Owns the long-lived collaborators (stores, pipelines, index) and
exposes them through a single attribute on the FastAPI app.

The builder path is parameterized so tests can inject fakes for
parser/tree_builder/chunker/embedder/vector_store without duplicating
wiring logic.
"""

from __future__ import annotations

import contextlib
import logging
import threading

from answering.pipeline import AnsweringPipeline
from config import AppConfig
from embedder.base import Embedder, make_embedder
from ingestion import IngestionPipeline
from ingestion.queue import IngestionQueue
from parser.blob_store import BlobStore, make_blob_store
from parser.chunker import Chunker
from parser.pipeline import ParserPipeline
from parser.tree_builder import TreeBuilder
from persistence.files import FileStore
from persistence.store import Store
from persistence.vector.base import VectorStore, make_vector_store
from retrieval.pipeline import RetrievalPipeline, build_bm25_index

log = logging.getLogger(__name__)


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

        # Relational store (authoritative metadata)
        self.store = Store(cfg.persistence.relational)
        self.store.connect()
        self.store.ensure_schema()

        # Seed + apply DB settings overrides + resolve provider_id → credentials
        # This MUST happen before any component reads cfg.*
        from config.settings_manager import apply_overrides, resolve_providers, seed_defaults

        seed_defaults(cfg, self.store)
        applied = apply_overrides(cfg, self.store)
        if applied:
            log.info("applied %d DB setting overrides", applied)
        resolved = resolve_providers(cfg, self.store)
        if resolved:
            log.info("resolved %d LLM providers", resolved)

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

        # Graph store (Knowledge Graph — optional)
        self.graph_store = None
        try:
            from graph.factory import make_graph_store

            self.graph_store = make_graph_store(cfg.graph)
            log.info("graph store initialized: %s", cfg.graph.backend)

            # Wrap with entity disambiguation if enabled
            if cfg.graph.entity_disambiguation.enabled:
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

        # Ingestion orchestrator
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

        # Background ingestion queue
        self.ingest_queue = IngestionQueue(
            self.ingestion,
            max_workers=cfg.parser.ingest_max_workers,
            on_complete=self._on_ingest_complete,
        )
        self.ingest_queue.start()

        # Re-queue documents that were stuck mid-ingestion when the
        # process last exited (crash, restart, worker recycled by uvicorn).
        self.ingest_queue.recover_stuck(self.store)

        # BM25 index is lazy -- built on first /ask
        self._init_lock = threading.RLock()
        self._bm25 = None
        self._retrieval: RetrievalPipeline | None = None
        self._answering: AnsweringPipeline | None = None

    # ------------------------------------------------------------------
    def _ensure_retrieval(self) -> RetrievalPipeline:
        if self._retrieval is not None:
            return self._retrieval
        with self._init_lock:
            if self._retrieval is not None:
                return self._retrieval
            cache_path = self.cfg.cache.bm25_path if self.cfg.cache.bm25_persistence else ""
            self._bm25 = build_bm25_index(
                self.store,
                self.cfg.retrieval.bm25,
                cache_path=cache_path,
            )

            # Build LLM tree navigator if configured
            tree_nav = None
            tp = self.cfg.retrieval.tree_path
            if tp.llm_nav_enabled:
                from retrieval.tree_navigator import LLMTreeNavigator

                tree_nav = LLMTreeNavigator(
                    model=tp.nav.model,
                    api_key=tp.nav.api_key,
                    api_key_env=tp.nav.api_key_env,
                    api_base=tp.nav.api_base,
                    temperature=tp.nav.temperature,
                    max_tokens=tp.nav.max_tokens,
                    timeout=tp.nav.timeout,
                    max_nodes=tp.nav.max_nodes,
                    system_prompt=tp.nav.system_prompt,
                )

            self._retrieval = RetrievalPipeline(
                self.cfg.retrieval,
                embedder=self.embedder,
                vector_store=self.vector,
                relational_store=self.store,
                bm25_index=self._bm25,
                tree_navigator=tree_nav,
                graph_store=self.graph_store,
            )
            return self._retrieval

    def _ensure_answering(self) -> AnsweringPipeline:
        if self._answering is not None:
            return self._answering
        with self._init_lock:
            if self._answering is not None:
                return self._answering
            retrieval = self._ensure_retrieval()
            self._answering = AnsweringPipeline(
                self.cfg.answering,
                retrieval=retrieval,
                store=self.store,
            )
            return self._answering

    # ------------------------------------------------------------------
    @property
    def retrieval(self) -> RetrievalPipeline:
        return self._ensure_retrieval()

    @property
    def answering(self) -> AnsweringPipeline:
        return self._ensure_answering()

    # ------------------------------------------------------------------
    def refresh_bm25(self, *, force_rebuild: bool = True) -> None:
        """Rebuild BM25 and optionally persist to disk cache."""
        cache_path = self.cfg.cache.bm25_path if self.cfg.cache.bm25_persistence else None
        new_bm25 = build_bm25_index(
            self.store,
            self.cfg.retrieval.bm25,
            force_rebuild=force_rebuild,
            cache_path=cache_path or "",
        )
        with self._init_lock:
            self._bm25 = new_bm25
            if self._retrieval is not None:
                self._retrieval.bm25 = new_bm25

    # ------------------------------------------------------------------
    def _on_ingest_complete(self, doc_id: str, error: Exception | None) -> None:
        """Called from worker thread after each ingestion job finishes."""
        if error is None:
            try:
                self.refresh_bm25()
            except Exception:
                log.exception("post-ingest bm25 refresh failed")

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        # Stop ingestion queue
        try:
            self.ingest_queue.shutdown()
        except Exception:
            log.exception("ingestion queue shutdown failed")
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
