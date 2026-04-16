"""
Tree retrieval path.

Three modes (auto-selected):

    1. LLM navigation (PageIndex-style with heat-map hints):
           BM25 + Vector pre-filter → candidate doc_ids →
           build heat-map from pre-filter scored chunks →
           parallel LLM tree nav (ThreadPoolExecutor) →
           LLM returns nodes with relevance scores →
           expand nodes to chunks

    2. BM25 fallback (llm_nav_enabled=False):
           BM25 prefilter N docs → top BM25 chunks within those docs

    3. Disabled (tree_path.enabled=False): return []

Optimizations:
    - Cross-validation: docs hit by BOTH BM25 and vector go first
    - Parallel: up to max_workers concurrent LLM nav calls
    - Early stop: once target_chunks accumulated, stop waiting
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Protocol

from config import BM25Config, TreePathConfig
from persistence.store import Store as RelationalStore

from .bm25 import InMemoryBM25Index
from .tree_navigator import HeatMap, NavResult
from .types import ScoredChunk

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional LLM navigator protocol (stub-friendly)
# ---------------------------------------------------------------------------


class TreeNavigator(Protocol):
    def navigate(self, query: str, tree: dict, *, top_k: int) -> list[str]: ...


# Extended protocol for scored navigation
class ScoredTreeNavigator(Protocol):
    def navigate_scored(
        self,
        query: str,
        tree: dict,
        *,
        top_k: int,
        heat_map: HeatMap | None,
    ) -> list[NavResult]: ...


# ---------------------------------------------------------------------------
# Pre-filter chunk info (from BM25/vector)
# ---------------------------------------------------------------------------


class PreFilterHit:
    """A chunk hit from BM25 or vector, used to build heat maps."""

    __slots__ = ("chunk_id", "doc_id", "node_id", "score", "snippet", "source")

    def __init__(
        self,
        chunk_id: str,
        doc_id: str,
        node_id: str,
        score: float,
        source: str,
        snippet: str = "",
    ):
        self.chunk_id = chunk_id
        self.doc_id = doc_id
        self.node_id = node_id
        self.score = score
        self.source = source
        self.snippet = snippet


# ---------------------------------------------------------------------------
# TreePath
# ---------------------------------------------------------------------------


class TreePath:
    def __init__(
        self,
        cfg: TreePathConfig,
        bm25_cfg: BM25Config,
        bm25_index: InMemoryBM25Index,
        relational_store: RelationalStore,
        navigator: TreeNavigator | None = None,
    ):
        self.cfg = cfg
        self.bm25_cfg = bm25_cfg
        self.bm25 = bm25_index
        self.rel = relational_store
        self.navigator = navigator
        self._llm_calls: list[dict] = []  # collect LLM call info for trace
        self._llm_calls_lock = threading.Lock()  # thread-safe append

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        vector_doc_ids: set[str] | None = None,
        prefilter_hits: list[PreFilterHit] | None = None,
    ) -> list[ScoredChunk]:
        if not self.cfg.enabled:
            return []

        # BM25 doc prefilter (may return empty for non-Latin queries, etc.)
        doc_hits = self.bm25.search_docs(query, self.bm25_cfg.doc_prefilter_top_k)
        bm25_doc_set = {d for d, _ in doc_hits}

        # Merge externally-provided doc_ids (from pipeline's BM25 + Vector)
        # so tree nav runs even when the internal BM25 prefilter misses.
        ext_ids = (vector_doc_ids or set()) - bm25_doc_set
        if ext_ids:
            for did in ext_ids:
                doc_hits.append((did, 0.0))  # score 0 = no BM25 signal

        if not doc_hits:
            return []

        if self.cfg.llm_nav_enabled and self.navigator is not None:
            return self._llm_nav_parallel(query, doc_hits, vector_doc_ids or set(), prefilter_hits)
        return self._bm25_fallback(query, doc_hits)

    # ==================================================================
    # Mode 1: LLM navigation with heat-map hints
    # ==================================================================

    def _llm_nav_parallel(
        self,
        query: str,
        doc_hits: list[tuple[str, float]],
        vector_doc_ids: set[str],
        prefilter_hits: list[PreFilterHit] | None = None,
    ) -> list[ScoredChunk]:
        assert self.navigator is not None
        nav_cfg = self.cfg.nav

        # --- Cross-validation: dual-hit docs first ---
        prioritized = sorted(
            doc_hits,
            key=lambda d: (
                0 if d[0] in vector_doc_ids else 1,
                -d[1],  # then by BM25 score desc
            ),
        )
        # Limit candidate docs to avoid excessive LLM calls
        max_docs = getattr(nav_cfg, "max_docs", 5)
        if len(prioritized) > max_docs:
            prioritized = prioritized[:max_docs]

        dual = sum(1 for d, _ in prioritized if d in vector_doc_ids)
        log.info(
            "tree_path: %d candidate docs (capped from %d), %d dual-hit",
            len(prioritized),
            len(doc_hits),
            dual,
        )

        # --- Build per-doc heat maps from prefilter hits ---
        doc_heat_maps: dict[str, HeatMap] = {}
        if prefilter_hits:
            for hit in prefilter_hits:
                if hit.doc_id not in doc_heat_maps:
                    doc_heat_maps[hit.doc_id] = {}
                hm = doc_heat_maps[hit.doc_id]
                if hit.node_id not in hm:
                    hm[hit.node_id] = []
                hm[hit.node_id].append((hit.source, hit.snippet, hit.score))

        # --- Parallel LLM navigation ---
        workers = min(nav_cfg.max_workers, len(prioritized))
        results_by_doc: dict[str, list[ScoredChunk]] = {}
        t0 = time.time()

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self._nav_one_doc,
                    query,
                    doc_id,
                    doc_heat_maps.get(doc_id),
                ): doc_id
                for doc_id, _ in prioritized
            }

            accumulated = 0
            for fut in as_completed(futures):
                doc_id = futures[fut]
                try:
                    chunks = fut.result()
                    results_by_doc[doc_id] = chunks
                    accumulated += len(chunks)
                except Exception as e:
                    log.warning("tree nav failed for %s: %s", doc_id, e)

                # --- Early stop ---
                if accumulated >= nav_cfg.target_chunks:
                    for f in futures:
                        f.cancel()
                    log.info(
                        "tree_path early stop: %d chunks from %d/%d docs",
                        accumulated,
                        len(results_by_doc),
                        len(prioritized),
                    )
                    break

        elapsed = int((time.time() - t0) * 1000)
        log.info(
            "tree_path parallel nav: %d docs, %d chunks, %dms (workers=%d)",
            len(results_by_doc),
            accumulated,
            elapsed,
            workers,
        )

        # --- Collect in priority order (not completion order) ---
        result: list[ScoredChunk] = []
        for doc_id, _ in prioritized:
            if doc_id in results_by_doc:
                result.extend(results_by_doc[doc_id])
        return result[: self.cfg.top_k]

    # ------------------------------------------------------------------
    def _nav_one_doc(
        self,
        query: str,
        doc_id: str,
        heat_map: HeatMap | None,
    ) -> list[ScoredChunk]:
        """Navigate one document's tree. Runs in a worker thread."""
        doc_row = self.rel.get_document(doc_id)
        if not doc_row:
            return []
        pv = doc_row["active_parse_version"]
        tree_json = self.rel.load_tree(doc_id, pv)
        if not tree_json:
            return []

        t0 = time.time()

        # Use scored navigation if available
        if hasattr(self.navigator, "navigate_scored"):
            nav_results = self.navigator.navigate_scored(
                query,
                tree_json,
                top_k=self.cfg.nav.max_nodes,
                heat_map=heat_map,
            )
        else:
            # Legacy navigator: no scores
            node_ids = self.navigator.navigate(query, tree_json, top_k=self.cfg.nav.max_nodes)
            nav_results = [NavResult(nid, 0.5) for nid in node_ids]

        nav_ms = int((time.time() - t0) * 1000)
        model_name = getattr(self.navigator, "model", "unknown")
        with self._llm_calls_lock:
            self._llm_calls.append(
                dict(
                    model=model_name,
                    purpose=f"tree_nav:{doc_id[:20]}",
                    latency_ms=nav_ms,
                    output_preview=str([(r.node_id, r.relevance) for r in nav_results[:5]]),
                )
            )
        log.debug(
            "tree nav doc=%s nodes=%d nav_ms=%d",
            doc_id[:20],
            len(nav_results),
            nav_ms,
        )
        if not nav_results:
            return []

        # Expand selected nodes to chunks, using LLM relevance as score
        all_node_ids = [r.node_id for r in nav_results]
        node_relevance = {r.node_id: r.relevance for r in nav_results}
        chunk_rows = self.rel.get_chunks_by_node_ids(all_node_ids)

        # Preserve navigator's node ordering
        node_order = {nid: i for i, nid in enumerate(all_node_ids)}
        chunk_rows.sort(key=lambda r: node_order.get(r["node_id"], 1_000))

        results: list[ScoredChunk] = []
        for r in chunk_rows:
            nid = r["node_id"]
            relevance = node_relevance.get(nid, 0.3)
            results.append(
                ScoredChunk(
                    chunk_id=r["chunk_id"],
                    score=relevance,
                    source="tree",
                )
            )
        return results

    # ==================================================================
    # Mode 2: pure BM25 fallback
    # ==================================================================

    def _bm25_fallback(
        self,
        query: str,
        doc_hits: list[tuple[str, float]],
    ) -> list[ScoredChunk]:
        allowed_docs = {d for d, _ in doc_hits}
        doc_score = dict(doc_hits)

        broad = self.bm25.search_chunks(query, self.cfg.top_k * 4)

        result: list[ScoredChunk] = []
        per_doc_count: dict[str, int] = {}
        per_doc_cap = max(1, self.cfg.top_k // max(1, len(allowed_docs)))

        cid_to_idx = {cid: i for i, cid in enumerate(self.bm25.chunk_ids)}
        for chunk_id, chunk_score in broad:
            idx = cid_to_idx.get(chunk_id)
            if idx is None:
                continue
            did = self.bm25.doc_ids[idx]
            if did not in allowed_docs:
                continue
            if per_doc_count.get(did, 0) >= per_doc_cap:
                continue
            per_doc_count[did] = per_doc_count.get(did, 0) + 1
            combined = chunk_score + 0.2 * doc_score.get(did, 0.0)
            result.append(ScoredChunk(chunk_id=chunk_id, score=combined, source="tree"))
            if len(result) >= self.cfg.top_k:
                break
        return result
