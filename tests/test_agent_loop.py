"""
AgentLoop — bounded LLM-driven retrieval orchestration.

What's pinned:

  * Direct-answer path (intent recognition): LLM returns text-only,
    NO tool_use → loop exits in one iteration with stop_reason="done"
    and 0 tools called. This is the "the user said hi, don't run
    BM25" UX win.

  * Single tool call → result fed back → final answer in 2 iterations.

  * Parallel tool calls in one assistant turn (Anthropic pattern):
    LLM emits search_bm25 + search_vector + read_chunk in a single
    response; loop dispatches them concurrently; results merge into
    one tool_calls_log + citation_pool.

  * Multi-turn: tool → tool_result → another tool → another result
    → final text. The conversation messages list grows correctly
    so the LLM has full context each turn.

  * Tool error recovery: the dispatch error dict is fed back as a
    tool_result; LLM picks a different tool the next turn.

  * Budget caps — three independent triggers force a synthesis turn
    (tool_choice="none"):
      - max_tool_calls   : LLM keeps requesting tools, cut at limit
      - max_iterations   : 6 turns each with 1 tool, no resolution
      - max_wall_time_s  : stub LLM that takes 10s × 4 = 40s

  * Citation pool collection: chunks across multiple tool calls roll
    up into AgentResult.citations sorted by score desc; ``sources``
    set serialised to a sorted list (JSON-safe).

  * History injection: prior chat turns prepended after system prompt.

The LLMClient protocol is small — one ``chat`` method — so a
deterministic stub returns ``LLMResponse``s in sequence. No live
LLM calls. Production end-to-end is left to manual smoke + the
benchmark suite once step 4 (SSE) is in.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from api.agent import (
    AgentConfig,
    AgentLoop,
    LLMResponse,
    ToolCall,
    build_tool_context,
)
from api.auth import AuthenticatedPrincipal, AuthorizationService
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
# Fixtures (same shape as test_agent_dispatch — alice owns
# /research, bob owns /scratch, plus a trashed doc).
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> Store:
    cfg = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "agentl.db")),
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
                bbox_x0=0.0,
                bbox_y0=0.0,
                bbox_x1=100.0,
                bbox_y1=20.0,
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
    def __init__(self):
        self._rows = [
            ("d_research:1:c1", "d_research", "alice owns this research chunk."),
        ]

    def __len__(self):
        return len(self._rows)

    def search_chunks(self, query, top_k, *, allowed_doc_ids=None):
        out = []
        for cid, did, content in self._rows:
            if allowed_doc_ids is not None and did not in allowed_doc_ids:
                continue
            if any(t in content.lower() for t in query.lower().split()):
                out.append((cid, 1.0))
        return out[:top_k]


class _StubEmbedder:
    def embed_texts(self, texts):
        return [[0.0] * 4 for _ in texts]


class _StubVector:
    def search(self, q_vec, top_k=10, filter=None):
        return [{"chunk_id": "d_research:1:c1", "score": 0.9}]


def _state(store: Store):
    return SimpleNamespace(
        store=store,
        cfg=SimpleNamespace(auth=AuthConfig(enabled=True)),
        authz=AuthorizationService(store),
        _bm25=_StubBM25(),
        embedder=_StubEmbedder(),
        vector=_StubVector(),
    )


def _alice(seeded):
    return AuthenticatedPrincipal(
        user_id=seeded["users"]["alice"],
        username="alice",
        role="user",
        via="session",
    )


@dataclass
class _RecordedCall:
    """Snapshot of one chat() invocation — for asserting the loop
    builds messages / tools correctly."""

    messages: list[dict]
    tools: list[dict] | None
    tool_choice: str


class _StubLLM:
    """Returns canned LLMResponse objects in order. Each call gets
    the next response off the queue. ``calls`` records what the
    loop actually sent — drives test assertions about message
    construction.

    Optional ``per_call_delay`` simulates LLM latency for budget
    tests against ``max_wall_time_s``.
    """

    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        per_call_delay: float = 0.0,
        side_effect: Callable[[int], None] | None = None,
    ):
        self._queue = list(responses)
        self.calls: list[_RecordedCall] = []
        self.per_call_delay = per_call_delay
        self.side_effect = side_effect

    def chat(
        self,
        messages,
        *,
        tools=None,
        tool_choice="auto",
        temperature=0.0,
        max_tokens=4096,
    ):
        self.calls.append(
            _RecordedCall(
                messages=list(messages), tools=list(tools) if tools else None,
                tool_choice=tool_choice,
            )
        )
        if self.per_call_delay:
            time.sleep(self.per_call_delay)
        if self.side_effect:
            self.side_effect(len(self.calls) - 1)
        if not self._queue:
            raise RuntimeError("StubLLM exhausted")
        return self._queue.pop(0)


def _bm25_call(call_id="c1", query="research") -> ToolCall:
    return ToolCall(id=call_id, name="search_bm25", arguments={"query": query})


def _vector_call(call_id="c2", query="research") -> ToolCall:
    return ToolCall(id=call_id, name="search_vector", arguments={"query": query})


def _read_call(call_id="c3", chunk_id="d_research:1:c1") -> ToolCall:
    return ToolCall(id=call_id, name="read_chunk", arguments={"chunk_id": chunk_id})


# ---------------------------------------------------------------------------
# Direct-answer (intent recognition)
# ---------------------------------------------------------------------------


class TestDirectAnswer:
    def test_no_tools_one_iteration(self, store, seeded):
        """User says 'hi', LLM answers directly without retrieving.
        This is the headline UX win over the old fixed pipeline
        which ran BM25+vector+KG+rerank on every message."""
        llm = _StubLLM([LLMResponse(text="Hello! How can I help?", tool_calls=[])])
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("hi", ctx)
        assert result.answer == "Hello! How can I help?"
        assert result.stop_reason == "done"
        assert result.iterations == 1
        assert result.tool_calls_count == 0
        assert result.citations == []
        # Exactly one LLM call, with the user message and tool catalogue.
        assert len(llm.calls) == 1
        first = llm.calls[0]
        assert first.tool_choice == "auto"
        assert first.tools is not None and len(first.tools) > 0
        assert first.messages[-1] == {"role": "user", "content": "hi"}


# ---------------------------------------------------------------------------
# Tool dispatch — single + parallel + multi-turn
# ---------------------------------------------------------------------------


class TestToolDispatch:
    def test_single_tool_then_answer(self, store, seeded):
        llm = _StubLLM(
            [
                LLMResponse(text="", tool_calls=[_bm25_call()]),
                LLMResponse(text="Found it: alice owns research.", tool_calls=[]),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("what does alice own?", ctx)
        assert result.stop_reason == "done"
        assert result.iterations == 2
        assert result.tool_calls_count == 1
        assert "alice owns research" in result.answer
        # Citation pool seeded from the BM25 hit.
        assert any(c["chunk_id"] == "d_research:1:c1" for c in result.citations)

    def test_parallel_tools_in_one_turn(self, store, seeded):
        """LLM emits search_bm25 + search_vector simultaneously.
        Both run concurrently; both results feed back as tool_result
        messages on the next turn."""
        llm = _StubLLM(
            [
                LLMResponse(
                    text="",
                    tool_calls=[_bm25_call("a"), _vector_call("b")],
                ),
                LLMResponse(text="Synthesised answer.", tool_calls=[]),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("what about research?", ctx)
        assert result.stop_reason == "done"
        assert result.iterations == 2
        assert result.tool_calls_count == 2
        # Both tools logged.
        tool_names = [c["tool"] for c in result.tool_calls_log]
        assert tool_names == ["search_bm25", "search_vector"]
        # Same chunk hit by both → merged sources in the pool.
        rec = next(
            c for c in result.citations if c["chunk_id"] == "d_research:1:c1"
        )
        assert "bm25" in rec["sources"]
        assert "vector" in rec["sources"]

    def test_multi_turn_tool_then_read(self, store, seeded):
        """search_bm25 → read_chunk → answer. Three turns, two
        tool calls. Verifies the conversation history is built
        correctly across turns (assistant message with tool_calls
        + tool_result content)."""
        llm = _StubLLM(
            [
                LLMResponse(text="", tool_calls=[_bm25_call("a")]),
                LLMResponse(text="", tool_calls=[_read_call("b")]),
                LLMResponse(text="Final.", tool_calls=[]),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("dig in", ctx)
        assert result.stop_reason == "done"
        assert result.iterations == 3
        assert result.tool_calls_count == 2
        # Inspect the second LLM call — it should have seen the
        # tool_result for the BM25 search appended.
        second_call_messages = llm.calls[1].messages
        roles = [m["role"] for m in second_call_messages]
        assert "assistant" in roles
        assert "tool" in roles


# ---------------------------------------------------------------------------
# Tool error recovery
# ---------------------------------------------------------------------------


class TestToolError:
    def test_error_result_fed_back_to_llm(self, store, seeded):
        """LLM asks for an unknown tool; dispatch returns an error
        dict; the LLM sees it on the next turn and recovers."""
        llm = _StubLLM(
            [
                LLMResponse(
                    text="",
                    tool_calls=[
                        ToolCall(id="x", name="nonexistent_tool", arguments={}),
                    ],
                ),
                LLMResponse(text="Recovered.", tool_calls=[]),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("?", ctx)
        assert result.stop_reason == "done"
        # The recovery turn's last tool message should carry the
        # error JSON so the LLM can read what went wrong.
        recovery_messages = llm.calls[1].messages
        last_tool_msg = next(
            m for m in reversed(recovery_messages) if m["role"] == "tool"
        )
        assert "unknown" in last_tool_msg["content"]


# ---------------------------------------------------------------------------
# Budget caps
# ---------------------------------------------------------------------------


class TestBudgetCaps:
    def test_max_tool_calls(self, store, seeded):
        """LLM keeps asking for tools forever. Loop must cut at
        max_tool_calls and force a synthesis turn (tool_choice="none").

        With max_tool_calls=3 and 1 tool per turn: 3 normal turns
        consume the budget, then iter 4's pre-flight check trips →
        synthesise. So queue is 3 tool-call responses + 1 synth.
        """
        responses: list[LLMResponse] = [
            LLMResponse(text="", tool_calls=[_bm25_call(f"c{i}", "x")])
            for i in range(3)
        ]
        responses.append(LLMResponse(text="Best effort answer.", tool_calls=[]))
        llm = _StubLLM(responses)
        cfg = AgentConfig(max_tool_calls=3, max_iterations=20)
        loop = AgentLoop(cfg, llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("?", ctx)
        assert result.stop_reason == "max_tool_calls"
        assert result.tool_calls_count == 3
        assert result.answer == "Best effort answer."
        # Last LLM call should have tool_choice="none".
        assert llm.calls[-1].tool_choice == "none"

    def test_max_iterations(self, store, seeded):
        """Hit the LLM-turn cap before tool count or wall-time."""
        responses: list[LLMResponse] = [
            LLMResponse(text="", tool_calls=[_bm25_call(f"c{i}")]) for i in range(10)
        ]
        responses.append(LLMResponse(text="Synth.", tool_calls=[]))
        llm = _StubLLM(responses)
        cfg = AgentConfig(max_iterations=2, max_tool_calls=20)
        loop = AgentLoop(cfg, llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("?", ctx)
        assert result.stop_reason == "max_iterations"
        assert result.iterations == 2

    def test_max_wall_time(self, store, seeded):
        """Stub LLM sleeps ~0.15s per call; with max_wall_time_s=0.1
        the second iteration's pre-flight check should trip."""
        responses: list[LLMResponse] = [
            LLMResponse(text="", tool_calls=[_bm25_call("a")]),
            LLMResponse(text="", tool_calls=[_bm25_call("b")]),
            LLMResponse(text="Synth.", tool_calls=[]),
        ]
        llm = _StubLLM(responses, per_call_delay=0.15)
        cfg = AgentConfig(max_wall_time_s=0.1, max_iterations=10, max_tool_calls=10)
        loop = AgentLoop(cfg, llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("?", ctx)
        assert result.stop_reason == "max_wall_time"

    def test_excess_tool_calls_in_one_turn_get_truncated(self, store, seeded):
        """LLM emits 5 tool_use blocks in one message but
        max_tool_calls=2. Loop must execute only 2 and feed back
        only those tool_results — protecting against the 'spam tool
        calls in one message' bypass."""
        many = [
            ToolCall(id=f"c{i}", name="search_bm25", arguments={"query": "x"})
            for i in range(5)
        ]
        responses: list[LLMResponse] = [
            LLMResponse(text="", tool_calls=many),
            LLMResponse(text="ok.", tool_calls=[]),
        ]
        llm = _StubLLM(responses)
        cfg = AgentConfig(max_tool_calls=2, max_iterations=10)
        loop = AgentLoop(cfg, llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("?", ctx)
        # Exactly 2 dispatched.
        assert result.tool_calls_count == 2
        # Next turn should see 2 tool_result messages.
        second_call = llm.calls[1]
        tool_msgs = [m for m in second_call.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 2


# ---------------------------------------------------------------------------
# Citations + history
# ---------------------------------------------------------------------------


class TestCitationsAndHistory:
    def test_citations_sorted_by_score(self, store, seeded):
        # Seed two chunks via direct DB insert so we have multiple
        # to rank in the pool.
        with store.transaction() as sess:
            sess.add(
                ParsedBlock(
                    block_id="d_research:1:1:2",
                    doc_id="d_research",
                    parse_version=1,
                    page_no=2,
                    seq=2,
                    bbox_x0=0.0, bbox_y0=0.0, bbox_x1=100.0, bbox_y1=20.0,
                    type="paragraph",
                    text="another bit of research content.",
                    image_storage_key=None,
                )
            )
            sess.add(
                ChunkRow(
                    chunk_id="d_research:1:c2",
                    doc_id="d_research",
                    parse_version=1,
                    node_id="node-d_research",
                    block_ids=["d_research:1:1:2"],
                    content="another bit of research content.",
                    content_type="text",
                    page_start=2,
                    page_end=2,
                    token_count=5,
                    path="/research/r.pdf",
                )
            )
            sess.commit()

        # Custom BM25 stub that returns both with different scores.
        class _Two:
            def __len__(self):
                return 2

            def search_chunks(self, query, top_k, *, allowed_doc_ids=None):
                return [
                    ("d_research:1:c1", 5.0),
                    ("d_research:1:c2", 9.0),
                ]

        state = _state(store)
        state._bm25 = _Two()

        llm = _StubLLM(
            [
                LLMResponse(text="", tool_calls=[_bm25_call()]),
                LLMResponse(text="ok", tool_calls=[]),
            ]
        )
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(state, _alice(seeded))
        result = loop.run("?", ctx)
        # Highest score first.
        assert result.citations[0]["chunk_id"] == "d_research:1:c2"
        assert result.citations[1]["chunk_id"] == "d_research:1:c1"
        # ``sources`` serialised as a list (JSON-safe).
        assert isinstance(result.citations[0]["sources"], list)

    def test_history_prepended(self, store, seeded):
        history = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        llm = _StubLLM([LLMResponse(text="ok", tool_calls=[])])
        loop = AgentLoop(AgentConfig(), llm)
        ctx = build_tool_context(_state(store), _alice(seeded))
        loop.run("now what?", ctx, history=history)
        sent = llm.calls[0].messages
        # System + 2 history + 1 new user.
        assert sent[0]["role"] == "system"
        assert sent[1]["content"] == "earlier question"
        assert sent[2]["content"] == "earlier answer"
        assert sent[3] == {"role": "user", "content": "now what?"}


# ---------------------------------------------------------------------------
# LLM failure path
# ---------------------------------------------------------------------------


class TestLLMError:
    def test_initial_call_failure_returns_error_result(self, store, seeded):
        """litellm raising should NOT crash the request — agent
        returns an empty-answer result with stop_reason='error'."""

        class _Boom:
            def chat(self, *a, **kw):
                raise RuntimeError("provider down")

        loop = AgentLoop(AgentConfig(), _Boom())
        ctx = build_tool_context(_state(store), _alice(seeded))
        result = loop.run("?", ctx)
        assert result.stop_reason == "error"
        assert result.answer == ""
