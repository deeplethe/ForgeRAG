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

from config import RetrievalSection
from embedder.base import Embedder
from persistence.store import Store as RelationalStore
from persistence.vector.base import VectorStore

from .bm25 import InMemoryBM25Index
from .citations import build_citations
from .rerank import Reranker, make_reranker
from .telemetry import get_tracer
from .tree_path import TreeNavigator
from .types import RetrievalResult, ScoredChunk

_tracer = get_tracer()


def _doc_id_from_chunk_id(chunk_id: str) -> str:
    """Extract doc_id from a ``{doc_id}:{parse_version}:c{seq}`` chunk_id.

    Falls back to the full id if the format is unexpected.
    """
    parts = chunk_id.rsplit(":", 2)
    return parts[0] if len(parts) == 3 else chunk_id


def _propagate_ctx(fn):
    """
    Decorator: capture the current OTel context when the wrapped function
    is CREATED, re-attach it when the function RUNS. Use this on worker
    closures before handing them to a ThreadPoolExecutor so child spans
    are correctly parented to the current retrieval root span instead of
    becoming orphaned trace-less spans.
    """
    from opentelemetry.context import attach, detach, get_current

    parent_ctx = get_current()

    def wrapper(*args, **kwargs):
        token = attach(parent_ctx)
        try:
            return fn(*args, **kwargs)
        finally:
            detach(token)

    return wrapper


class RetrievalError(RuntimeError):
    """
    Infrastructure failure during retrieval — an LLM call, embedding API,
    KG store, or reranker endpoint errored out.

    By default a single path failure aborts the whole query and surfaces
    here; callers that prefer the legacy "log + zero hits + carry on"
    behaviour can set ``QueryOverrides.allow_partial_failure = True``.

    ``path`` identifies which component failed (``"bm25"`` / ``"vector"``
    / ``"tree"`` / ``"kg"`` / ``"query_understanding"`` / ``"rerank"``).
    ``__cause__`` carries the original exception.
    """

    def __init__(self, path: str, cause: BaseException):
        self.path = path
        super().__init__(f"{path} path failed: {type(cause).__name__}: {cause}")


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

        # ── Composable retrieval components ─────────────────────────────
        # Each owns its slice of the pipeline; this class is now primarily
        # an orchestrator that wires them together with the right concurrency,
        # overrides, and error policy. SDK users can instantiate any subset
        # directly from ``forgerag.retrieval.components`` and assemble their
        # own chain.
        from .components import (
            BM25Retriever,
            ContextExpander,
            KGRetriever,
            PathScopeResolver,
            RerankComponent,
            RRFFusion,
            TreeRetriever,
            VectorRetriever,
        )

        self.c_path_scope = PathScopeResolver(self.rel)
        self.c_bm25 = BM25Retriever(cfg.bm25, self.bm25)
        self.c_vector = VectorRetriever(cfg.vector, embedder=self.embedder, vector_store=self.vector)
        self.c_tree = TreeRetriever(
            cfg.tree_path,
            bm25_cfg=cfg.bm25,
            bm25_index=self.bm25,
            rel=self.rel,
            navigator=self.navigator,
        )
        self.c_kg = KGRetriever(
            cfg.kg_path,
            graph_store=self.graph_store,
            rel=self.rel,
            embedder=self.embedder,
        )
        self.c_fusion = RRFFusion(k=cfg.merge.rrf_k)
        self.c_expand = ContextExpander(cfg.merge, rel=self.rel)
        self.c_rerank = RerankComponent(cfg.rerank, reranker=self.reranker)

    # ------------------------------------------------------------------
    # Path scoping
    # ------------------------------------------------------------------

    def _resolve_path_scope(self, filter: dict | None) -> tuple[str | None, set[str] | None]:
        """
        Resolve the API-level path_filter into:
          1. A path prefix string for backends that support native
             path-prefix filtering (pgvector, Chroma, Neo4j). None means
             "no user-visible scope — match anything except trash".
          2. A doc_id snapshot set for backends that don't store path
             (Python BM25 index, in-memory tree nav). The snapshot is
             resolved once per query from the documents table so all
             retrieval paths see a consistent scope even if a concurrent
             rename commits mid-query.

        Trashed documents are ALWAYS excluded, regardless of path_filter:
        ``_trashed_doc_ids`` is populated as a side effect and used by
        the minimal post-filter step that follows merge.
        """
        from sqlalchemy import select

        from persistence.folder_service import TRASH_PATH
        from persistence.models import Document
        from persistence.pending_ops import or_fallback_prefixes

        raw = (filter or {}).get("_path_filter") if filter else None
        path_prefix: str | None = None
        if raw and raw != "/":
            path_prefix = raw.rstrip("/")

        with self.rel.transaction() as sess:
            # Snapshot doc_ids inside scope (only for BM25/Tree; skip
            # entirely when no path_filter — they'll work on the full set).
            allowed_doc_ids: set[str] | None = None
            if path_prefix is not None:
                allowed_doc_ids = set(
                    sess.execute(
                        select(Document.doc_id).where(
                            (Document.path == path_prefix) | (Document.path.like(path_prefix + "/%"))
                        )
                    ).scalars()
                )

            # Trashed doc_ids — always excluded from all paths
            trashed = set(sess.execute(select(Document.doc_id).where(Document.path.like(TRASH_PATH + "/%"))).scalars())

            # Pending rename OR-fallback: when a big rename is still
            # draining through pending_folder_ops, Chroma/Neo4j haven't
            # seen the rewrite yet, so an incoming query scoped to the
            # NEW path would miss chunks whose denormalised metadata
            # still carries the OLD path. These are the prefixes we
            # tell the stores to ALSO match.
            or_prefixes = or_fallback_prefixes(sess, path_prefix)

        self._trashed_doc_ids = trashed
        if allowed_doc_ids is not None:
            allowed_doc_ids -= trashed
        # Stash for worker closures that can't easily plumb a new
        # argument (kg_path, chroma metadata builder).
        self._or_fallback_prefixes = or_prefixes
        return path_prefix, allowed_doc_ids

    def _drop_trashed_hits(self, hits):
        """
        Drop hits whose chunk lives in a trashed document. Used as a
        post-filter safety net on top of the backend-native path_prefix
        pre-filter. In practice the trashed set is tiny (items waiting
        for nightly purge), so this is cheap even at high QPS.
        """
        trashed = getattr(self, "_trashed_doc_ids", set()) or set()
        if not trashed or not hits:
            return hits
        # Collect chunk_ids that still need doc_id resolution.
        need_lookup = [
            getattr(h, "chunk_id", "")
            for h in hits
            if getattr(h, "doc_id", None) is None and getattr(h, "chunk_id", None)
        ]
        doc_id_by_chunk: dict[str, str] = {}
        if need_lookup:
            try:
                for row in self.rel.get_chunks_by_ids(need_lookup):
                    doc_id_by_chunk[row["chunk_id"]] = row.get("doc_id", "")
            except Exception:
                pass
        out = []
        for h in hits:
            did = getattr(h, "doc_id", None)
            if did is None:
                did = doc_id_by_chunk.get(getattr(h, "chunk_id", ""))
            if did is not None and did in trashed:
                continue
            out.append(h)
        return out

    # ------------------------------------------------------------------
    def analyze_query(
        self,
        query: str,
        *,
        chat_history: list[dict] | None = None,
        strict: bool = True,
    ):
        """Run query understanding only (no retrieval).

        Returns a ``QueryPlan`` on success. By default (``strict=True``)
        any exception from the underlying LLM call bubbles up wrapped in
        ``RetrievalError``; pass ``strict=False`` for the legacy
        "log + return None" behaviour.
        """
        qu_cfg = self.cfg.query_understanding
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
            if strict:
                raise RetrievalError("query_understanding", e) from e
            log.warning("analyze_query failed (strict=False): %s", e)
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
        overrides=None,
    ) -> RetrievalResult:
        # Root span for the whole retrieval. All child spans (phase spans
        # via components, LiteLLM auto-spans inside the workers, SQL
        # spans, HTTPX spans) parent to this one automatically via OTel
        # context. Span lifetime is managed with a manual __enter__ /
        # __exit__ pair so we don't re-indent the whole function body.
        _root_cm = _tracer.start_as_current_span("forgerag.retrieve")
        _root_span = _root_cm.__enter__()
        _root_span.set_attribute("forgerag.query", (query or "")[:500])
        if filter and "_path_filter" in filter:
            _root_span.set_attribute("forgerag.path_filter", filter["_path_filter"])
        if overrides is not None:
            # Record any explicitly-set overrides so the trace shows user intent.
            for ov_name in (
                "query_understanding",
                "kg_path",
                "tree_path",
                "tree_llm_nav",
                "rerank",
                "bm25_top_k",
                "vector_top_k",
                "tree_top_k",
                "kg_top_k",
                "rerank_top_k",
                "candidate_limit",
                "descendant_expansion",
                "sibling_expansion",
                "crossref_expansion",
                "allow_partial_failure",
            ):
                _v = getattr(overrides, ov_name, None)
                if _v is not None:
                    _root_span.set_attribute(f"forgerag.overrides.{ov_name}", _v)
        try:
            return self._retrieve_impl(
                query,
                filter=filter,
                progress_cb=progress_cb,
                chat_history=chat_history,
                precomputed_plan=precomputed_plan,
                overrides=overrides,
            )
        finally:
            _root_cm.__exit__(None, None, None)

    def _retrieve_impl(
        self,
        query: str,
        *,
        filter: dict | None = None,
        progress_cb=None,
        chat_history: list[dict] | None = None,
        precomputed_plan=None,
        overrides=None,
    ) -> RetrievalResult:
        _t_started = time.time()
        stats: dict = {}
        _pcb = progress_cb or (lambda *a, **k: None)

        # ── Per-request overrides: any field left as None falls through
        #    to cfg. Effective values are used throughout this function
        #    instead of raw self.cfg.* reads.
        def _ov(attr: str, default):
            if overrides is None:
                return default
            v = getattr(overrides, attr, None)
            return default if v is None else v

        # query_understanding / kg_path / rerank no longer have a cfg-level
        # ``enabled`` toggle: when their dependencies are configured (LLM
        # creds, graph_store) they always run. Per-query opt-out is via
        # ``QueryOverrides.{query_understanding,kg_path,rerank} = False``.
        eff_qu_on = _ov("query_understanding", True)
        eff_kg_on = _ov("kg_path", True)
        eff_tree_on = _ov("tree_path", self.cfg.tree_path.enabled)
        eff_tree_llm_nav = _ov("tree_llm_nav", self.cfg.tree_path.llm_nav_enabled)
        eff_rerank_on = _ov("rerank", True)

        eff_bm25_top_k = _ov("bm25_top_k", self.cfg.bm25.top_k)
        eff_vector_top_k = _ov("vector_top_k", self.cfg.vector.top_k)
        eff_tree_top_k = _ov("tree_top_k", self.cfg.tree_path.top_k)
        eff_kg_top_k = _ov("kg_top_k", self.cfg.kg_path.top_k)
        eff_rerank_top_k = _ov("rerank_top_k", self.cfg.rerank.top_k)

        eff_candidate_limit = _ov("candidate_limit", self.cfg.merge.candidate_limit)
        eff_desc_expand = _ov("descendant_expansion", self.cfg.merge.descendant_expansion_enabled)
        eff_sib_expand = _ov("sibling_expansion", self.cfg.merge.sibling_expansion_enabled)
        eff_xref_expand = _ov("crossref_expansion", self.cfg.merge.crossref_expansion_enabled)

        # Failure policy: default = fail loud (raise RetrievalError); opt in
        # via overrides.allow_partial_failure to swallow and continue.
        eff_allow_partial = _ov("allow_partial_failure", False)

        # Override asks for LLM tree navigation but the navigator wasn't
        # built at startup. Fail loud only if the client EXPLICITLY set
        # this via overrides — silently downgrading would violate the
        # explicit request. yaml defaults (no override) keep the original
        # warn-and-fallback behaviour so existing deployments don't break
        # when LLMTreeNavigator wiring is absent.
        if eff_tree_llm_nav and self.navigator is None:
            override_set = overrides is not None and getattr(overrides, "tree_llm_nav", None) is True
            if override_set and not eff_allow_partial:
                raise RetrievalError(
                    "tree_llm_nav",
                    RuntimeError(
                        "overrides.tree_llm_nav=true but LLMTreeNavigator is not "
                        "initialised (yaml retrieval.tree_path.llm_nav_enabled=false). "
                        "Set overrides.allow_partial_failure=true to fall back to heuristic."
                    ),
                )
            log.warning("tree_llm_nav requested but LLMTreeNavigator is not initialised; falling back to heuristic")
            eff_tree_llm_nav = False

        # Resolve path-scoping into two complementary representations:
        #   - path_prefix  → passed to SQL/metadata-indexed backends
        #     (pgvector, Chroma, Neo4j) for native prefix filtering.
        #   - allowed_doc_ids → snapshot whitelist for Python backends
        #     (BM25 in-memory index, Tree nav) that don't store path.
        _scope = self.c_path_scope.run(filter)
        path_prefix = _scope.path_prefix
        allowed_doc_ids = _scope.allowed_doc_ids
        # Stash trashed set + or-fallback prefixes for post-filter + Chroma/Neo4j
        self._trashed_doc_ids = _scope.trashed_doc_ids
        self._or_fallback_prefixes = _scope.or_fallback_prefixes
        if filter and "_path_filter" in filter:
            stats["path_filter"] = filter["_path_filter"]
        if allowed_doc_ids is not None:
            stats["path_scope_size"] = len(allowed_doc_ids)

        # ============================================================
        # Phase 0: Query Understanding (intent + routing + expansion)
        # ============================================================
        queries = [query]
        query_plan = precomputed_plan
        _skip_paths: set[str] = set()

        if precomputed_plan is not None:
            # QU already ran upstream — just record its outcome on a span.
            queries = precomputed_plan.expanded_queries or [query]
            _skip_paths = set(precomputed_plan.skip_paths)
            with _tracer.start_as_current_span("forgerag.query_understanding") as span:
                span.set_attributes(
                    {
                        "forgerag.intent": precomputed_plan.intent or "",
                        "forgerag.needs_retrieval": bool(precomputed_plan.needs_retrieval),
                        "forgerag.expanded_count": len(queries),
                        "forgerag.precomputed": True,
                    }
                )
                if precomputed_plan.skip_paths:
                    span.set_attribute("forgerag.skip_paths", list(precomputed_plan.skip_paths))
                if precomputed_plan.latency_ms:
                    span.set_attribute("forgerag.llm.latency_ms", int(precomputed_plan.latency_ms))
                    span.set_attribute("gen_ai.request.model", precomputed_plan.model or "unknown")

        qu_cfg = self.cfg.query_understanding
        if eff_qu_on and precomputed_plan is None:
            _pcb(phase="query_understanding", status="running")
            with _tracer.start_as_current_span("forgerag.query_understanding") as span:
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
                    span.set_attributes(
                        {
                            "forgerag.intent": query_plan.intent or "",
                            "forgerag.needs_retrieval": bool(query_plan.needs_retrieval),
                            "forgerag.expanded_count": len(queries),
                        }
                    )
                    if query_plan.skip_paths:
                        span.set_attribute("forgerag.skip_paths", list(query_plan.skip_paths))
                    if query_plan.latency_ms:
                        span.set_attribute("forgerag.llm.latency_ms", int(query_plan.latency_ms))
                except Exception as e:
                    span.set_attribute("forgerag.error", str(e))
                    if not eff_allow_partial:
                        raise RetrievalError("query_understanding", e) from e
                    log.warning("query understanding failed (allow_partial_failure=True): %s", e)
            _qu_ok = query_plan is not None
            _pcb(
                phase="query_understanding",
                status="done",
                detail=(
                    f"{query_plan.intent}, {len(queries)} queries"
                    if _qu_ok
                    else "skipped (timeout), using original query"
                ),
            )
        stats["expanded_queries"] = queries

        # ── Short-circuit: no retrieval needed ──
        if query_plan and not query_plan.needs_retrieval:
            log.info("query understanding: skipping retrieval (intent=%s)", query_plan.intent)
            stats["total_ms"] = int((time.time() - _t_started) * 1000)
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

        def _run_bm25() -> tuple[list[ScoredChunk], set[str]]:
            _pcb(phase="bm25_path", status="running")
            if not self.cfg.bm25.enabled or "bm25_path" in _skip_paths:
                _pcb(phase="bm25_path", status="done", detail="skipped" if "bm25_path" in _skip_paths else "disabled")
                return [], set()
            result = self.c_bm25.run(
                queries,
                top_k=eff_bm25_top_k,
                allowed_doc_ids=allowed_doc_ids,
            )
            _pcb(phase="bm25_path", status="done", detail=f"{len(result.hits)} hits")
            return result.hits, result.expanded_doc_ids

        def _run_vector() -> tuple[list[ScoredChunk], list]:
            _pcb(phase="vector_path", status="running")
            if not self.cfg.vector.enabled or "vector_path" in _skip_paths:
                _pcb(
                    phase="vector_path", status="done", detail="skipped" if "vector_path" in _skip_paths else "disabled"
                )
                return [], []
            result = self.c_vector.run(
                queries,
                top_k=eff_vector_top_k,
                filter=filter,
                path_prefix=path_prefix,
                or_fallback_prefixes=getattr(self, "_or_fallback_prefixes", None),
            )
            _pcb(phase="vector_path", status="done", detail=f"{len(result.hits)} hits")
            return result.hits, result.raw_hits

        def _run_tree(
            bm25_doc_ids: set[str],
            vector_doc_ids: set[str],
            prefilter_hits: list | None = None,
        ) -> list[ScoredChunk]:
            _pcb(phase="tree_path", status="running")
            if not eff_tree_on or "tree_path" in _skip_paths:
                _pcb(phase="tree_path", status="done", detail="skipped" if "tree_path" in _skip_paths else "disabled")
                return []
            result = self.c_tree.run(
                query,
                bm25_doc_ids=bm25_doc_ids,
                vector_doc_ids=vector_doc_ids,
                prefilter_hits=prefilter_hits,
                allowed_doc_ids=allowed_doc_ids,
                top_k=eff_tree_top_k,
                llm_nav_enabled=eff_tree_llm_nav,
            )
            _pcb(phase="tree_path", status="done", detail=f"{len(result)} hits")
            return result

        _kg_context = [None]  # mutable container for thread result

        def _run_kg() -> list[ScoredChunk]:
            _pcb(phase="kg_path", status="running")
            if not (eff_kg_on and self.graph_store is not None) or "kg_path" in _skip_paths:
                _pcb(phase="kg_path", status="done", detail="skipped" if "kg_path" in _skip_paths else "disabled")
                return []
            result = self.c_kg.run(
                query,
                top_k=eff_kg_top_k,
                allowed_doc_ids=allowed_doc_ids,
                path_prefix=path_prefix,
                path_prefixes_or=getattr(self, "_or_fallback_prefixes", None),
            )
            _kg_context[0] = result.kg_context
            _pcb(phase="kg_path", status="done", detail=f"{len(result.hits)} hits")
            return result.hits

        # Phase 1: Launch BM25 + Vector + KG in parallel. Workers run in
        # ThreadPoolExecutor threads which don't inherit OTel context by
        # default — wrap them so LiteLLM's auto-emitted spans (tokens / cost)
        # correctly parent to our retrieval trace_id.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="retrieval") as _pool:
            bm25_future = _pool.submit(_propagate_ctx(_run_bm25))
            vec_future = _pool.submit(_propagate_ctx(_run_vector))
            kg_future = _pool.submit(_propagate_ctx(_run_kg))

            # Wait for BM25 (fast, <5ms) and Vector (~1s)
            try:
                bm25_hits, expanded_bm25_docs = bm25_future.result(timeout=10)
            except Exception as e:
                _pcb(phase="bm25_path", status="done", detail=f"error: {type(e).__name__}")
                if not eff_allow_partial:
                    raise RetrievalError("bm25", e) from e
                log.warning("BM25 path failed (allow_partial_failure=True): %s", e, exc_info=True)
                bm25_hits, expanded_bm25_docs = [], set()

            try:
                vector_hits, raw_vector_hits = vec_future.result(timeout=30)
            except Exception as e:
                _pcb(phase="vector_path", status="done", detail=f"error: {type(e).__name__}")
                if not eff_allow_partial:
                    raise RetrievalError("vector", e) from e
                log.warning("Vector path failed (allow_partial_failure=True): %s", e, exc_info=True)
                vector_hits, raw_vector_hits = [], []

            # Trashed-doc safety net: BM25 and vector search don't know
            # about /__trash__. Pre-filtering via path_prefix already
            # blocks trash when a non-trash scope is set, but the
            # unscoped case still needs this post-filter pass. It's O(N)
            # over hits, not over the corpus.
            bm25_hits = self._drop_trashed_hits(bm25_hits)
            vector_hits = self._drop_trashed_hits(vector_hits)
            raw_vector_hits = self._drop_trashed_hits(raw_vector_hits)

            # Phase 2: Tree navigation — uses combined doc_ids from
            # BM25 + Vector for cross-validated document routing.
            # Build prefilter hits for heat-map annotations in tree navigator.
            vector_doc_ids = {h.doc_id for h in raw_vector_hits if h.doc_id} | expanded_bm25_docs

            # Build prefilter hits from BM25 + vector results
            from .tree_path import PreFilterHit

            prefilter_hits: list[PreFilterHit] = []
            # Resolve chunk metadata for heat map (node_id, doc_id, snippet).
            # Take top-50 from each path so a deep BM25 list doesn't crowd
            # out the vector signal — the tree navigator wants both as hints.
            _bm25_pf = [s.chunk_id for s in bm25_hits[:50]]
            _vec_pf = [s.chunk_id for s in vector_hits[:50]]
            _prefilter_chunk_ids = list(dict.fromkeys(_bm25_pf + _vec_pf))  # dedup, preserve order
            _prefilter_rows = {}
            if _prefilter_chunk_ids:
                for row in self.rel.get_chunks_by_ids(_prefilter_chunk_ids):
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

            tree_future = _pool.submit(
                _propagate_ctx(_run_tree),
                expanded_bm25_docs,
                vector_doc_ids,
                prefilter_hits,
            )

            try:
                tree_hits = tree_future.result(timeout=60)
            except Exception as e:
                _pcb(phase="tree_path", status="done", detail=f"error: {type(e).__name__}")
                if not eff_allow_partial:
                    raise RetrievalError("tree", e) from e
                log.warning("Tree path failed (allow_partial_failure=True): %s", e, exc_info=True)
                tree_hits = []

            # Collect KG results (may already be done by now)
            try:
                kg_hits = kg_future.result(timeout=30)
            except Exception as e:
                _pcb(phase="kg_path", status="done", detail=f"error: {type(e).__name__}")
                if not eff_allow_partial:
                    raise RetrievalError("kg", e) from e
                log.warning("KG path failed (allow_partial_failure=True): %s", e, exc_info=True)
                kg_hits = []

            # Tree/KG are already pre-filtered by allowed_doc_ids (passed
            # into their search()); here we only need to drop trashed
            # chunks that slipped through in the unscoped case.
            tree_hits = self._drop_trashed_hits(tree_hits)
            kg_hits = self._drop_trashed_hits(kg_hits)

        # Component spans (forgerag.bm25_path / vector_path / tree_path /
        # kg_path) are already emitted from inside the workers via OTel; we
        # only populate the flat stats dict here for answering.pipeline /
        # benchmark consumers that expect counts + top-id previews.
        stats["bm25_hits"] = len(bm25_hits)
        stats["bm25_top_ids"] = [s.chunk_id for s in bm25_hits[:5]]
        stats["vector_hits"] = len(vector_hits)
        stats["vector_top_ids"] = [s.chunk_id for s in vector_hits[:5]]
        stats["kg_hits"] = len(kg_hits)
        stats["kg_top_ids"] = [s.chunk_id for s in kg_hits[:5]]
        stats["tree_hits"] = len(tree_hits)
        stats["tree_top_ids"] = [s.chunk_id for s in tree_hits[:5]]
        stats["vector_doc_ids"] = len(vector_doc_ids)

        # ============================================================
        # Phase 3: RRF merge
        # ============================================================
        # Default architecture: tree + KG are the "reasoning" primary layer;
        # BM25/vector are their pre-filters. RRF fuses whichever primary
        # paths produced hits.
        #
        # Fallback rule: when BOTH tree and KG are absent (either disabled
        # via cfg/overrides, or enabled but produced zero hits), switch
        # straight to an RRF of BM25 + vector. This makes "plain" retrieval
        # a first-class citizen — per-request `overrides.tree_path=false` +
        # `overrides.kg_path=false` gives you a lexical/semantic hybrid
        # search without any reasoning LLM calls.
        # ── Compose RRF inputs based on primary / fallback policy ────
        _pcb(phase="rrf_merge", status="running")
        rrf_inputs: list[list[ScoredChunk]] = []
        active_paths: list[str] = []
        primary_has_hits = bool((eff_tree_on and tree_hits) or (eff_kg_on and kg_hits))
        if primary_has_hits:
            covered_doc_ids: set[str] = set()
            if eff_tree_on and tree_hits:
                rrf_inputs.append(tree_hits)
                active_paths.append("tree")
                covered_doc_ids |= {_doc_id_from_chunk_id(s.chunk_id) for s in tree_hits}
            if eff_kg_on and kg_hits:
                rrf_inputs.append(kg_hits)
                active_paths.append("kg")
                covered_doc_ids |= {_doc_id_from_chunk_id(s.chunk_id) for s in kg_hits}
            # Per-doc supplement: docs that BM25 / vector found but tree/KG
            # didn't visit (e.g. tree_navigable=False, low quality, or just
            # not selected). Without this they'd silently drop out of the
            # answer even though pre-filter saw them.
            if eff_tree_on and tree_hits:
                if self.cfg.vector.enabled and vector_hits:
                    vec_extra = [s for s in vector_hits if _doc_id_from_chunk_id(s.chunk_id) not in covered_doc_ids]
                    if vec_extra:
                        rrf_inputs.append(vec_extra)
                        active_paths.append("vector_supplement")
                if self.cfg.bm25.enabled and bm25_hits:
                    bm25_extra = [s for s in bm25_hits if _doc_id_from_chunk_id(s.chunk_id) not in covered_doc_ids]
                    if bm25_extra:
                        rrf_inputs.append(bm25_extra)
                        active_paths.append("bm25_supplement")
        else:
            # No primary path — fuse BM25 + vector directly. (BM25 / vector
            # are always-on infrastructure paths; no per-request override
            # exposed for them today, so reading cfg directly is correct.)
            if self.cfg.vector.enabled and vector_hits:
                rrf_inputs.append(vector_hits)
                active_paths.append("vector_fallback")
            if self.cfg.bm25.enabled and bm25_hits:
                rrf_inputs.append(bm25_hits)
                active_paths.append("bm25_fallback")

        # ── Fusion ─────────────────────────────────────────────────────
        merged = self.c_fusion.run(rrf_inputs, labels=active_paths) if rrf_inputs else []
        _pcb(phase="rrf_merge", status="done", detail=f"{len(merged)} merged")

        # ── Context expansion ──────────────────────────────────────────
        _pcb(phase="expansion", status="running")
        finalized = self.c_expand.run(
            merged,
            base_top_k=eff_vector_top_k,
            descendant=eff_desc_expand,
            sibling=eff_sib_expand,
            crossref=eff_xref_expand,
            candidate_limit=eff_candidate_limit,
        )
        _pcb(phase="expansion", status="done", detail=f"{len(finalized)} candidates")
        stats["merged_count"] = len(finalized)

        # ── Rerank ─────────────────────────────────────────────────────
        _pcb(phase="rerank", status="running")
        picked, rerank_error = self.c_rerank.run(
            query,
            finalized,
            top_k=eff_rerank_top_k,
            enabled=eff_rerank_on,
        )
        if rerank_error and not eff_allow_partial:
            # Component returns (slice, err); orchestrator decides failure policy.
            raise RetrievalError("rerank", Exception(rerank_error))
        if rerank_error:
            log.warning(
                "rerank phase failed (allow_partial_failure=True) — falling back to RRF order: %s",
                rerank_error,
            )
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
        with _tracer.start_as_current_span("forgerag.citations") as span:
            citations = build_citations(picked, self.rel, self.cfg.citations)
            span.set_attribute("forgerag.citation_count", len(citations))
            span.add_event(
                "chunks_recorded",
                {
                    "forgerag.chunks.source": "citation_builder",
                    "forgerag.chunks.count": len(citations),
                    "forgerag.chunks.ids": [c.citation_id for c in citations[:50]],
                },
            )
        _pcb(phase="citations", status="done", detail=f"{len(citations)} citations")
        stats["citations_count"] = len(citations)

        # ============================================================
        # Finalize
        # ============================================================
        stats["total_ms"] = int((time.time() - _t_started) * 1000)

        log.info(
            "retrieve q=%r vec=%d bm25=%d tree=%d kg=%d merged=%d rerank=%d cites=%d total=%dms",
            query[:50],
            len(vector_hits),
            len(bm25_hits),
            len(tree_hits),
            len(kg_hits),
            len(finalized),
            len(picked),
            len(citations),
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
