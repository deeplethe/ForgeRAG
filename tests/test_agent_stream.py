"""
Streaming agent path: ``AgentLoop.stream`` events + the
``POST /api/v1/agent/chat`` SSE endpoint.

What's pinned:

  * Event vocabulary — each loop branch emits the right sequence
    in the right order:
      direct answer  : turn_start, turn_end(direct_answer), answer, done
      single tool    : turn_start, tool.call_start, tool.call_end,
                       turn_end(tools), turn_start, turn_end(direct_answer),
                       answer, done
      parallel tools : turn_start, 2× call_start, 2× call_end (in
                       completion order), turn_end(tools), ...
      budget hit     : ..., turn_start(synthesis_only), turn_end(synthesis),
                       answer, done(stop_reason=max_*)
      error          : ..., done(stop_reason="error")

  * ``done`` is ALWAYS the last event — clients close the stream
    on ``done``.

  * Tool execution order is by future-completion, not submission.
    Fast tools land first; slow tools last. Verified by injecting
    different per-call sleep times.

  * SSE wire format: each event is ``data: <json>\\n\\n``. Headers:
    ``text/event-stream``, ``Cache-Control: no-cache``,
    ``X-Accel-Buffering: no``.

  * 403 on UnauthorizedPath: the resolver raises BEFORE the stream
    opens, so the client gets a clean HTTP 403, not a malformed
    event stream.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.agent import (
    AgentConfig,
    AgentLoop,
    LLMResponse,
    ToolCall,
    build_tool_context,
)
from api.auth import AuthenticatedPrincipal, AuthorizationService
from api.deps import get_state
from api.routes.agent import router as agent_router
from config import RelationalConfig, SQLiteConfig
from config.auth_config import AuthConfig
from persistence.models import (
    AuthUser,
    ChunkRow,
    Document,
    File,
    Folder,
    ParsedBlock,
)
from persistence.store import Store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "agentstr.db")),
    )
    s = Store(cfg)
    s.connect()
    s.ensure_schema(with_vector=False, embedding_dim=1536)
    yield s
    s.close()


@pytest.fixture
def seeded(store: Store) -> dict:
    ids: dict[str, str] = {}
    with store.transaction() as sess:
        for username, role in (("admin", "admin"), ("alice", "user")):
            uid = f"u_{username}"
            ids[username] = uid
            sess.add(
                AuthUser(
                    user_id=uid,
                    username=username,
                    email=f"{username}@example.com",
                    password_hash="x",
                    role=role,
                    status="active",
                    is_active=True,
                )
            )
        sess.flush()
        sess.add(
            Folder(
                folder_id="f_research",
                path="/research",
                path_lower="/research",
                parent_id="__root__",
                name="research",
                shared_with=[{"user_id": ids["alice"], "role": "rw"}],
            )
        )
        sess.flush()
        sess.add(
            File(
                file_id="file_research",
                content_hash="h",
                storage_key="r.pdf",
                original_name="r.pdf",
                display_name="r.pdf",
                size_bytes=1,
                mime_type="application/pdf",
                user_id=ids["alice"],
            )
        )
        sess.flush()
        sess.add(
            Document(
                doc_id="d_research",
                file_id="file_research",
                folder_id="f_research",
                path="/research/r.pdf",
                filename="r.pdf",
                format="pdf",
                active_parse_version=1,
            )
        )
        sess.flush()
        sess.add(
            ParsedBlock(
                block_id="d_research:1:1:1",
                doc_id="d_research",
                parse_version=1,
                page_no=1,
                seq=1,
                bbox_x0=0.0, bbox_y0=0.0, bbox_x1=100.0, bbox_y1=20.0,
                type="paragraph",
                text="alice owns this research chunk.",
                image_storage_key=None,
            )
        )
        sess.add(
            ChunkRow(
                chunk_id="d_research:1:c1",
                doc_id="d_research",
                parse_version=1,
                node_id="node-d_research",
                block_ids=["d_research:1:1:1"],
                content="alice owns this research chunk.",
                content_type="text",
                page_start=1,
                page_end=1,
                token_count=5,
                path="/research/r.pdf",
            )
        )
        sess.commit()
    return {"users": ids}


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubBM25:
    def __init__(self, *, sleep_s: float = 0.0):
        self._sleep = sleep_s

    def __len__(self):
        return 1

    def search_chunks(self, query, top_k, *, allowed_doc_ids=None):
        if self._sleep:
            time.sleep(self._sleep)
        return [("d_research:1:c1", 1.0)]


class _StubEmbedder:
    def embed_texts(self, texts):
        return [[0.0] * 4 for _ in texts]


class _StubVector:
    def __init__(self, *, sleep_s: float = 0.0):
        self._sleep = sleep_s

    def search(self, q_vec, top_k=10, filter=None):
        if self._sleep:
            time.sleep(self._sleep)
        return [{"chunk_id": "d_research:1:c1", "score": 0.9}]


def _state(store: Store, *, bm25_sleep: float = 0.0, vector_sleep: float = 0.0):
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True)),
        authz=AuthorizationService(store),
        _bm25=_StubBM25(sleep_s=bm25_sleep),
        embedder=_StubEmbedder(),
        vector=_StubVector(sleep_s=vector_sleep),
    )


def _alice(seeded):
    return AuthenticatedPrincipal(
        user_id=seeded["users"]["alice"],
        username="alice",
        role="user",
        via="session",
    )


class _StubLLM:
    def __init__(self, responses: list[LLMResponse]):
        self._queue = list(responses)
        self.calls: list[dict] = []

    def chat(self, messages, *, tools=None, tool_choice="auto", temperature=0.0, max_tokens=4096):
        self.calls.append({"messages": list(messages), "tool_choice": tool_choice})
        if not self._queue:
            raise RuntimeError("StubLLM exhausted")
        return self._queue.pop(0)

    def chat_stream(self, messages, *, tools=None, tool_choice="auto", temperature=0.0, max_tokens=4096):
        # Same record + queue as ``chat`` so the existing tests on
        # ``llm.calls`` keep working unchanged. Emit text (if any)
        # as a single ``delta`` chunk, then ``done``.
        resp = self.chat(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if resp.text:
            yield ("delta", resp.text)
        yield ("done", resp)


def _bm25_call(call_id="c1", query="research"):
    # See test_agent_loop.py for rationale — production registry
    # omits search_bm25; tests/conftest.py's autouse fixture
    # re-registers it so this helper exercises the BM25 dispatch
    # path on the underlying handler.
    return ToolCall(id=call_id, name="search_bm25", arguments={"query": query})


def _vector_call(call_id="c2", query="research"):
    return ToolCall(id=call_id, name="search_vector", arguments={"query": query})


# ---------------------------------------------------------------------------
# Direct event sequencing
# ---------------------------------------------------------------------------


class TestStreamEvents:
    def test_direct_answer_event_sequence(self, store, seeded):
        llm = _StubLLM([LLMResponse(text="hi there", tool_calls=[])])
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        events = list(loop.stream("hi", ctx))
        types = [e["type"] for e in events]
        # Main turn now streams via ``_stream_main_turn`` (with
        # DSML head-buffer protection — see loop.py). Direct-answer
        # text flows through ``answer.delta`` first, then the
        # final aggregated ``answer`` event for non-streaming
        # consumers.
        assert types == [
            "agent.turn_start",
            "answer.delta",
            "agent.turn_end",
            "answer",
            "done",
        ]
        assert events[1]["text"] == "hi there"
        assert events[2]["decision"] == "direct_answer"
        assert events[2]["tools_called"] == 0
        assert events[3]["text"] == "hi there"
        assert events[4]["stop_reason"] == "done"

    def test_single_tool_event_sequence(self, store, seeded):
        llm = _StubLLM(
            [
                LLMResponse(text="", tool_calls=[_bm25_call()]),
                LLMResponse(text="answer", tool_calls=[]),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        events = list(loop.stream("?", ctx))
        types = [e["type"] for e in events]
        # Tool-decision turn 1 has empty text → no answer.delta.
        # Direct-answer turn 2 streams "answer" → one answer.delta
        # then the aggregated answer event.
        assert types == [
            "agent.turn_start",
            "tool.call_start",
            "tool.call_end",
            "agent.turn_end",
            "agent.turn_start",
            "answer.delta",
            "agent.turn_end",
            "answer",
            "done",
        ]
        # tool.call_start carries id + tool + params for the UI to
        # show "calling search_bm25(query=research)".
        cs = events[1]
        assert cs["tool"] == "search_bm25"
        assert cs["params"] == {"query": "research"}
        # tool.call_end carries latency_ms + result_summary.
        ce = events[2]
        assert "latency_ms" in ce
        assert "hit_count" in ce["result_summary"]

    def test_streamed_preface_no_duplicate_thought_event(self, store, seeded):
        """When the LLM returns text + tool_calls and the streaming
        layer delivers the text as deltas, NO redundant
        ``agent.thought`` event fires — the frontend already has
        the text via ``answer.delta`` and promotes it into a thought
        entry when ``tool.call_start`` lands.

        This pins the de-duplication invariant: streamed text is
        the single source of truth for the chain UI on clean
        providers; ``agent.thought`` is reserved for the
        DSML-fallback case where deltas were suppressed.
        """
        llm = _StubLLM(
            [
                LLMResponse(
                    text="Let me search the corpus for that.",
                    tool_calls=[_bm25_call()],
                ),
                LLMResponse(text="answer", tool_calls=[]),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        events = list(loop.stream("?", ctx))
        types = [e["type"] for e in events]
        # Preface streams as ``answer.delta`` BEFORE tool.call_start.
        # No ``agent.thought`` event — the deltas already carried
        # the text.
        assert "agent.thought" not in types
        assert types == [
            "agent.turn_start",
            "answer.delta",
            "tool.call_start",
            "tool.call_end",
            "agent.turn_end",
            "agent.turn_start",
            "answer.delta",
            "agent.turn_end",
            "answer",
            "done",
        ]
        # Delta text matches the LLM's preface — frontend uses this
        # to populate the chain's thought entry.
        assert events[1]["text"] == "Let me search the corpus for that."

    def test_dsml_leak_falls_back_and_emits_thought(self, store, seeded):
        """Simulate a DeepSeek-style DSML leak: the streaming call
        surfaces tool-call markup in content with ``tool_calls=[]``.
        Defence path:
          1. Head buffer sees ``<|DSML`` sentinel within first ~32
             chars → ``_stream_main_turn`` swallows the rest of the
             stream, no ``answer.delta`` events for this turn.
          2. Falls back to ``chat()`` which the (well-behaved)
             stub returns with text + proper tool_calls.
          3. Loop emits ``agent.thought`` for the text (since no
             deltas streamed) and ``tool.call_start`` for the tool.
        """
        # Stream call returns DSML in content with no tool_calls.
        # Fallback chat call returns the same intent properly parsed.
        leaky_stream_resp = LLMResponse(
            text='<|DSML|invoke name="search_bm25"><parameter name="query">research</parameter></invoke>',
            tool_calls=[],
        )
        clean_fallback_resp = LLMResponse(
            text="Looking up research on that.",
            tool_calls=[_bm25_call()],
        )

        class _DSMLStubLLM:
            def __init__(self, stream_resp, chat_resp, final_resp):
                self._stream_resp = stream_resp
                self._chat_resp = chat_resp
                self._final_resp = final_resp
                self.chat_calls = 0
                self.stream_calls = 0

            def chat(self, messages, *, tools=None, tool_choice="auto",
                     temperature=0.0, max_tokens=4096):
                self.chat_calls += 1
                # Distinguish: first chat() call is the DSML fallback
                # (still has tools, tool_choice="auto"). Second is the
                # direct-answer turn (also "auto" but no DSML this
                # time — exercised via chat_stream).
                if self.chat_calls == 1:
                    return self._chat_resp
                return self._final_resp

            def chat_stream(self, messages, *, tools=None, tool_choice="auto",
                            temperature=0.0, max_tokens=4096):
                self.stream_calls += 1
                if self.stream_calls == 1:
                    # Yield DSML content as one delta — simulates
                    # DeepSeek's leak. The head-buffer should catch
                    # ``<|DSML`` and switch to suppress mode.
                    yield ("delta", self._stream_resp.text)
                    yield ("done", self._stream_resp)
                else:
                    # Final direct-answer turn streams cleanly.
                    if self._final_resp.text:
                        yield ("delta", self._final_resp.text)
                    yield ("done", self._final_resp)

        llm = _DSMLStubLLM(
            stream_resp=leaky_stream_resp,
            chat_resp=clean_fallback_resp,
            final_resp=LLMResponse(text="ok", tool_calls=[]),
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        events = list(loop.stream("?", ctx))
        types = [e["type"] for e in events]
        # No DSML markup leaked to the user as deltas — it was
        # caught by the head buffer. The reasoning surfaces via
        # agent.thought instead.
        for e in events:
            if e["type"] == "answer.delta":
                assert "<|DSML" not in e["text"]
                assert "<|" not in e["text"]
        assert "agent.thought" in types
        thought_evt = next(e for e in events if e["type"] == "agent.thought")
        assert thought_evt["text"] == "Looking up research on that."
        # Tool was actually dispatched (proves fallback restored
        # the tool_calls list).
        assert any(e["type"] == "tool.call_start" for e in events)
        # The fallback ``chat()`` call ran.
        assert llm.chat_calls >= 1

    def test_dsml_leak_on_synthesis_turn_falls_back(self, store, seeded):
        """Repro for the user-reported bug:
            "Reasoned for 0s · 8 tools" → forced synthesis →
            answer body = ``<||DSML||tool_calls> <||DSML||invoke
            name="search_vector"> …``

        After max_tool_calls hits, the loop runs a synthesis turn
        (tools=None, tool_choice="none"). DeepSeek-V4-Pro can still
        emit DSML markup as content. The defence filter must catch
        the markup, suppress the deltas, and fall back to a
        non-streaming chat() call to produce a clean answer.
        """
        # 8 tool turns exhaust max_tool_calls=8 → synthesis fires.
        # Synthesis stream emits DSML; fallback chat() returns clean.
        bm25_responses = [
            LLMResponse(text="", tool_calls=[_bm25_call(f"c{i}")])
            for i in range(8)
        ]
        leaky_synthesis = LLMResponse(
            text='<||DSML||tool_calls>\n<||DSML||invoke name="search_vector">'
                 '<||DSML||parameter name="query">stuff</||DSML||parameter>\n'
                 '</||DSML||invoke>\n</||DSML||tool_calls>',
            tool_calls=[],
        )
        clean_fallback = LLMResponse(
            text="Final synthesised answer based on search results.",
            tool_calls=[],
        )

        class _BudgetDSMLStub:
            def __init__(self, responses, synth_stream, synth_fallback):
                self._queue = list(responses)
                self._synth_stream = synth_stream
                self._synth_fallback = synth_fallback
                self.synth_chat_calls = 0

            def chat(self, messages, *, tools=None, tool_choice="auto",
                     temperature=0.0, max_tokens=4096):
                # Synthesis fallback comes through as tool_choice=none.
                if tool_choice == "none":
                    self.synth_chat_calls += 1
                    return self._synth_fallback
                if not self._queue:
                    raise RuntimeError("Stub exhausted")
                return self._queue.pop(0)

            def chat_stream(self, messages, *, tools=None, tool_choice="auto",
                            temperature=0.0, max_tokens=4096):
                if tool_choice == "none":
                    # Synthesis stream — emit DSML as content.
                    if self._synth_stream.text:
                        yield ("delta", self._synth_stream.text)
                    yield ("done", self._synth_stream)
                    return
                # Tool-decision turn: pop next response and emit
                # text-then-done. Stub responses are always text="" +
                # tool_calls so no deltas land.
                if not self._queue:
                    raise RuntimeError("Stub exhausted")
                r = self._queue.pop(0)
                if r.text:
                    yield ("delta", r.text)
                yield ("done", r)

        llm = _BudgetDSMLStub(bm25_responses, leaky_synthesis, clean_fallback)
        cfg = AgentConfig(max_tool_calls=8, max_iterations=20)
        loop = AgentLoop(cfg, llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        events = list(loop.stream("?", ctx))

        # Critical invariant: NO ``<|`` / DSML markup in any
        # answer.delta or answer event the user would see.
        for e in events:
            if e["type"] in ("answer.delta", "answer"):
                assert "DSML" not in e.get("text", ""), e
                assert "<|" not in e.get("text", ""), e
                assert "tool_calls>" not in e.get("text", ""), e

        # The fallback chat() ran (tool_choice=none).
        assert llm.synth_chat_calls == 1

        # Final answer is the clean fallback text — proves the
        # filter+fallback recovered a usable response.
        done = events[-1]
        assert done["type"] == "done"
        assert done["answer"] == "Final synthesised answer based on search results."

    def test_no_thought_event_when_preface_empty(self, store, seeded):
        """Tool turns with empty resp.text produce NO agent.thought
        event AND no answer.delta — pins the contract the frontend
        reasoning-chain renderer relies on (no text → don't push a
        thought entry, don't accumulate empty deltas)."""
        llm = _StubLLM(
            [
                LLMResponse(text="", tool_calls=[_bm25_call()]),
                LLMResponse(text="answer", tool_calls=[]),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        events = list(loop.stream("?", ctx))
        types = {e["type"] for e in events}
        assert "agent.thought" not in types

    def test_parallel_tool_calls_complete_in_speed_order(self, store, seeded):
        """Make BM25 fast (~5ms) and vector slow (~120ms). The
        tool.call_end events MUST land in speed order, NOT
        submission order — that's the user-visible win of
        ``as_completed`` over the previous ``futures.items()``
        pattern."""
        llm = _StubLLM(
            [
                LLMResponse(
                    text="",
                    tool_calls=[_bm25_call("a"), _vector_call("b")],
                ),
                LLMResponse(text="ok", tool_calls=[]),
            ]
        )
        state = _state(store, vector_sleep=0.12)
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(state, _alice(seeded))
        events = list(loop.stream("?", ctx))
        # Get the call_end events in order.
        call_ends = [e for e in events if e["type"] == "tool.call_end"]
        assert [e["tool"] for e in call_ends] == ["search_bm25", "search_vector"]

    def test_budget_max_tool_calls_emits_synthesis_turn(self, store, seeded):
        responses = [
            LLMResponse(text="", tool_calls=[_bm25_call(f"c{i}")]) for i in range(3)
        ]
        responses.append(LLMResponse(text="best effort", tool_calls=[]))
        llm = _StubLLM(responses)
        cfg = AgentConfig(max_tool_calls=3, max_iterations=20)
        loop = AgentLoop(cfg, llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        events = list(loop.stream("?", ctx))
        # Synthesis turn now streams its output as a delta. Last 5
        # events: turn_start (synthesis_only=True), answer.delta,
        # turn_end (synthesis), answer (full), done.
        types_tail = [e["type"] for e in events[-5:]]
        assert types_tail == [
            "agent.turn_start",
            "answer.delta",
            "agent.turn_end",
            "answer",
            "done",
        ]
        assert events[-5]["synthesis_only"] is True
        assert events[-3]["decision"] == "synthesis"
        assert events[-1]["stop_reason"] == "max_tool_calls"

    def test_done_always_last(self, store, seeded):
        """No matter the path, ``done`` is always the final event.
        Critical for the SSE client's stream-close logic."""
        # Three different paths, all should end with done.
        for responses in (
            [LLMResponse(text="hi", tool_calls=[])],  # direct
            [
                LLMResponse(text="", tool_calls=[_bm25_call()]),
                LLMResponse(text="ok", tool_calls=[]),
            ],
        ):
            llm = _StubLLM(responses)
            loop = AgentLoop(AgentConfig(), llm)
            ctx = build_tool_context(_state(store), _alice(seeded))
            events = list(loop.stream("?", ctx))
            assert events[-1]["type"] == "done"

    def test_done_event_carries_full_summary(self, store, seeded):
        llm = _StubLLM(
            [
                LLMResponse(text="", tool_calls=[_bm25_call()]),
                LLMResponse(text="found it", tool_calls=[], tokens_in=10, tokens_out=5),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        events = list(loop.stream("?", ctx))
        done = events[-1]
        assert done["stop_reason"] == "done"
        assert done["answer"] == "found it"
        assert done["iterations"] == 2
        assert done["tool_calls_count"] == 1
        # Citation pool surfaced.
        assert any(c["chunk_id"] == "d_research:1:c1" for c in done["citations"])

    def test_run_drains_stream_to_agentresult(self, store, seeded):
        """Backwards compat: run() returns AgentResult assembled
        from the stream's done event. Existing test_agent_loop
        suite covers this path implicitly; this is the explicit
        check that nothing changed shape."""
        llm = _StubLLM([LLMResponse(text="hi", tool_calls=[])])
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("hi", ctx)
        assert result.answer == "hi"
        assert result.stop_reason == "done"
        assert result.iterations == 1


# ---------------------------------------------------------------------------
# /api/v1/agent/chat — SSE wire format
# ---------------------------------------------------------------------------


def _build_app(store: Store, principal: AuthenticatedPrincipal, llm) -> FastAPI:
    """Build a FastAPI app with the agent router + dependency
    overrides so we can drive an SSE round trip without hitting a
    real LLM."""
    state = _state(store)

    # The route constructs its own LiteLLMClient + AgentLoop inside;
    # to swap in a stub LLM we monkeypatch the helper functions
    # via dependency override on the router import.
    from api.routes import agent as agent_route_mod

    def _stub_config(*a, **kw):
        return AgentConfig(model="stub")

    def _stub_llm(*a, **kw):
        return llm

    # Patch the module-level helpers for the test.
    agent_route_mod._agent_config_for = _stub_config
    agent_route_mod._llm_client_for = _stub_llm

    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[get_state] = lambda: state

    @app.middleware("http")
    async def _set_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    return app


def _parse_sse(body: bytes) -> list[dict]:
    """Split an SSE response body into parsed event dicts."""
    out = []
    for chunk in body.decode("utf-8").split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data:"):
            continue
        out.append(json.loads(chunk[5:].strip()))
    return out


class TestSSERoute:
    def test_direct_answer_round_trip(self, store, seeded):
        llm = _StubLLM([LLMResponse(text="hello", tool_calls=[])])
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        with TestClient(app) as c:
            r = c.post("/api/v1/agent/chat", json={"message": "hi"})
        assert r.status_code == 200
        # Headers — the SSE-friendly set.
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["x-accel-buffering"] == "no"
        events = _parse_sse(r.content)
        types = [e["type"] for e in events]
        assert types[0] == "agent.turn_start"
        assert types[-1] == "done"
        # Final answer reached the wire.
        answer_evt = next(e for e in events if e["type"] == "answer")
        assert answer_evt["text"] == "hello"

    def test_tool_round_trip_carries_call_events(self, store, seeded):
        llm = _StubLLM(
            [
                LLMResponse(text="", tool_calls=[_bm25_call()]),
                LLMResponse(text="found", tool_calls=[]),
            ]
        )
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/agent/chat", json={"message": "look it up"}
            )
        events = _parse_sse(r.content)
        types = [e["type"] for e in events]
        assert "tool.call_start" in types
        assert "tool.call_end" in types
        # call_start and call_end share the same id.
        cs = next(e for e in events if e["type"] == "tool.call_start")
        ce = next(e for e in events if e["type"] == "tool.call_end")
        assert cs["id"] == ce["id"]

    def test_history_passed_through(self, store, seeded):
        """Prior conversation turns get prepended to the LLM's
        message list."""
        llm = _StubLLM([LLMResponse(text="ok", tool_calls=[])])
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        body = {
            "message": "now what?",
            "history": [
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "previous reply"},
            ],
        }
        with TestClient(app) as c:
            r = c.post("/api/v1/agent/chat", json=body)
        assert r.status_code == 200
        # Verify the LLM saw both the history AND the new message.
        sent = llm.calls[0]["messages"]
        contents = [m.get("content") for m in sent]
        assert "earlier" in contents
        assert "previous reply" in contents
        assert "now what?" in contents

    def test_unauthorized_path_403(self, store, seeded):
        """An explicit path_filters that the user can't access
        surfaces as 403 BEFORE the stream opens. Without this guard
        the client would receive a half-open SSE stream + a JSON
        error block, which most SSE clients can't parse."""
        llm = _StubLLM([LLMResponse(text="ok", tool_calls=[])])
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/agent/chat",
                json={"message": "?", "path_filters": ["/scratch"]},
            )
        assert r.status_code == 403
        body = r.json()
        assert body["detail"]["error"] == "unauthorized_path"

    def test_empty_message_400(self, store, seeded):
        llm = _StubLLM([LLMResponse(text="ok", tool_calls=[])])
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        with TestClient(app) as c:
            r = c.post("/api/v1/agent/chat", json={"message": ""})
        # FastAPI / pydantic returns 422 for min_length violation.
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Conversation persistence (the multi-turn fix)
# ---------------------------------------------------------------------------


class TestConversationPersistence:
    """Wired in the cutover commit. Goal: frontend doesn't have to
    track history — it just sends a ``conversation_id`` and the
    server loads + persists messages."""

    def test_creates_conversation_on_first_turn(self, store, seeded):
        """Unknown ``conversation_id`` → server creates the row +
        writes user msg + assistant msg. Subsequent calls find the
        conv + load history."""
        llm = _StubLLM(
            [
                LLMResponse(text="first answer", tool_calls=[]),
                LLMResponse(text="second answer", tool_calls=[]),
            ]
        )
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/agent/chat",
                json={"message": "first turn", "conversation_id": "cv_alice"},
            )
            assert r.status_code == 200
            # Drain the SSE stream so the post-stream persist runs.
            _ = _parse_sse(r.content)

        # Row created, messages written.
        conv = store.get_conversation("cv_alice")
        assert conv is not None
        assert conv["user_id"] == seeded["users"]["alice"]
        msgs = store.get_messages("cv_alice")
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "first turn"
        assert msgs[1]["content"] == "first answer"

    def test_history_loaded_into_second_turn_messages(self, store, seeded):
        """Second turn — server loads prior turns from DB and
        injects them as agent history. The LLM sees the full
        conversation, frontend sent only the new message."""
        llm = _StubLLM(
            [
                LLMResponse(text="first answer", tool_calls=[]),
                LLMResponse(text="second answer", tool_calls=[]),
            ]
        )
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        with TestClient(app) as c:
            c.post(
                "/api/v1/agent/chat",
                json={"message": "first turn", "conversation_id": "cv_x"},
            )
            r2 = c.post(
                "/api/v1/agent/chat",
                json={"message": "second turn", "conversation_id": "cv_x"},
            )
        assert r2.status_code == 200
        # The LLM's second call should have seen the full history
        # (system + first turn user + first answer assistant + new user).
        second_call_msgs = llm.calls[1]["messages"]
        contents = [m.get("content") for m in second_call_msgs]
        assert "first turn" in contents
        assert "first answer" in contents
        assert "second turn" in contents

    def test_cross_user_conversation_404(self, store, seeded):
        """alice's conversation, bob asks → 404. Per S5.2 admins
        also don't bypass, but this path wires only alice."""
        llm = _StubLLM([LLMResponse(text="ok", tool_calls=[])])
        # First turn as alice creates the row.
        app_alice = _build_app(store, _alice(seeded), llm)
        with TestClient(app_alice) as c:
            c.post(
                "/api/v1/agent/chat",
                json={"message": "alice msg", "conversation_id": "cv_priv"},
            )
        # Now bob tries to reuse the id.
        bob = AuthenticatedPrincipal(
            user_id="u_bob",
            username="bob",
            role="user",
            via="session",
        )
        with store.transaction() as sess:
            from persistence.models import AuthUser
            sess.add(
                AuthUser(
                    user_id="u_bob",
                    username="bob",
                    email="bob@example.com",
                    password_hash="x",
                    role="user",
                    status="active",
                    is_active=True,
                )
            )
            sess.commit()
        llm2 = _StubLLM([LLMResponse(text="should not run", tool_calls=[])])
        app_bob = _build_app(store, bob, llm2)
        with TestClient(app_bob) as c:
            r = c.post(
                "/api/v1/agent/chat",
                json={"message": "intrude", "conversation_id": "cv_priv"},
            )
        assert r.status_code == 404
        # Bob's LLM never invoked — privacy gate fired before stream.
        assert llm2.calls == []

    def test_citations_persisted_on_assistant_message(self, store, seeded):
        """When the agent collects chunks via tools, the citation
        pool snapshot lands on the assistant row's
        ``citations_json``. Frontend "click chunk → open PDF" flow
        works on next page-load because the data is in the row."""
        llm = _StubLLM(
            [
                LLMResponse(
                    text="",
                    tool_calls=[_bm25_call()],
                ),
                LLMResponse(text="grounded answer", tool_calls=[]),
            ]
        )
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/agent/chat",
                json={
                    "message": "what does alice own?",
                    "conversation_id": "cv_cite",
                },
            )
            assert r.status_code == 200
            _ = _parse_sse(r.content)
        msgs = store.get_messages("cv_cite")
        assistant = msgs[1]
        assert assistant["role"] == "assistant"
        assert assistant["content"] == "grounded answer"
        cits = assistant.get("citations_json") or []
        assert any(c.get("chunk_id") == "d_research:1:c1" for c in cits)

    def test_no_conversation_id_works_stateless(self, store, seeded):
        """Backwards compat: passing ``history`` without
        ``conversation_id`` still works for one-off / stateless
        callers (tests, scripts, eval harnesses)."""
        llm = _StubLLM([LLMResponse(text="ok", tool_calls=[])])
        principal = _alice(seeded)
        app = _build_app(store, principal, llm)
        with TestClient(app) as c:
            r = c.post(
                "/api/v1/agent/chat",
                json={
                    "message": "now what?",
                    "history": [
                        {"role": "user", "content": "earlier"},
                        {"role": "assistant", "content": "earlier reply"},
                    ],
                },
            )
        assert r.status_code == 200
        # No conversation row created — request was stateless.
        assert store.count_conversations() == 0
