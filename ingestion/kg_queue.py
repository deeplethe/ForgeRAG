"""
Background KG-extraction queue (separate from the parse/embed queue).

KG extraction is a long-running step (minutes per document) — when it
shares the same worker pool as parse + chunk + embed, a batch of
12 concurrent uploads fills all slots with KG jobs and the 13th
document sits in ``pending`` until a slot frees minutes later. By
giving KG its own dedicated pool the parse/embed path stays
latency-bounded regardless of how much KG work is queued.

Workflow:

    upload → parse + chunk + embed (fast pool) → status=ready
                                              ↓
                                   submit(KGJob) → kg pool
                                              ↓
                                   _run_kg_from_store(doc_id)

The fast worker is free to pick up the next document as soon as
status hits ``ready``; KG runs asynchronously after that.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue

log = logging.getLogger(__name__)


@dataclass
class KGJob:
    doc_id: str


class KGQueue:
    """Thread-safe KG-extraction queue with a fixed-size worker pool.

    The pool is intentionally smaller than the parse/embed pool —
    KG jobs run for minutes each, so a small number of concurrent KG
    workers is sufficient to saturate the LLM API without starving
    the rest of the ingestion pipeline.
    """

    def __init__(
        self,
        handler: Callable[[str], None],
        *,
        max_workers: int = 3,
    ):
        self._handler = handler
        self.max_workers = max_workers
        self._queue: Queue[KGJob | None] = Queue()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for i in range(self.max_workers):
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"kg-worker-{i}",
                    daemon=True,
                )
                t.start()
                self._workers.append(t)
            self._started = True
            log.info("KG queue started with %d workers", self.max_workers)

    def shutdown(self, timeout: float = 30.0) -> None:
        with self._lock:
            if not self._started:
                return
            for _ in self._workers:
                self._queue.put(None)
        for t in self._workers:
            t.join(timeout=timeout)
        self._workers.clear()
        self._started = False
        log.info("KG queue shut down")

    def submit(self, doc_id: str) -> None:
        """Enqueue a KG job. Returns immediately."""
        self._queue.put(KGJob(doc_id=doc_id))
        log.info(
            "queued KG job doc_id=%s (queue_size≈%d)",
            doc_id,
            self._queue.qsize(),
        )

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def _worker_loop(self) -> None:
        name = threading.current_thread().name
        log.debug("KG worker %s started", name)
        while True:
            job = self._queue.get()
            if job is None:
                self._queue.task_done()
                break
            try:
                self._handler(job.doc_id)
            except Exception:
                log.exception("KG worker %s: unhandled error for doc=%s", name, job.doc_id)
            finally:
                self._queue.task_done()
