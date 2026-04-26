"""
Background ingestion queue.

Provides a thread-pool–based processing queue so that document uploads
return immediately. The queue:

    1. Accepts jobs via submit() — returns instantly.
    2. Worker threads pull jobs and run IngestionPipeline.ingest().
    3. Document status is updated throughout the lifecycle:
         pending → processing → parsing → … → ready | error

Concurrency is controlled by max_workers (default 2).
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class IngestionJob:
    file_id: str
    doc_id: str
    parse_version: int = 1
    enrich_summary: bool | None = None
    force_reparse: bool = False
    # Callback after completion (success or failure). Called from worker thread.
    on_complete: Callable | None = None


class IngestionQueue:
    """
    Thread-safe ingestion queue with a fixed-size worker pool.

    Usage:
        q = IngestionQueue(pipeline, max_workers=2)
        q.start()
        q.submit(IngestionJob(file_id="...", doc_id="..."))
        ...
        q.shutdown()
    """

    def __init__(
        self,
        pipeline: Any,  # IngestionPipeline (avoid circular import)
        *,
        max_workers: int = 2,
        on_complete: Callable | None = None,
    ):
        self.pipeline = pipeline
        self.max_workers = max_workers
        self._on_complete = on_complete  # global callback (e.g. refresh_bm25)
        self._queue: Queue[IngestionJob | None] = Queue()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for i in range(self.max_workers):
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"ingest-worker-{i}",
                    daemon=True,
                )
                t.start()
                self._workers.append(t)
            self._started = True
            log.info("ingestion queue started with %d workers", self.max_workers)

    def shutdown(self, timeout: float = 30.0) -> None:
        """Signal all workers to stop and wait for them."""
        with self._lock:
            if not self._started:
                return
            # Send poison pills
            for _ in self._workers:
                self._queue.put(None)
        for t in self._workers:
            t.join(timeout=timeout)
        self._workers.clear()
        self._started = False
        log.info("ingestion queue shut down")

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def submit(self, job: IngestionJob) -> None:
        """Enqueue a job. Returns immediately."""
        self._queue.put(job)
        log.info(
            "queued ingestion job doc_id=%s file_id=%s (queue_size≈%d)",
            job.doc_id,
            job.file_id,
            self._queue.qsize(),
        )

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def recover_stuck(self, store: Any) -> int:
        """
        Re-enqueue documents that were stuck in intermediate states
        when the process last exited (crash, restart, worker death).

        Called once at startup. The store atomically resets their status
        to 'pending', then we re-submit them as new ingestion jobs.

        Returns the number of recovered jobs.
        """
        recovered = store.recover_stuck_documents()
        for doc in recovered:
            job = IngestionJob(
                file_id=doc["file_id"],
                doc_id=doc["doc_id"],
                force_reparse=True,
            )
            self.submit(job)
        if recovered:
            log.info("re-queued %d stuck document(s) for ingestion", len(recovered))
        return len(recovered)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        name = threading.current_thread().name
        log.debug("worker %s started", name)
        while True:
            job = self._queue.get()
            if job is None:
                # Poison pill → exit
                self._queue.task_done()
                break
            try:
                self._process(job)
            except Exception:
                log.exception("worker %s: unhandled error for doc=%s", name, job.doc_id)
            finally:
                self._queue.task_done()

    def _process(self, job: IngestionJob) -> None:
        doc_id = job.doc_id
        log.info("processing ingestion job doc_id=%s", doc_id)

        # Pre-flight: check embedder has credentials
        # Object chain (CachedEmbedder wrapping):
        #   CachedEmbedder.inner → LiteLLMEmbedder
        #   LiteLLMEmbedder.inner → LiteLLMEmbedderConfig  (has .model, .api_key)
        #   LiteLLMEmbedder.cfg   → EmbedderConfig          (no .model!)
        embedder = self.pipeline.embedder
        if embedder is not None:
            try:
                # Unwrap CachedEmbedder if present
                base = getattr(embedder, "inner", embedder)
                # Get backend-specific config (LiteLLMEmbedderConfig / SentenceTransformersConfig)
                backend_cfg = getattr(base, "inner", None)
                if backend_cfg is not None:
                    model = getattr(backend_cfg, "model", None) or getattr(backend_cfg, "model_name", None)
                    if not model:
                        log.error(
                            "ingestion pre-check: no embedding model configured; "
                            "set a provider in Architecture → Embedding"
                        )
                        self.pipeline.rel.update_document_status(
                            doc_id,
                            status="error",
                            error_message="No embedding model configured. Set a provider in Architecture → Embedding.",
                        )
                        _err = RuntimeError("embedder pre-check failed")
                        if job.on_complete:
                            with contextlib.suppress(Exception):
                                job.on_complete(doc_id, _err)
                        if self._on_complete:
                            with contextlib.suppress(Exception):
                                self._on_complete(doc_id, _err)
                        return
                    api_key = getattr(backend_cfg, "api_key", None)
                    api_key_env = getattr(backend_cfg, "api_key_env", None)
                    if not api_key and not api_key_env:
                        import os

                        if not os.environ.get("OPENAI_API_KEY"):
                            log.error(
                                "ingestion pre-check: embedding api_key missing; "
                                "set a provider in Architecture → Embedding"
                            )
                            self.pipeline.rel.update_document_status(
                                doc_id,
                                status="error",
                                error_message="Embedding API key missing. Set a provider in Architecture → Embedding.",
                            )
                            _err = RuntimeError("embedder pre-check failed")
                            if job.on_complete:
                                with contextlib.suppress(Exception):
                                    job.on_complete(doc_id, _err)
                            if self._on_complete:
                                with contextlib.suppress(Exception):
                                    self._on_complete(doc_id, _err)
                            return
            except Exception:
                pass  # don't block on pre-check failures

        # Mark as processing (the pipeline will further update to parsing/parsed/etc.)
        # Clear any previous error_message so stale errors don't persist on retry.
        with contextlib.suppress(Exception):
            self.pipeline.rel.update_document_status(
                doc_id,
                status="processing",
                error_message=None,
            )

        try:
            result = self.pipeline.ingest(
                job.file_id,
                doc_id=job.doc_id,
                parse_version=job.parse_version,
                enrich_summary=job.enrich_summary,
                force_reparse=job.force_reparse,
            )
            log.info(
                "ingestion job done doc_id=%s blocks=%d chunks=%d",
                doc_id,
                result.num_blocks,
                result.num_chunks,
            )
        except Exception as exc:
            log.exception("ingestion job failed doc_id=%s", doc_id)
            # pipeline.ingest already sets status="error" + error_message, but be defensive
            try:
                msg = str(exc)[:500] if str(exc) else type(exc).__name__
                self.pipeline.rel.update_document_status(
                    doc_id,
                    status="error",
                    error_message=msg,
                )
            except Exception:
                pass
            if job.on_complete:
                with contextlib.suppress(Exception):
                    job.on_complete(doc_id, exc)
            return

        # Success callback
        if job.on_complete:
            with contextlib.suppress(Exception):
                job.on_complete(doc_id, None)
        if self._on_complete:
            with contextlib.suppress(Exception):
                self._on_complete(doc_id, None)
