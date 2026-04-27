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
from ingestion.kg_queue import KGQueue
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

        # KG-extraction worker pool — separate from the parse/embed pool
        # so long-running KG jobs (minutes per doc) can't starve incoming
        # parse jobs. Created before ingest_queue so we can wire its
        # submit callback into the pipeline.
        self.kg_queue = KGQueue(
            self.ingestion.run_kg_for_doc,
            max_workers=cfg.parser.kg_max_workers,
        )
        self.kg_queue.start()
        self.ingestion.kg_submit = self.kg_queue.submit

        # Background parse/embed queue
        self.ingest_queue = IngestionQueue(
            self.ingestion,
            max_workers=cfg.parser.ingest_max_workers,
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
