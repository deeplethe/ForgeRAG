"""
End-to-end retrieval pipeline.

Execution order:

    Phase 0: Query Understanding (intent + routing + expansion)

    Phase 1: Parallel retrieval — BM25 + Vector + KG all start immediately
             ┌─────────────────────────────────────┐
             │  BM25 (<5ms)  Vector (~1s)  KG (~2s)│
             └─────────────────────────────────────┘

    Phase 2: Tree navigation — waits for BM25 + Vector to finish,
             uses their combined doc_ids for cross-validated routing.
             KG continues independently in the background.

    Phase 3: RRF merge — fuses all 4 ranked lists via Reciprocal
             Rank Fusion, then expands context + reranks + citations.

The pipeline owns no global state: caller constructs the collaborators
(embedder, vector store, relational store, BM25 index, reranker) and
passes them in. BM25 index building is a one-shot operation the
caller should run at startup -- see `build_bm25_index`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from config import RetrievalSection
from embedder.base import Embedder
from persistence.store import Store as RelationalStore
from persistence.vector.base import VectorStore

from .bm25 import InMemoryBM25Index
from .citations import build_citations
from .merge import (
    expand_crossrefs,
    expand_descendants,
    expand_siblings,
    finalize_merged,
    rehydrate,
    rrf_merge,
)
from .rerank import Reranker, make_reranker
from .trace import RetrievalTrace
from .tree_path import TreeNavigator, TreePath
from .types import RetrievalResult, ScoredChunk

log = logging.getLogger(__name__)


class RetrievalPipeline:
    def __init__(
        self,
        cfg: RetrievalSection,
        *,
        embedder: Embedder,
        vector_store: VectorStore,
        relational_store: RelationalStore,
        bm25_index: InMemoryBM25Index,
        reranker: Reranker | None = None,
        tree_navigator: TreeNavigator | None = None,
        graph_store=None,  # Optional[GraphStore] for KG path
    ):
        self.cfg = cfg
        self.embedder = embedder
        self.vector = vector_store
        self.rel = relational_store
        self.bm25 = bm25_index
        self.reranker = reranker or make_reranker(cfg.rerank)
        self.navigator = tree_navigator
        self.graph_store = graph_store
        self._expander = None

    # ------------------------------------------------------------------
    def analyze_query(
        self,
        query: str,
        *,
        chat_history: list[dict] | None = None,
    ):
        """Run query understanding only (no retrieval). Returns QueryPlan or None."""
        qu_cfg = self.cfg.query_understanding
        if not qu_cfg.enabled:
            return None
        try:
            from .query_understanding import QueryUnderstanding

            if self._expander is None:
                self._expander = QueryUnderstanding(
                    model=qu_cfg.model,
                    api_key=qu_cfg.api_key,
                    api_key_env=qu_cfg.api_key_env,
                    api_base=qu_cfg.api_base,
                    max_expansions=qu_cfg.max_expansions,
                    timeout=qu_cfg.timeout,
                    system_prompt=qu_cfg.system_prompt,
                    user_prompt_template=qu_cfg.user_prompt_template,
                )
            return self._expander.analyze(query, chat_history=chat_history)
        except Exception as e:
            log.warning("analyze_query failed: %s", e)
            return None

    # ------------------------------------------------------------------
    def retrieve(
        self,
        query: str,
        *,
        filter: dict | None = None,
        progress_cb=None,
        chat_history: list[dict] | None = None,
        precomputed_plan=None,
    ) -> RetrievalResult:
        trace = RetrievalTrace(query)
        stats: dict = {}
        _pcb = progress_cb or (lambda *a, **k: None)

        # ============================================================
        # Phase 0: Query Understanding (intent + routing + expansion)
        # ============================================================
        queries = [query]
        query_plan = precomputed_plan
        _skip_paths: set[str] = set()

        if precomputed_plan is not None:
            # QU already ran upstream — use its results directly
            queries = precomputed_plan.expanded_queries or [query]
            _skip_paths = set(precomputed_plan.skip_paths)
            trace.begin_phase("query_understanding")
            if precomputed_plan.latency_ms:
                trace.record_llm_call(
                    model=precomputed_plan.model or "unknown",
                    purpose="query_understanding",
                    output_preview=(
                        f"intent={precomputed_plan.intent} "
                        f"retrieval={precomputed_plan.needs_retrieval} "
                        f"skip={precomputed_plan.skip_paths}"
                    ),
                    latency_ms=precomputed_plan.latency_ms,
                )
            trace.add_detail(
                intent=precomputed_plan.intent,
                needs_retrieval=precomputed_plan.needs_retrieval,
                skip_paths=precomputed_plan.skip_paths,
                hint=precomputed_plan.hint,
                variants=queries[1:],
                total_queries=len(queries),
                precomputed=True,
            )
            trace.end_phase(
                intent=precomputed_plan.intent,
                expanded_count=len(queries),
                fallback=False,
            )
            # The QU LLM call already happened upstream, before this pipeline
            # was invoked. The begin_phase/end_phase sandwich above ran in
            # ~0ms because it's just metadata bookkeeping. Override the
            # recorded duration so the trace reflects the real upstream work
            # (matches how the parallel paths post-fix their timings).
            if precomputed_plan.latency_ms:
                trace.phases[-1]["duration_ms"] = precomputed_plan.latency_ms

        qu_cfg = self.cfg.query_understanding
        if qu_cfg.enabled and precomputed_plan is None:
            _pcb(phase="query_understanding", status="running")
            trace.begin_phase("query_understanding")
            try:
                from .query_understanding import QueryUnderstanding

                if self._expander is None:
                    self._expander = QueryUnderstanding(
                        model=qu_cfg.model,
                        api_key=qu_cfg.api_key,
                        api_key_env=qu_cfg.api_key_env,
                        api_base=qu_cfg.api_base,
                        max_expansions=qu_cfg.max_expansions,
                        timeout=qu_cfg.timeout,
                        system_prompt=qu_cfg.system_prompt,
                        user_prompt_template=qu_cfg.user_prompt_template,
                    )
                query_plan = self._expander.analyze(query, chat_history=chat_history)
                queries = query_plan.expanded_queries or [query]
                _skip_paths = set(query_plan.skip_paths)
                trace.record_llm_call(
                    model=qu_cfg.model,
                    purpose="query_understanding",
                    output_preview=(
                        f"intent={query_plan.intent} "
                        f"retrieval={query_plan.needs_retrieval} "
                        f"skip={query_plan.skip_paths}"
                    ),
                    latency_ms=query_plan.latency_ms,
                )
                trace.add_detail(
                    intent=query_plan.intent,
                    needs_retrieval=query_plan.needs_retrieval,
                    skip_paths=query_plan.skip_paths,
                    hint=query_plan.hint,
                    variants=queries[1:],
                    total_queries=len(queries),
                    direct_answer=query_plan.direct_answer,
                )
            except Exception as e:
                log.warning("query understanding failed: %s", e)
                trace.add_detail(error=str(e))
            _qu_ok = query_plan is not None
            trace.end_phase(
                intent=query_plan.intent if query_plan else "unknown",
                expanded_count=len(queries),
                fallback=not _qu_ok,
            )
            _pcb(
                phase="query_understanding",
                status="done",
                detail=(
                    f"{query_plan.intent}, {len(queries)} queries"
                    if _qu_ok
                    else "skipped (timeout), using original query"
                ),
            )
        elif self.cfg.query_expansion.enabled and precomputed_plan is None:
            # Legacy fallback: old-style query expansion only
            _pcb(phase="query_expansion", status="running")
            trace.begin_phase("query_expansion")
            try:
                from .query_expansion import QueryExpander

                qe_cfg = self.cfg.query_expansion
                if self._expander is None:
                    self._expander = QueryExpander(
                        model=qe_cfg.model,
                        api_key=qe_cfg.api_key,
                        api_key_env=qe_cfg.api_key_env,
                        api_base=qe_cfg.api_base,
                        max_expansions=qe_cfg.max_expansions,
                        timeout=qe_cfg.timeout,
                    )
                t_qe = time.time()
                queries = self._expander.expand(query)
                qe_ms = int((time.time() - t_qe) * 1000)
                trace.record_llm_call(
                    model=qe_cfg.model,
                    purpose="query_expansion",
                    output_preview=str(queries[1:]),
                    latency_ms=qe_ms,
                )
                trace.add_detail(
                    original=query,
                    variants=queries[1:],
                    total_queries=len(queries),
                )
            except Exception as e:
                log.warning("query expansion failed: %s", e)
                trace.add_detail(error=str(e))
            trace.end_phase(expanded_count=len(queries))
            _pcb(phase="query_expansion", status="done", detail=f"{len(queries)} queries")

        stats["expanded_queries"] = queries

        # ── Short-circuit: no retrieval needed ──
        if query_plan and not query_plan.needs_retrieval:
            log.info("query understanding: skipping retrieval (intent=%s)", query_plan.intent)
            stats["trace"] = trace.to_dict()
            stats["total_ms"] = stats["trace"]["total_ms"]
            stats["skipped"] = True
            return RetrievalResult(
                query=query,
                merged=[],
                citations=[],
                vector_hits=[],
                tree_hits=[],
                stats=stats,
                query_plan=query_plan,
            )

        # ============================================================
        # Phase 1: Parallel retrieval — BM25 + Vector + KG
        # All three start immediately. Tree waits for BM25+Vector.
        # ============================================================
        from concurrent.futures import ThreadPoolExecutor

        # Timing containers for parallel phases (written from worker threads)
        _timings: dict[str, dict] = {}

        def _run_bm25() -> tuple[list[ScoredChunk], set[str]]:
            t0b = time.time()
            _pcb(phase="bm25_path", status="running")
            if not self.cfg.bm25.enabled or "bm25_path" in _skip_paths:
                _timings["bm25"] = {"start": t0b, "end": t0b, "llm_calls": []}
                _pcb(phase="bm25_path", status="done", detail="skipped" if "bm25_path" in _skip_paths else "disabled")
                return [], set()
            bm25_all: list[ScoredChunk] = []
            for q in queries:
                for cid, score in self.bm25.search_chunks(q, self.cfg.bm25.top_k):
                    bm25_all.append(ScoredChunk(chunk_id=cid, score=score, source="bm25"))
            bm25_best: dict[str, ScoredChunk] = {}
            for sc in bm25_all:
                if sc.chunk_id not in bm25_best or sc.score > bm25_best[sc.chunk_id].score:
                    bm25_best[sc.chunk_id] = sc
            hits = sorted(bm25_best.values(), key=lambda s: -s.score)
            # Doc prefilter for Tree path
            doc_ids: set[str] = set()
            for q in queries:
                for doc_id, _ in self.bm25.search_docs(q, self.cfg.bm25.doc_prefilter_top_k):
                    doc_ids.add(doc_id)
            _timings["bm25"] = {"start": t0b, "end": time.time(), "llm_calls": []}
            _pcb(phase="bm25_path", status="done", detail=f"{len(hits)} hits")
            return hits, doc_ids

        def _run_vector() -> tuple[list[ScoredChunk], list]:
            all_vec: list[ScoredChunk] = []
            all_raw = []
            t0v = time.time()
            _pcb(phase="vector_path", status="running")
            if not self.cfg.vector.enabled or "vector_path" in _skip_paths:
                _timings["vector"] = {"start": t0v, "end": t0v, "llm_calls": []}
                _pcb(
                    phase="vector_path", status="done", detail="skipped" if "vector_path" in _skip_paths else "disabled"
                )
                return [], []
            # Embedding API call (often the most expensive IO operation)
            t_embed = time.time()
            q_vecs = self.embedder.embed_texts(queries)
            embed_ms = int((time.time() - t_embed) * 1000)
            # Resolve embedder model name for trace
            _emb_model = (
                getattr(getattr(self.embedder, "inner", None), "model", None)
                or getattr(getattr(self.embedder, "inner", None), "model_name", None)
                or getattr(self.embedder, "backend", "unknown")
            )
            _embed_call = dict(
                model=str(_emb_model),
                purpose="embedding",
                latency_ms=embed_ms,
                output_preview=f"{len(queries)} queries -> {len(q_vecs)} vectors",
            )
            for q_vec in q_vecs:
                hits = self.vector.search(
                    q_vec,
                    top_k=self.cfg.vector.top_k,
                    filter=filter or self.cfg.vector.default_filter,
                )
                all_raw.extend(hits)
                all_vec.extend([ScoredChunk(chunk_id=h.chunk_id, score=h.score, source="vector") for h in hits])
            # Dedup
            best_v: dict[str, ScoredChunk] = {}
            for sc in all_vec:
                if sc.chunk_id not in best_v or sc.score > best_v[sc.chunk_id].score:
                    best_v[sc.chunk_id] = sc
            deduped = sorted(best_v.values(), key=lambda s: -s.score)
            raw_d: dict[str, Any] = {}
            for h in all_raw:
                if h.chunk_id not in raw_d:
                    raw_d[h.chunk_id] = h
            _timings["vector"] = {
                "start": t0v,
                "end": time.time(),
                "llm_calls": [_embed_call],
            }
            _pcb(phase="vector_path", status="done", detail=f"{len(deduped)} hits")
            return deduped, list(raw_d.values())

        def _run_tree(
            bm25_doc_ids: set[str],
            vector_doc_ids: set[str],
            prefilter_hits: list | None = None,
        ) -> list[ScoredChunk]:
            t0t = time.time()
            _pcb(phase="tree_path", status="running")
            if not self.cfg.tree_path.enabled or "tree_path" in _skip_paths:
                _timings["tree"] = {"start": t0t, "end": t0t, "llm_calls": []}
                _pcb(phase="tree_path", status="done", detail="skipped" if "tree_path" in _skip_paths else "disabled")
                return []
            doc_ids = bm25_doc_ids | vector_doc_ids
            tp = TreePath(
                self.cfg.tree_path,
                self.cfg.bm25,
                self.bm25,
                self.rel,
                navigator=self.navigator,
            )
            result = tp.search(
                query,
                vector_doc_ids=doc_ids,
                prefilter_hits=prefilter_hits,
            )
            _timings["tree"] = {
                "start": t0t,
                "end": time.time(),
                "llm_calls": getattr(tp, "_llm_calls", []),
            }
            _pcb(phase="tree_path", status="done", detail=f"{len(result)} hits")
            return result

        _kg_context = [None]  # mutable container for thread result

        def _run_kg() -> list[ScoredChunk]:
            t0k = time.time()
            _pcb(phase="kg_path", status="running")
            if not (self.cfg.kg_path.enabled and self.graph_store is not None) or "kg_path" in _skip_paths:
                _timings["kg"] = {"start": t0k, "end": t0k, "llm_calls": []}
                _pcb(phase="kg_path", status="done", detail="skipped" if "kg_path" in _skip_paths else "disabled")
                return []
            from .kg_path import KGPath

            kp = KGPath(self.cfg.kg_path, self.graph_store, self.rel, embedder=self.embedder)
            result = kp.search(query)
            _kg_context[0] = kp.kg_context  # capture synthesized KG context
            _timings["kg"] = {
                "start": t0k,
                "end": time.time(),
                "llm_calls": getattr(kp, "_llm_calls", []),
            }
            _pcb(phase="kg_path", status="done", detail=f"{len(result)} hits")
            return result

        # Phase 1: Launch BM25 + Vector + KG in parallel
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="retrieval") as _pool:
            bm25_future = _pool.submit(_run_bm25)
            vec_future = _pool.submit(_run_vector)
            kg_future = _pool.submit(_run_kg)

            # Wait for BM25 (fast, <5ms) and Vector (~1s)
            try:
                bm25_hits, expanded_bm25_docs = bm25_future.result(timeout=10)
            except Exception as e:
                log.warning("BM25 path failed: %s", e, exc_info=True)
                bm25_hits, expanded_bm25_docs = [], set()
                _pcb(phase="bm25_path", status="done", detail=f"error: {type(e).__name__}")
                _timings.setdefault("bm25", {"start": time.time(), "end": time.time(), "llm_calls": []})

            try:
                vector_hits, raw_vector_hits = vec_future.result(timeout=30)
            except Exception as e:
                log.warning("Vector path failed: %s", e, exc_info=True)
                vector_hits, raw_vector_hits = [], []
                _pcb(phase="vector_path", status="done", detail=f"error: {type(e).__name__}")
                _timings.setdefault("vector", {"start": time.time(), "end": time.time(), "llm_calls": []})

            # Phase 2: Tree navigation — uses combined doc_ids from
            # BM25 + Vector for cross-validated document routing.
            # Build prefilter hits for heat-map annotations in tree navigator.
            vector_doc_ids = {h.doc_id for h in raw_vector_hits if h.doc_id} | expanded_bm25_docs

            # Build prefilter hits from BM25 + vector results
            from .tree_path import PreFilterHit

            prefilter_hits: list[PreFilterHit] = []
            # Resolve chunk metadata for heat map (node_id, doc_id, snippet)
            _prefilter_chunk_ids = [s.chunk_id for s in bm25_hits + vector_hits]
            _prefilter_rows = {}
            if _prefilter_chunk_ids:
                for row in self.rel.get_chunks_by_ids(_prefilter_chunk_ids[:100]):
                    _prefilter_rows[row["chunk_id"]] = row
            for sc in bm25_hits:
                row = _prefilter_rows.get(sc.chunk_id)
                if row:
                    prefilter_hits.append(
                        PreFilterHit(
                            chunk_id=sc.chunk_id,
                            doc_id=row.get("doc_id", ""),
                            node_id=row.get("node_id", ""),
                            score=sc.score,
                            source="bm25",
                            snippet=(row.get("content") or "")[:80],
                        )
                    )
            for sc in vector_hits:
                row = _prefilter_rows.get(sc.chunk_id)
                if row:
                    prefilter_hits.append(
                        PreFilterHit(
                            chunk_id=sc.chunk_id,
                            doc_id=row.get("doc_id", ""),
                            node_id=row.get("node_id", ""),
                            score=sc.score,
                            source="vector",
                            snippet=(row.get("content") or "")[:80],
                        )
                    )

            tree_future = _pool.submit(_run_tree, expanded_bm25_docs, vector_doc_ids, prefilter_hits)

            try:
                tree_hits = tree_future.result(timeout=60)
            except Exception as e:
                log.warning("Tree path failed: %s", e, exc_info=True)
                tree_hits = []
                _pcb(phase="tree_path", status="done", detail=f"error: {type(e).__name__}")
                _timings.setdefault("tree", {"start": time.time(), "end": time.time(), "llm_calls": []})

            # Collect KG results (may already be done by now)
            try:
                kg_hits = kg_future.result(timeout=30)
            except Exception as e:
                log.warning("KG path failed: %s", e, exc_info=True)
                kg_hits = []
                _pcb(phase="kg_path", status="done", detail=f"error: {type(e).__name__}")
                _timings.setdefault("kg", {"start": time.time(), "end": time.time(), "llm_calls": []})

        # ── Record traces for all parallel paths with real timings ──
        # Order: BM25 → Vector → KG → Tree (reflects actual start order)
        _trace_started = trace.started_at  # retrieval epoch

        # BM25
        trace.begin_phase("bm25_path")
        trace.set_inputs(
            enabled=self.cfg.bm25.enabled,
            query=query,
            top_k=self.cfg.bm25.top_k,
        )
        trace.record_chunks("bm25_results", [s.chunk_id for s in bm25_hits], "bm25")
        bt = _timings.get("bm25")
        if bt:
            trace._current["started_at"] = bt["start"]
            for lc in bt.get("llm_calls", []):
                trace.record_llm_call(**lc)
        trace.end_phase(
            total_hits=len(bm25_hits),
            unique_docs=len(expanded_bm25_docs),
        )
        if bt:
            trace.phases[-1]["duration_ms"] = int((bt["end"] - bt["start"]) * 1000)
            trace.phases[-1]["started_at_ms"] = int((bt["start"] - _trace_started) * 1000)
        stats["bm25_hits"] = len(bm25_hits)
        # Per-path top-5 chunk_ids (for benchmark per-path attribution analysis)
        stats["bm25_top_ids"] = [s.chunk_id for s in bm25_hits[:5]]

        # Vector
        trace.begin_phase("vector_path")
        trace.set_inputs(
            enabled=self.cfg.vector.enabled,
            queries=queries,
            top_k=self.cfg.vector.top_k,
        )
        trace.record_chunks("vector_results", [s.chunk_id for s in vector_hits], "vector")
        vt = _timings.get("vector")
        if vt:
            trace._current["started_at"] = vt["start"]
            for lc in vt.get("llm_calls", []):
                trace.record_llm_call(**lc)
        trace.end_phase(
            total_hits=len(vector_hits),
            unique_docs=len({h.doc_id for h in raw_vector_hits if h.doc_id}),
            top_scores=[round(s.score, 3) for s in vector_hits[:5]],
        )
        if vt:
            trace.phases[-1]["duration_ms"] = int((vt["end"] - vt["start"]) * 1000)
            trace.phases[-1]["started_at_ms"] = int((vt["start"] - _trace_started) * 1000)
        stats["vector_hits"] = len(vector_hits)
        stats["vector_top_ids"] = [s.chunk_id for s in vector_hits[:5]]

        # KG
        trace.begin_phase("kg_path")
        trace.record_chunks("kg_results", [s.chunk_id for s in kg_hits], "kg")
        kt = _timings.get("kg")
        if kt:
            trace._current["started_at"] = kt["start"]
            for lc in kt.get("llm_calls", []):
                trace.record_llm_call(**lc)
        trace.end_phase(total_hits=len(kg_hits))
        if kt:
            trace.phases[-1]["duration_ms"] = int((kt["end"] - kt["start"]) * 1000)
            trace.phases[-1]["started_at_ms"] = int((kt["start"] - _trace_started) * 1000)
        stats["kg_hits"] = len(kg_hits)
        stats["kg_top_ids"] = [s.chunk_id for s in kg_hits[:5]]

        # Tree (runs after BM25 + Vector, so recorded last)
        trace.begin_phase("tree_path")
        trace.set_inputs(
            query=query,
            vector_doc_ids=len(vector_doc_ids),
            expanded_bm25_docs=len(expanded_bm25_docs),
            llm_nav_enabled=self.cfg.tree_path.llm_nav_enabled,
        )
        trace.record_chunks("tree_results", [s.chunk_id for s in tree_hits], "tree")
        tt = _timings.get("tree")
        if tt:
            trace._current["started_at"] = tt["start"]
            for lc in tt.get("llm_calls", []):
                trace.record_llm_call(**lc)
        trace.end_phase(total_hits=len(tree_hits))
        if tt:
            trace.phases[-1]["duration_ms"] = int((tt["end"] - tt["start"]) * 1000)
            trace.phases[-1]["started_at_ms"] = int((tt["start"] - _trace_started) * 1000)
        stats["tree_hits"] = len(tree_hits)
        stats["tree_top_ids"] = [s.chunk_id for s in tree_hits[:5]]
        stats["vector_doc_ids"] = len(vector_doc_ids)

        # ============================================================
        # Phase 3: RRF merge
        # ============================================================
        # New architecture: BM25/vector are pre-filters for tree navigation.
        # RRF only fuses: tree + KG + vector_fallback (for non-navigable docs).
        # If tree path produced no results, BM25/vector hits enter RRF
        # as fallback so retrieval still works without tree navigation.
        _pcb(phase="rrf_merge", status="running")
        trace.begin_phase("rrf_merge")

        rrf_inputs: list[list[ScoredChunk]] = []
        active_paths: list[str] = []

        # Primary reasoning paths
        if self.cfg.tree_path.enabled and tree_hits:
            rrf_inputs.append(tree_hits)
            active_paths.append("tree")
        if self.cfg.kg_path.enabled and kg_hits:
            rrf_inputs.append(kg_hits)
            active_paths.append("kg")

        # Fallback: if tree produced nothing, include BM25/vector directly
        if not tree_hits:
            if self.cfg.vector.enabled and vector_hits:
                rrf_inputs.append(vector_hits)
                active_paths.append("vector_fallback")
            if self.cfg.bm25.enabled and bm25_hits:
                rrf_inputs.append(bm25_hits)
                active_paths.append("bm25_fallback")

        trace.set_inputs(
            active_paths=active_paths,
            vector_count=len(vector_hits),
            bm25_count=len(bm25_hits),
            tree_count=len(tree_hits),
            kg_count=len(kg_hits),
            rrf_k=self.cfg.merge.rrf_k,
        )

        merged = rrf_merge(rrf_inputs, k=self.cfg.merge.rrf_k)
        pre_expand = len(merged)
        trace.add_detail(post_rrf=pre_expand)
        trace.end_phase(merged_count=pre_expand)
        _pcb(phase="rrf_merge", status="done", detail=f"{pre_expand} merged")

        # ============================================================
        # Phase 4: Context expansion
        # ============================================================
        _pcb(phase="expansion", status="running")
        trace.begin_phase("expansion")
        trace.set_inputs(
            descendant=self.cfg.merge.descendant_expansion_enabled,
            sibling=self.cfg.merge.sibling_expansion_enabled,
            crossref=self.cfg.merge.crossref_expansion_enabled,
        )

        expand_descendants(merged, self.rel, self.cfg.merge)
        post_desc = len(merged)
        expand_siblings(merged, self.rel, self.cfg.merge)
        post_sib = len(merged)
        expand_crossrefs(merged, self.rel, self.cfg.merge)
        post_xref = len(merged)
        rehydrate(merged, self.rel)

        finalized = finalize_merged(
            merged,
            base_top_k=self.cfg.vector.top_k,
            cfg=self.cfg.merge,
        )

        trace.add_detail(
            added_by_descendant=post_desc - pre_expand,
            added_by_sibling=post_sib - post_desc,
            added_by_crossref=post_xref - post_sib,
            after_budget_cap=len(finalized),
        )
        trace.end_phase(final_candidates=len(finalized))
        _pcb(phase="expansion", status="done", detail=f"{len(finalized)} candidates")
        stats["merged_count"] = len(finalized)

        # ============================================================
        # Phase 5: Rerank
        # ============================================================
        _pcb(phase="rerank", status="running")
        trace.begin_phase("rerank")
        trace.set_inputs(
            enabled=self.cfg.rerank.enabled,
            top_k=self.cfg.rerank.top_k,
            candidates=len(finalized),
        )
        rerank_error: str | None = None
        if self.cfg.rerank.enabled:
            try:
                picked = self.reranker.rerank(query, finalized, top_k=self.cfg.rerank.top_k)
            except Exception as e:  # including RerankerError in strict mode
                # Log + record in trace but keep the query alive with RRF order
                # so the user still gets an answer. The UI will show the error
                # via the health-components endpoint + architecture red dot.
                rerank_error = f"{type(e).__name__}: {e}"
                log.warning("rerank phase failed — falling back to RRF order: %s", rerank_error)
                picked = finalized[: self.cfg.rerank.top_k]
        else:
            picked = finalized[: self.cfg.rerank.top_k]
        if rerank_error:
            trace.end_phase(output_count=len(picked), error=rerank_error)
        else:
            trace.end_phase(output_count=len(picked))
        _pcb(
            phase="rerank",
            status="error" if rerank_error else "done",
            detail=rerank_error or f"{len(picked)} selected",
        )
        stats["reranked_count"] = len(picked)
        if rerank_error:
            stats["rerank_error"] = rerank_error

        # ============================================================
        # Phase 6: Citations
        # ============================================================
        _pcb(phase="citations", status="running")
        trace.begin_phase("citations")
        citations = build_citations(picked, self.rel, self.cfg.citations)
        trace.record_chunks(
            "cited_chunks",
            [c.citation_id for c in citations],
            "citation_builder",
        )
        trace.end_phase(count=len(citations))
        _pcb(phase="citations", status="done", detail=f"{len(citations)} citations")
        stats["citations_count"] = len(citations)

        # ============================================================
        # Finalize
        # ============================================================
        stats["trace"] = trace.to_dict()
        stats["total_ms"] = stats["trace"]["total_ms"]

        log.info(
            "retrieve q=%r vec=%d bm25=%d tree=%d kg=%d merged=%d rerank=%d cites=%d llm_calls=%d total=%dms",
            query[:50],
            len(vector_hits),
            len(bm25_hits),
            len(tree_hits),
            len(kg_hits),
            len(finalized),
            len(picked),
            len(citations),
            stats["trace"]["total_llm_calls"],
            stats["total_ms"],
        )

        return RetrievalResult(
            query=query,
            merged=picked,
            citations=citations,
            vector_hits=vector_hits,
            tree_hits=tree_hits,
            stats=stats,
            query_plan=query_plan,
            kg_context=_kg_context[0],
        )


# ---------------------------------------------------------------------------
# BM25 index builder
# ---------------------------------------------------------------------------


BM25_CACHE_PATH = "./storage/bm25_index.pkl"


def build_bm25_index(
    relational: RelationalStore,
    cfg,  # BM25Config
    *,
    doc_ids: list[str] | None = None,
    cache_path: str = BM25_CACHE_PATH,
    force_rebuild: bool = False,
) -> InMemoryBM25Index:
    """
    Build or load a BM25 index.

    1. Try loading from disk cache (fast, <50ms for 10K chunks)
    2. If cache missing/corrupt/force_rebuild: full rebuild from DB
    3. Save to disk for next startup

    Incremental updates: after ingesting a new doc, call
        index.add(...) + index.finalize() + index.save(cache_path)
    instead of a full rebuild.
    """
    # Try cache first (if path is set)
    if not force_rebuild and cache_path:
        cached = InMemoryBM25Index.load(cache_path, cfg)
        if cached is not None and len(cached) > 0:
            return cached

    # Full rebuild
    t0 = time.time()
    index = InMemoryBM25Index(cfg)

    if doc_ids is None:
        doc_ids = _list_all_doc_ids(relational)

    for doc_id in doc_ids:
        row = relational.get_document(doc_id)
        if not row:
            continue
        pv = row["active_parse_version"]
        chunks = relational.get_chunks(doc_id, pv)
        for c in chunks:
            section = " ".join(c.get("section_path") or [])
            text = c.get("content") or ""
            if section:
                text = section + "\n" + text
            index.add(
                chunk_id=c["chunk_id"],
                doc_id=c["doc_id"],
                text=text,
            )
    index.finalize()
    elapsed = int((time.time() - t0) * 1000)
    log.info("BM25 index built: %d chunks, %d docs, %dms", len(index), len(set(index.doc_ids)), elapsed)

    # Persist for next startup (if path is set)
    if cache_path:
        try:
            index.save(cache_path)
        except Exception as e:
            log.warning("BM25 cache save failed: %s", e)

    return index


def _list_all_doc_ids(relational: RelationalStore) -> list[str]:
    """
    Workaround: RelationalStore doesn't yet expose list_documents().
    We do a direct private call via get_document() loop in the
    future, but for now we return an empty list when unknown; the
    caller should pass doc_ids explicitly. Concrete stores can
    override this by providing their own list.
    """
    lister = getattr(relational, "list_document_ids", None)
    if callable(lister):
        return list(lister())
    return []
