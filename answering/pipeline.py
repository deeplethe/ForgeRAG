"""
AnsweringPipeline: query in, grounded Answer out.

Composition:
    RetrievalPipeline  +  Generator  =  AnsweringPipeline

The pipeline runs retrieval, builds the prompt via prompts.build_messages,
calls the generator, then parses the cited markers back into a
`citations_used` list that the caller can show to the user.

Statistics from both retrieval and generation are merged into
`Answer.stats` for observability.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import time
from uuid import uuid4

from config import AnsweringSection
from persistence.store import Store
from retrieval.pipeline import RetrievalPipeline
from retrieval.telemetry import RequestSpanCollector, get_tracer, spans_to_payload
from retrieval.types import RetrievalResult

from .generator import Generator, make_generator
from .prompts import build_messages
from .types import Answer

_tracer = get_tracer()

log = logging.getLogger(__name__)


class AnsweringPipeline:
    def __init__(
        self,
        cfg: AnsweringSection,
        *,
        retrieval: RetrievalPipeline,
        generator: Generator | None = None,
        store: Store | None = None,
    ):
        self.cfg = cfg
        self.retrieval = retrieval
        self.generator = generator or make_generator(cfg.generator)
        self.store = store  # if set, traces are persisted to DB
        # Per-conversation cache of last retrieval result for reuse
        # on reformulation queries ("用中文回答", "简短一点", etc.)
        # Bounded to prevent unbounded memory growth on long-running
        # processes that serve many conversations.
        self._retrieval_cache: collections.OrderedDict[str, RetrievalResult] = collections.OrderedDict()
        self._cache_max = 200

    def _cache_put(self, conversation_id: str, result: RetrievalResult) -> None:
        """Insert into LRU cache, evicting oldest if over capacity."""
        self._retrieval_cache[conversation_id] = result
        self._retrieval_cache.move_to_end(conversation_id)
        while len(self._retrieval_cache) > self._cache_max:
            self._retrieval_cache.popitem(last=False)

    # ------------------------------------------------------------------
    def ask(
        self,
        query: str,
        *,
        filter: dict | None = None,
        conversation_id: str | None = None,
        overrides=None,
    ) -> Answer:
        # Root span covering retrieval + generation. We extract the
        # trace_id so the route layer can collect all child spans (LLM
        # calls, SQL, HTTPX, retrieval phases) and ship them to the
        # frontend as raw OTel JSON.
        _root_cm = _tracer.start_as_current_span("forgerag.answer")
        _root_span = _root_cm.__enter__()
        _root_span.set_attribute("forgerag.query", (query or "")[:500])
        if conversation_id:
            _root_span.set_attribute("forgerag.conversation_id", conversation_id)
        _trace_id = _root_span.get_span_context().trace_id
        try:
            answer = self._ask_impl(
                query,
                filter=filter,
                conversation_id=conversation_id,
                overrides=overrides,
                _trace_id=_trace_id,
            )
            # Stash trace_id so the route layer can ``collector.take()`` the
            # raw OTel spans for this request after the root span ends.
            answer.stats["otel_trace_id_int"] = _trace_id
            answer.stats["otel_trace_id"] = f"{_trace_id:032x}"
            return answer
        finally:
            _root_cm.__exit__(None, None, None)

    def _ask_impl(
        self,
        query: str,
        *,
        filter: dict | None = None,
        conversation_id: str | None = None,
        overrides=None,
        _trace_id: int | None = None,
    ) -> Answer:
        stats: dict = {}
        t0 = time.time()

        # Persist user message immediately so it's never lost
        if conversation_id and self.store:
            try:
                conv = self.store.get_conversation(conversation_id)
                if not conv:
                    self.store.create_conversation(
                        {
                            "conversation_id": conversation_id,
                            "title": query[:100],
                        }
                    )
                self.store.add_message(
                    {
                        "message_id": uuid4().hex,
                        "conversation_id": conversation_id,
                        "role": "user",
                        "content": query,
                    }
                )
            except Exception as e:
                log.warning("failed to persist user message early: %s", e)

        # Load conversation history for context-aware QU
        chat_history = self._load_history(conversation_id) if conversation_id else []

        # ── Early QU: greeting/meta/reformulation short-circuit ──
        _reuse = False
        _early_qu_done = False
        qp_early = None
        # QueryUnderstanding has no cfg-level toggle anymore; it runs on
        # every retrieve unless the per-request override turns it off.
        qu_enabled = True
        if overrides is not None and getattr(overrides, "query_understanding", None) is not None:
            qu_enabled = bool(overrides.query_understanding)
        if qu_enabled:
            _allow_partial = bool(overrides is not None and getattr(overrides, "allow_partial_failure", None) is True)
            qp_early = self.retrieval.analyze_query(
                query,
                chat_history=chat_history,
                strict=not _allow_partial,
            )
            _early_qu_done = qp_early is not None

            if qp_early and not qp_early.needs_retrieval and not qp_early.direct_answer and conversation_id:
                cached = self._retrieval_cache.get(conversation_id)
                if cached is not None:
                    log.info("reusing cached retrieval (sync) conv=%s intent=%s", conversation_id, qp_early.intent)
                    retrieval_result = cached
                    retrieval_result.query_plan = qp_early
                    stats["retrieval"] = {**cached.stats, "reused": True}
                    _reuse = True
                else:
                    prev_query = self._last_user_query(chat_history)
                    if prev_query:
                        log.info(
                            "reformulation cache miss, re-retrieving with prev query: %s",
                            prev_query[:80],
                        )
                        retrieval_result = self.retrieval.retrieve(
                            prev_query,
                            filter=filter,
                            chat_history=chat_history,
                            precomputed_plan=qp_early,
                            overrides=overrides,
                        )
                        retrieval_result.query_plan = qp_early
                        stats["retrieval"] = retrieval_result.stats
                        if conversation_id:
                            self._cache_put(conversation_id, retrieval_result)
                        _reuse = True

        if not _reuse:
            retrieval_query = self._contextualize_query(query, conversation_id)
            retrieval_result = self.retrieval.retrieve(
                retrieval_query,
                filter=filter,
                chat_history=chat_history,
                precomputed_plan=qp_early if _early_qu_done else None,
                overrides=overrides,
            )
            stats["retrieval"] = retrieval_result.stats
            if conversation_id:
                self._cache_put(conversation_id, retrieval_result)

        messages, used_in_prompt = build_messages(
            query=query,
            merged=retrieval_result.merged,
            citations=retrieval_result.citations,
            cfg=self.cfg.generator,
            include_expanded_chunks=self.cfg.include_expanded_chunks,
            max_chunks=self.cfg.max_chunks,
            kg_context=retrieval_result.kg_context,
        )
        stats["context_chunks"] = len(used_in_prompt)

        # Inject conversation history into messages (multi-turn).
        # Reuse chat_history loaded earlier (no new messages since then).
        if conversation_id and chat_history:
            messages = self._inject_history(messages, chat_history, current_query=query)
            stats["history_turns"] = len(chat_history) // 2

        # If no context chunks AND intent requires documents, refuse.
        # But for greeting/meta/reformulation, let the generator respond freely.
        _intent = (
            qp_early.intent
            if qp_early
            else (retrieval_result.query_plan.intent if retrieval_result.query_plan else None)
        )
        _no_context_ok = _intent in ("greeting", "meta", "reformulation")
        if not used_in_prompt and not _no_context_ok:
            log.info("answering: empty context; returning refusal")
            answer = Answer(
                query=query,
                text=self.cfg.generator.refuse_message,
                citations_used=[],
                citations_all=retrieval_result.citations,
                model=self.cfg.generator.model,
                finish_reason="no_context",
                stats=stats,
            )
            tid = self._persist_trace(answer, retrieval_result, trace_id=_trace_id)
            if conversation_id:
                self._save_turn(conversation_id, query, answer, trace_id=tid)
            return answer

        t1 = time.time()
        gen_result = self.generator.generate(messages)
        t2 = time.time()
        stats["generate_ms"] = int((t2 - t1) * 1000)
        stats["total_ms"] = int((t2 - t0) * 1000)
        if gen_result.get("usage"):
            stats["usage"] = gen_result["usage"]
        if gen_result.get("error"):
            stats["error"] = gen_result["error"]

        cited_ids = set(gen_result.get("cited_ids") or [])
        citations_used = [c for c in used_in_prompt if c.citation_id in cited_ids]

        log.info(
            "answering: ctx=%d cited=%d finish=%s gen_ms=%d conv=%s",
            len(used_in_prompt),
            len(citations_used),
            gen_result.get("finish_reason"),
            stats.get("generate_ms", 0),
            conversation_id or "none",
        )

        # Stash the synthesized KG context (entities + relations injected
        # into the prompt) into stats so downstream consumers — notably
        # the benchmark — can see the full set of material the LLM had
        # available, not just chunk citations. Without this, LLM-judge
        # evaluations mis-score answers grounded in KG synthesis as
        # "hallucinated" because they're not in any citation snippet.
        if retrieval_result.kg_context and not retrieval_result.kg_context.is_empty:
            kg = retrieval_result.kg_context
            stats["kg_context"] = {
                "entities": [{"name": e.get("name", ""), "description": e.get("description", "")} for e in kg.entities],
                "relations": [
                    {
                        "source": r.get("source", ""),
                        "target": r.get("target", ""),
                        "description": r.get("description", ""),
                    }
                    for r in kg.relations
                ],
            }

        answer = Answer(
            query=query,
            text=gen_result.get("text") or "",
            citations_used=citations_used,
            citations_all=retrieval_result.citations,
            model=gen_result.get("model") or self.cfg.generator.model,
            finish_reason=gen_result.get("finish_reason") or "unknown",
            stats=stats,
        )

        # Persist trace + conversation turn
        tid = self._persist_trace(answer, retrieval_result, trace_id=_trace_id)
        if conversation_id:
            self._save_turn(conversation_id, query, answer, trace_id=tid)

        return answer

    # ------------------------------------------------------------------
    # Streaming variant
    # ------------------------------------------------------------------

    def ask_stream(
        self,
        query: str,
        *,
        filter: dict | None = None,
        conversation_id: str | None = None,
        overrides=None,
    ):
        # Wrap the streaming generator in the same ``forgerag.answer`` root
        # span used by ask(). Because this is a generator, we manage the
        # context manager manually — yielding while inside the span keeps
        # child operations correctly parented.
        _root_cm = _tracer.start_as_current_span("forgerag.answer")
        _root_span = _root_cm.__enter__()
        _root_span.set_attribute("forgerag.query", (query or "")[:500])
        _root_span.set_attribute("forgerag.stream", True)
        if conversation_id:
            _root_span.set_attribute("forgerag.conversation_id", conversation_id)
        _trace_id = _root_span.get_span_context().trace_id

        # Snapshot the OTel context with the root span attached, so we can
        # re-attach it on every generator resume. ``StreamingResponse``
        # iterates sync generators on a thread pool — each ``next()`` may
        # land on a different worker thread whose ``contextvars`` is empty,
        # so without an explicit re-attach the LLM / retrieval / SQL spans
        # produced between yields end up parented to nothing (each gets a
        # fresh trace_id) and ``collector.take(_trace_id)`` returns nothing.
        from opentelemetry.context import attach, detach, get_current
        _parent_ctx = get_current()

        _root_closed = False
        # Filled by ``_ask_stream_impl`` when its done handler fires;
        # the actual persist happens *after* we close the root span so
        # the persisted trace_json captures the root + every nested
        # span (LiteLLM completion, retrieval phases, etc.) instead of
        # only the SQL writes ``_persist_trace`` itself produces.
        pending_persist: dict = {}

        def _reattach_each_resume(inner_gen):
            """Re-attach ``_parent_ctx`` on every ``next()`` call into the
            inner generator so the OTel context survives the thread hops
            ``StreamingResponse`` does between yields."""
            while True:
                token = attach(_parent_ctx)
                try:
                    try:
                        value = next(inner_gen)
                    except StopIteration:
                        return
                finally:
                    # ``detach`` raises ValueError if a yield in the
                    # generator caused this token to land in a different
                    # context. Either way the contextvar gets re-attached
                    # on the next iteration, so the failure is benign.
                    with contextlib.suppress(Exception):
                        detach(token)
                yield value

        try:
            inner = self._ask_stream_impl(
                query,
                filter=filter,
                conversation_id=conversation_id,
                overrides=overrides,
                _trace_id=_trace_id,
                _pending_persist=pending_persist,
            )
            yield from _reattach_each_resume(inner)
            # Close the root span BEFORE persist + emit so the root + every
            # in-flight LiteLLM/HTTPX/SQLAlchemy span is committed to the
            # collector and we can claim them.
            _root_cm.__exit__(None, None, None)
            _root_closed = True
            if pending_persist:
                with contextlib.suppress(Exception):
                    self._persist_trace(
                        pending_persist["answer"],
                        pending_persist["retrieval"],
                        trace_id=_trace_id,
                        record_id=pending_persist["tid"],
                    )
                if pending_persist.get("conversation_id"):
                    with contextlib.suppress(Exception):
                        self._save_turn(
                            pending_persist["conversation_id"],
                            pending_persist["query"],
                            pending_persist["answer"],
                            trace_id=pending_persist["tid"],
                        )
            try:
                spans = RequestSpanCollector.singleton().take(_trace_id)
                yield {"event": "trace", "data": spans_to_payload(spans)}
            except Exception:
                log.exception("failed to emit trace SSE event")
        finally:
            if not _root_closed:
                _root_cm.__exit__(None, None, None)

    def _ask_stream_impl(
        self,
        query: str,
        *,
        filter: dict | None = None,
        conversation_id: str | None = None,
        overrides=None,
        _trace_id: int | None = None,
        _pending_persist: dict | None = None,
    ):
        """
        Yield SSE-friendly dicts. Emits progress during retrieval,
        then retrieval metadata, then text deltas, finally done.

        Events:
            {"event": "progress",  "data": {phase, status, detail?}}   (multiple)
            {"event": "retrieval", "data": {vector_hits, tree_hits, ...}}
            {"event": "delta",     "data": {"text": "..."}}
            {"event": "done",      "data": {text, citations_used, citations_all, stats, ...}}
        """
        import queue
        import threading

        stats: dict = {}
        t0 = time.time()

        # Persist user message immediately so it's never lost,
        # even if the client disconnects mid-retrieval.
        _user_msg_id = None
        if conversation_id and self.store:
            try:
                conv = self.store.get_conversation(conversation_id)
                if not conv:
                    self.store.create_conversation(
                        {
                            "conversation_id": conversation_id,
                            "title": query[:100],
                        }
                    )
                _user_msg_id = uuid4().hex
                self.store.add_message(
                    {
                        "message_id": _user_msg_id,
                        "conversation_id": conversation_id,
                        "role": "user",
                        "content": query,
                    }
                )
            except Exception as e:
                log.warning("failed to persist user message early: %s", e)

        # Load conversation history for context-aware QU
        chat_history = self._load_history(conversation_id) if conversation_id else []

        # ── Early QU check: can we skip retrieval entirely? ──
        # For reformulation queries ("用中文回答", "简短一点") we reuse
        # the previous turn's retrieval result instead of re-searching.
        # For greeting/meta we return the direct_answer immediately.
        _reuse_result = None
        _early_qu_done = False  # track whether we already ran QU
        qp_early = None

        # ── Early QU: greeting/meta/reformulation short-circuit ──
        # Query understanding has no ``enabled`` toggle in v0.2.0 — it
        # always runs when the retrieval section configures it. Per-query
        # opt-out goes through ``QueryOverrides.query_understanding=False``.
        qu_enabled = getattr(self.retrieval.cfg, "query_understanding", None) is not None
        if overrides is not None and getattr(overrides, "query_understanding", None) is not None:
            qu_enabled = bool(overrides.query_understanding)
        if qu_enabled:
            yield {"event": "progress", "data": {"phase": "query_understanding", "status": "running"}}
            _allow_partial = bool(overrides is not None and getattr(overrides, "allow_partial_failure", None) is True)
            qp_early = self.retrieval.analyze_query(
                query,
                chat_history=chat_history,
                strict=not _allow_partial,
            )
            _early_qu_done = qp_early is not None

            if qp_early and qp_early.needs_retrieval:
                # Normal path: QU done, proceed to retrieval
                yield {
                    "event": "progress",
                    "data": {
                        "phase": "query_understanding",
                        "status": "done",
                        "detail": f"{qp_early.intent}, {len(qp_early.expanded_queries)} queries",
                    },
                }

            if qp_early and not qp_early.needs_retrieval and not qp_early.direct_answer and conversation_id:
                # Reformulation / no-retrieval intent without direct answer
                cached = self._retrieval_cache.get(conversation_id)
                if cached is not None:
                    log.info(
                        "reusing cached retrieval for conversation %s (intent=%s)",
                        conversation_id,
                        qp_early.intent,
                    )
                    _reuse_result = cached
                    _reuse_result.query_plan = qp_early
                    stats["retrieval"] = {
                        **cached.stats,
                        "reused": True,
                        "reuse_intent": qp_early.intent,
                    }
                    yield {
                        "event": "progress",
                        "data": {
                            "phase": "query_understanding",
                            "status": "done",
                            "detail": f"{qp_early.intent}, reusing previous retrieval",
                        },
                    }
                else:
                    # Cache miss (e.g. server restarted): re-retrieve using
                    # the previous turn's user query instead of the current
                    # reformulation text.
                    prev_query = self._last_user_query(chat_history)
                    if prev_query:
                        log.info(
                            "reformulation cache miss, re-retrieving with prev query: %s",
                            prev_query[:80],
                        )
                        _reuse_result = self.retrieval.retrieve(
                            prev_query,
                            filter=filter,
                            chat_history=chat_history,
                            precomputed_plan=qp_early,
                            overrides=overrides,
                        )
                        _reuse_result.query_plan = qp_early
                        if conversation_id:
                            self._cache_put(conversation_id, _reuse_result)
                        stats["retrieval"] = _reuse_result.stats
                        yield {
                            "event": "progress",
                            "data": {
                                "phase": "query_understanding",
                                "status": "done",
                                "detail": f"{qp_early.intent}, re-retrieved with previous query",
                            },
                        }

        if _reuse_result is not None:
            retrieval_result = _reuse_result
        else:
            # Contextualize short follow-ups for better retrieval (fallback when QU is off)
            retrieval_query = self._contextualize_query(query, conversation_id)

            # Run retrieval in a thread so we can yield progress events
            progress_q: queue.Queue = queue.Queue()
            _retrieval_result = [None]
            _retrieval_error = [None]

            def _on_progress(**info):
                progress_q.put(info)

            # Pass early QU result to avoid duplicate QU call inside retrieve
            _precomputed = qp_early if _early_qu_done else None

            def _do_retrieval():
                try:
                    _retrieval_result[0] = self.retrieval.retrieve(
                        retrieval_query,
                        filter=filter,
                        progress_cb=_on_progress,
                        chat_history=chat_history,
                        precomputed_plan=_precomputed,
                        overrides=overrides,
                    )
                except Exception as e:
                    _retrieval_error[0] = e
                finally:
                    progress_q.put(None)  # sentinel

            t = threading.Thread(target=_do_retrieval, daemon=True)
            t.start()

            # Yield progress events as retrieval runs
            while True:
                try:
                    item = progress_q.get(timeout=120)
                except queue.Empty:
                    break
                if item is None:
                    break
                yield {"event": "progress", "data": item}

            t.join(timeout=300)

            if _retrieval_error[0] is not None:
                raise _retrieval_error[0]
            if _retrieval_result[0] is None:
                raise RuntimeError("Retrieval returned no result (timeout or internal error)")
            retrieval_result = _retrieval_result[0]
            stats["retrieval"] = retrieval_result.stats

            # Cache for potential reuse by next turn
            if conversation_id:
                self._cache_put(conversation_id, retrieval_result)

        qp = retrieval_result.query_plan

        messages, used_in_prompt = build_messages(
            query=query,
            merged=retrieval_result.merged,
            citations=retrieval_result.citations,
            cfg=self.cfg.generator,
            include_expanded_chunks=self.cfg.include_expanded_chunks,
            max_chunks=self.cfg.max_chunks,
            kg_context=retrieval_result.kg_context,
        )
        stats["context_chunks"] = len(used_in_prompt)

        # Inject generation hint from query understanding
        if qp and qp.hint and messages:
            sys_msg = messages[0]
            if sys_msg.get("role") == "system":
                sys_msg["content"] += f"\n\nNote: {qp.hint}"

        # Reuse chat_history loaded earlier (no new messages since then).
        if conversation_id and chat_history:
            messages = self._inject_history(messages, chat_history, current_query=query)

        # Emit retrieval metadata
        yield {
            "event": "retrieval",
            "data": {
                "vector_hits": retrieval_result.stats.get("vector_hits", 0),
                "bm25_hits": retrieval_result.stats.get("bm25_hits", 0),
                "tree_hits": retrieval_result.stats.get("tree_hits", 0),
                "merged_count": retrieval_result.stats.get("merged_count", 0),
                "context_chunks": len(used_in_prompt),
                "intent": qp.intent if qp else None,
                "citations_all": [
                    {
                        "citation_id": c.citation_id,
                        "doc_id": c.doc_id,
                        "file_id": c.file_id,
                        "page_no": c.page_no,
                        "snippet": c.snippet,
                    }
                    for c in retrieval_result.citations
                ],
            },
        }

        _intent_s = (
            qp_early.intent
            if _early_qu_done and qp_early
            else (retrieval_result.query_plan.intent if retrieval_result.query_plan else None)
        )
        _no_ctx_ok = _intent_s in ("greeting", "meta", "reformulation")
        if not used_in_prompt and not _no_ctx_ok:
            answer = Answer(
                query=query,
                text=self.cfg.generator.refuse_message,
                citations_used=[],
                citations_all=retrieval_result.citations,
                model=self.cfg.generator.model,
                finish_reason="no_context",
                stats=stats,
            )
            tid = self._persist_trace(answer, retrieval_result, trace_id=_trace_id)
            if conversation_id:
                self._save_turn(conversation_id, query, answer, trace_id=tid)
            yield {
                "event": "done",
                "data": {
                    "text": self.cfg.generator.refuse_message,
                    "finish_reason": "no_context",
                    "citations_used": [],
                    "model": self.cfg.generator.model,
                    "trace_id": tid,
                },
            }
            return

        # Stream generation
        yield {"event": "progress", "data": {"phase": "generation", "status": "running"}}
        full_text = ""
        _persisted = False
        _gen_model = self.cfg.generator.model
        _finish_reason = "incomplete"
        _cited_ids: set = set()

        full_thinking = ""
        try:
            for chunk in self.generator.generate_stream(messages):
                if chunk["type"] == "delta":
                    full_text += chunk["delta"]
                    yield {"event": "delta", "data": {"text": chunk["delta"]}}
                elif chunk["type"] == "thinking":
                    # Reasoning content (V4-Pro thinking mode etc.). Stream
                    # to the UI so the user sees what the model is
                    # considering during long generation.
                    full_thinking += chunk["delta"]
                    yield {"event": "thinking", "data": {"text": chunk["delta"]}}
                elif chunk["type"] == "done":
                    full_text = chunk.get("text", full_text)
                    full_thinking = chunk.get("thinking", full_thinking)
                    _cited_ids = set(chunk.get("cited_ids") or [])
                    _gen_model = chunk.get("model", _gen_model)
                    _finish_reason = chunk.get("finish_reason", "stop")
                    citations_used = [c for c in used_in_prompt if c.citation_id in _cited_ids]
                    stats["generate_ms"] = int((time.time() - t0) * 1000)
                    if full_thinking:
                        # Land the model's reasoning text in the trace so
                        # it's available for the trace viewer / debug
                        # endpoints alongside the final answer.
                        stats["reasoning_text"] = full_thinking

                    # Pre-allocate the trace UUID so it can flow into the
                    # done event the client sees, but defer the actual
                    # ``_persist_trace`` + ``_save_turn`` writes to ``ask_stream``
                    # — done after the root span closes, so the persisted
                    # trace_json captures every nested span.
                    answer = Answer(
                        query=query,
                        text=full_text,
                        citations_used=citations_used,
                        citations_all=retrieval_result.citations,
                        model=_gen_model,
                        finish_reason=_finish_reason,
                        stats=stats,
                    )
                    tid = uuid4().hex
                    if _pending_persist is not None:
                        _pending_persist.update({
                            "tid": tid,
                            "answer": answer,
                            "retrieval": retrieval_result,
                            "conversation_id": conversation_id,
                            "query": query,
                        })
                    _persisted = True

                    yield {
                        "event": "done",
                        "data": {
                            "text": full_text,
                            "finish_reason": _finish_reason,
                            "model": _gen_model,
                            "trace_id": tid,
                            "citations_used": [
                                {
                                    "citation_id": c.citation_id,
                                    "chunk_id": getattr(c, "chunk_id", ""),
                                    "doc_id": c.doc_id,
                                    "file_id": c.file_id,
                                    "source_file_id": getattr(c, "source_file_id", None),
                                    "source_format": getattr(c, "source_format", ""),
                                    "page_no": c.page_no,
                                    "snippet": c.snippet,
                                    "score": c.score,
                                    "highlights": [
                                        {"page_no": h.page_no, "bbox": list(h.bbox)} for h in (c.highlights or [])
                                    ],
                                }
                                for c in citations_used
                            ],
                            "stats": stats,
                        },
                    }
        except GeneratorExit:
            # Client disconnected mid-stream — fall through to finally
            log.info("ask_stream: client disconnected during generation")
        except Exception as e:
            log.error("ask_stream failed: %s", e)
            try:
                yield {"event": "error", "data": str(e)}
                yield {"event": "done", "data": {"text": "", "finish_reason": "error", "error": str(e)}}
            except GeneratorExit:
                pass
        finally:
            # Persist even if client disconnected, so the answer isn't lost
            if not _persisted and full_text.strip():
                try:
                    stats["generate_ms"] = int((time.time() - t0) * 1000)
                    citations_used = [c for c in used_in_prompt if c.citation_id in _cited_ids]
                    answer = Answer(
                        query=query,
                        text=full_text,
                        citations_used=citations_used,
                        citations_all=retrieval_result.citations,
                        model=_gen_model,
                        finish_reason=_finish_reason,
                        stats=stats,
                    )
                    tid = self._persist_trace(answer, retrieval_result, trace_id=_trace_id)
                    if conversation_id:
                        self._save_turn(
                            conversation_id,
                            query,
                            answer,
                            trace_id=tid,
                        )
                    log.info(
                        "persisted interrupted generation: %d chars, reason=%s",
                        len(full_text),
                        _finish_reason,
                    )
                except Exception as ex:
                    log.warning("failed to persist interrupted result: %s", ex)

    # ------------------------------------------------------------------
    # Multi-turn helpers
    # ------------------------------------------------------------------

    def _contextualize_query(
        self,
        query: str,
        conversation_id: str | None,
    ) -> str:
        """
        Rewrite a follow-up query to be self-contained for retrieval.

        Short/vague queries like "继续讲", "go on", "more details" carry no
        useful keywords for BM25/vector search. By prepending the last user
        query we give the retrieval pipeline enough context to find relevant
        chunks.

        Returns the original query if there's no history or if the query
        already looks self-contained (>30 chars).
        """
        if not conversation_id or not self.store:
            return query
        # Short queries are likely continuations
        if len(query.strip()) > 30:
            return query
        try:
            history = self._load_history(conversation_id)
            # Find last user message in history
            for msg in reversed(history):
                if msg.get("role") == "user":
                    prev = msg.get("content", "").strip()
                    if prev:
                        return f"{prev}\n{query}"
                    break
        except Exception:
            pass
        return query

    @staticmethod
    def _last_user_query(chat_history: list[dict]) -> str | None:
        """Extract the last user query from chat history (excluding the current turn)."""
        # chat_history is ordered oldest→newest; the last user message is
        # the current turn (already persisted), so we want the second-to-last.
        user_msgs = [m for m in chat_history if m.get("role") == "user" and (m.get("content") or "").strip()]
        # user_msgs[-1] = current "用英文作答"; user_msgs[-2] = previous real query
        if len(user_msgs) >= 2:
            return user_msgs[-2]["content"].strip()
        return None

    def _load_history(self, conversation_id: str) -> list[dict]:
        """Load recent messages for this conversation."""
        if not self.store:
            return []
        try:
            msgs = self.store.get_messages(conversation_id, limit=20)
            return msgs
        except Exception:
            return []

    def _inject_history(
        self,
        messages: list[dict],
        history: list[dict],
        *,
        current_query: str | None = None,
    ) -> list[dict]:
        """
        Insert conversation history between system and user messages.
        Trim from the oldest if total exceeds budget.

        Layout:
            [system] → [history user/assistant pairs] → [user with context]

        ``current_query`` is the user query that build_messages already
        rendered into the trailing user message. Because callers persist
        the user message before loading history, the newest entry is the
        same query — drop it here so it doesn't appear twice in the prompt.
        """
        if not history:
            return messages

        # Drop trailing user entries that match the current query.
        if current_query is not None:
            cq = current_query.strip()
            while history and history[-1].get("role") == "user" and (history[-1].get("content") or "").strip() == cq:
                history = history[:-1]
            if not history:
                return messages

        system = messages[0] if messages and messages[0]["role"] == "system" else None
        rest = messages[1:] if system else messages

        # Convert DB messages to chat format
        history_msgs = []
        char_budget = self.cfg.generator.max_context_chars
        # Approximate token budget (chars / 4 as rough estimate)
        max_history_tokens = 2000  # hard cap
        char_budget = min(char_budget, max_history_tokens * 4)

        # Subtract system prompt length from budget
        system_len = len(system["content"]) if system else 0
        used = system_len + sum(len(m.get("content", "")) for m in rest)

        # Walk from newest to oldest, add until budget
        for msg in reversed(history):
            content = msg.get("content", "")
            if used + len(content) > char_budget * 0.4:
                break  # reserve 60% for context + current query
            history_msgs.insert(
                0,
                {
                    "role": msg["role"],
                    "content": content,
                },
            )
            used += len(content)

        result = []
        if system:
            result.append(system)
        result.extend(history_msgs)
        result.extend(rest)
        return result

    def _save_turn(
        self,
        conversation_id: str,
        query: str,
        answer: Answer,
        trace_id: str | None = None,
    ) -> None:
        """Save assistant answer as a message.

        The user message is already persisted at the start of ask_stream/ask,
        so we only save the assistant response here.
        """
        if not self.store:
            return
        try:
            # Ensure conversation exists (for sync `ask()` path)
            conv = self.store.get_conversation(conversation_id)
            if not conv:
                self.store.create_conversation(
                    {
                        "conversation_id": conversation_id,
                        "title": query[:100],
                    }
                )

            # Full citation objects for frontend PDF viewer
            cites_full = []
            for c in answer.citations_used:
                cites_full.append(
                    {
                        "citation_id": c.citation_id,
                        "chunk_id": getattr(c, "chunk_id", ""),
                        "doc_id": c.doc_id,
                        "file_id": c.file_id,
                        "page_no": c.page_no,
                        "snippet": c.snippet,
                        "score": c.score,
                        "highlights": [{"page_no": h.page_no, "bbox": list(h.bbox)} for h in (c.highlights or [])],
                    }
                )

            # Assistant message
            self.store.add_message(
                {
                    "message_id": uuid4().hex,
                    "conversation_id": conversation_id,
                    "role": "assistant",
                    "content": answer.text,
                    "trace_id": trace_id,
                    "citations_json": cites_full,
                    # Persist the model's reasoning text so the Thinking
                    # pane survives conversation switches.
                    "thinking": answer.stats.get("reasoning_text") or None,
                }
            )

            # Update conversation title to first query if not set
            if conv and not conv.get("title"):
                self.store.update_conversation(
                    conversation_id,
                    title=query[:100],
                )
        except Exception as e:
            log.warning("failed to save conversation turn: %s", e)

    # ------------------------------------------------------------------
    def _persist_trace(
        self,
        answer: Answer,
        retrieval_result: RetrievalResult,
        *,
        trace_id: int | None = None,
        record_id: str | None = None,
    ) -> str | None:
        """
        Persist trace and return trace_id (or None on failure).

        ``trace_json`` is a snapshot of all OTel spans produced during this
        ``forgerag.answer`` root span, taken from the in-memory collector.
        We peek-by-copy so the route layer can claim the same spans for
        the live /query response payload.

        ``trace_id`` may be passed explicitly by callers running inside a
        sync generator (the streaming path), where ``yield`` causes
        StreamingResponse to resume on a worker thread that has lost the
        OTel context — ``get_current_span`` then returns NoOp and we'd
        ``take(0)`` an empty buffer. Non-streaming callers can rely on
        the ambient context.
        """
        if self.store is None:
            return None
        try:
            from opentelemetry import trace as _otel_trace

            otel_tid = trace_id
            if otel_tid is None:
                cur = _otel_trace.get_current_span()
                ctx = cur.get_span_context() if cur else None
                otel_tid = ctx.trace_id if (ctx and ctx.is_valid) else None

            trace_data: dict = {}
            if otel_tid is not None:
                collector = RequestSpanCollector.singleton()
                spans = collector.take(otel_tid)
                trace_data = spans_to_payload(spans)
                # Put them back so the route layer can take() later.
                for s in spans:
                    collector.on_end(s)

            trace_data["generation"] = {
                "model": answer.model,
                "finish_reason": answer.finish_reason,
                "latency_ms": answer.stats.get("generate_ms", 0),
                "usage": answer.stats.get("usage"),
                "context_chunks": answer.stats.get("context_chunks"),
                "answer_length": len(answer.text),
                "citations_used": [c.citation_id for c in answer.citations_used],
                "citations_full": [
                    {
                        "citation_id": c.citation_id,
                        "chunk_id": getattr(c, "chunk_id", ""),
                        "doc_id": c.doc_id,
                        "file_id": c.file_id,
                        "page_no": c.page_no,
                        "snippet": c.snippet,
                        "score": c.score,
                        "highlights": [{"page_no": h.page_no, "bbox": list(h.bbox)} for h in (c.highlights or [])],
                    }
                    for c in answer.citations_used
                ],
            }
            tid = record_id or uuid4().hex
            record = {
                "trace_id": tid,
                "query": answer.query,
                "total_ms": answer.stats.get("total_ms", 0),
                "total_llm_ms": 0,  # aggregate from spans if needed later
                "total_llm_calls": 0,
                "answer_text": answer.text,
                "answer_model": answer.model,
                "finish_reason": answer.finish_reason,
                "citations_used": [c.citation_id for c in answer.citations_used],
                "trace_json": trace_data,
                "metadata_json": {
                    "otel_trace_id": f"{otel_tid:032x}" if otel_tid else None,
                },
            }
            self.store.insert_trace(record)
            return tid
        except Exception as e:
            log.warning("failed to persist trace: %s", e)
            return None
