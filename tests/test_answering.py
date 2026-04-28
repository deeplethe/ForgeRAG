"""
Tests for the answering layer.

Exercises:
    - citation marker parser
    - prompt builder (grouping, char budgets, citation keying)
    - pipeline end-to-end with a fake generator and fake retrieval
"""

from __future__ import annotations

from answering.pipeline import AnsweringPipeline
from answering.prompts import build_messages, extract_cited_ids
from answering.types import Answer
from config import AnsweringSection, GeneratorConfig
from parser.schema import Chunk, Citation, HighlightRect
from retrieval.types import MergedChunk, RetrievalResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunk(chunk_id, content, section=None, block_ids=None) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="d1",
        parse_version=1,
        node_id="n1",
        block_ids=block_ids or [f"b_{chunk_id}"],
        content=content,
        content_type="text",
        page_start=1,
        page_end=1,
        token_count=len(content) // 4,
        section_path=section or ["doc", "Intro"],
    )


def _citation(cid, chunk) -> Citation:
    return Citation(
        citation_id=cid,
        chunk_id=chunk.chunk_id,
        doc_id=chunk.doc_id,
        parse_version=chunk.parse_version,
        block_ids=list(chunk.block_ids),
        page_no=chunk.page_start,
        highlights=[HighlightRect(page_no=chunk.page_start, bbox=(0, 0, 10, 10))],
        snippet=chunk.content[:40],
        score=0.9,
        file_id="file_abc",
    )


def _merged(chunks) -> list[MergedChunk]:
    return [
        MergedChunk(
            chunk_id=c.chunk_id,
            rrf_score=1.0 / (i + 1),
            sources={"vector"},
            chunk=c,
        )
        for i, c in enumerate(chunks)
    ]


# ---------------------------------------------------------------------------
# extract_cited_ids
# ---------------------------------------------------------------------------


class TestExtractCitedIds:
    def test_basic(self):
        text = "The model achieves 98.7% accuracy [c_3] on FinanceBench [c_1]."
        assert extract_cited_ids(text) == ["c_3", "c_1"]

    def test_dedup(self):
        text = "Foo [c_2] bar [c_2] baz [c_2]."
        assert extract_cited_ids(text) == ["c_2"]

    def test_no_markers(self):
        assert extract_cited_ids("plain text") == []

    def test_empty(self):
        assert extract_cited_ids("") == []


# ---------------------------------------------------------------------------
# build_messages
# ---------------------------------------------------------------------------


class TestBuildMessages:
    def test_basic_prompt_structure(self):
        c1 = _chunk("d1:1:c1", "First fact about X.", section=["doc", "Intro"])
        c2 = _chunk("d1:1:c2", "Second fact about Y.", section=["doc", "Methods"])
        merged = _merged([c1, c2])
        cits = [_citation("c_1", c1), _citation("c_2", c2)]

        msgs, used = build_messages(
            query="What are the facts?",
            merged=merged,
            citations=cits,
            cfg=GeneratorConfig(),
            max_chunks=10,
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        user = msgs[1]["content"]
        assert "## Context" in user
        assert "doc > Intro" in user
        assert "doc > Methods" in user
        assert "[c_1]" in user
        assert "[c_2]" in user
        assert "What are the facts?" in user
        assert len(used) == 2

    def test_section_grouping(self):
        c1 = _chunk("d1:1:c1", "A", section=["doc", "Sec1"])
        c2 = _chunk("d1:1:c2", "B", section=["doc", "Sec1"])
        c3 = _chunk("d1:1:c3", "C", section=["doc", "Sec2"])
        merged = _merged([c1, c2, c3])
        cits = [_citation(f"c_{i + 1}", c) for i, c in enumerate([c1, c2, c3])]
        msgs, _used = build_messages(
            query="q",
            merged=merged,
            citations=cits,
            cfg=GeneratorConfig(),
        )
        user = msgs[1]["content"]
        # Section headers should each appear exactly once
        assert user.count("### doc > Sec1") == 1
        assert user.count("### doc > Sec2") == 1

    def test_max_chunks_cap(self):
        chunks = [_chunk(f"d1:1:c{i}", f"body {i}") for i in range(20)]
        merged = _merged(chunks)
        cits = [_citation(f"c_{i + 1}", c) for i, c in enumerate(chunks)]
        _, used = build_messages(
            query="q",
            merged=merged,
            citations=cits,
            cfg=GeneratorConfig(),
            max_chunks=5,
        )
        assert len(used) == 5

    def test_total_char_budget_drops_tail(self):
        big = "x" * 2000
        chunks = [_chunk(f"d1:1:c{i}", big) for i in range(10)]
        merged = _merged(chunks)
        cits = [_citation(f"c_{i + 1}", c) for i, c in enumerate(chunks)]
        cfg = GeneratorConfig(chunk_chars=2000, max_context_chars=5000)
        _, used = build_messages(
            query="q",
            merged=merged,
            citations=cits,
            cfg=cfg,
        )
        # 5000 / (2000+50) ~ 2 chunks
        assert 1 <= len(used) <= 3

    def test_excludes_expanded_when_disabled(self):
        c1 = _chunk("d1:1:c1", "base")
        c2 = _chunk("d1:1:c2", "sibling")
        merged = [
            MergedChunk(chunk_id=c1.chunk_id, rrf_score=0.9, sources={"vector"}, chunk=c1),
            MergedChunk(chunk_id=c2.chunk_id, rrf_score=0.3, sources={"expansion:sibling"}, chunk=c2),
        ]
        cits = [_citation("c_1", c1), _citation("c_2", c2)]
        _, used = build_messages(
            query="q",
            merged=merged,
            citations=cits,
            cfg=GeneratorConfig(),
            include_expanded_chunks=False,
        )
        assert len(used) == 1
        assert used[0].citation_id == "c_1"


# ---------------------------------------------------------------------------
# AnsweringPipeline with fakes
# ---------------------------------------------------------------------------


class FakeRetrievalPipeline:
    """Returns a fixed RetrievalResult ignoring the query."""

    def __init__(self, merged, citations):
        self._merged = merged
        self._citations = citations
        self.cfg = None

    def analyze_query(self, query, *, chat_history=None, strict=True):
        # AnsweringPipeline now always calls analyze_query (no cfg toggle).
        # Return None to signal "no QU result" — caller proceeds without one.
        return None

    def retrieve(self, query, *, filter=None, chat_history=None, precomputed_plan=None, overrides=None):
        return RetrievalResult(
            query=query,
            merged=self._merged,
            citations=self._citations,
            vector_hits=[],
            tree_hits=[],
            stats={"vector_hits": 1, "total_ms": 5},
        )


class FakeGenerator:
    backend = "fake"
    model = "fake/test"

    def __init__(self, answer_text, finish="stop"):
        self.answer_text = answer_text
        self.finish = finish
        self.last_messages = None

    def generate(self, messages, *, overrides=None):
        self.last_messages = messages
        self.last_overrides = overrides
        from answering.prompts import extract_cited_ids

        return {
            "text": self.answer_text,
            "finish_reason": self.finish,
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "model": self.model,
            "cited_ids": extract_cited_ids(self.answer_text),
            "latency_ms": 10,
        }


class TestAnsweringPipeline:
    def _env(self, answer_text="Answer [c_2]."):
        c1 = _chunk("d1:1:c1", "irrelevant content")
        c2 = _chunk("d1:1:c2", "the specific fact that matters")
        merged = _merged([c1, c2])
        cits = [_citation("c_1", c1), _citation("c_2", c2)]
        retrieval = FakeRetrievalPipeline(merged, cits)
        gen = FakeGenerator(answer_text)
        cfg = AnsweringSection()
        pipe = AnsweringPipeline(cfg, retrieval=retrieval, generator=gen)
        return pipe, gen

    def test_ask_returns_answer_with_used_citations(self):
        pipe, _gen = self._env("The answer is [c_2].")
        answer = pipe.ask("What matters?")
        assert isinstance(answer, Answer)
        assert answer.text == "The answer is [c_2]."
        assert [c.citation_id for c in answer.citations_used] == ["c_2"]
        assert len(answer.citations_all) == 2
        assert answer.finish_reason == "stop"
        assert answer.model == "fake/test"

    def test_messages_contain_query_and_context(self):
        pipe, gen = self._env("Answer [c_1].")
        pipe.ask("what matters?")
        user = gen.last_messages[1]["content"]
        assert "what matters?" in user
        assert "the specific fact that matters" in user

    def test_empty_context_returns_refusal(self):
        retrieval = FakeRetrievalPipeline(merged=[], citations=[])
        cfg = AnsweringSection()
        pipe = AnsweringPipeline(
            cfg,
            retrieval=retrieval,
            generator=FakeGenerator("unused"),
        )
        answer = pipe.ask("q")
        assert answer.citations_used == []
        assert answer.finish_reason == "no_context"
        assert answer.text == cfg.generator.refuse_message

    def test_unknown_citation_marker_ignored(self):
        pipe, _gen = self._env("Answer [c_99] which does not exist.")
        answer = pipe.ask("q")
        assert answer.citations_used == []  # c_99 not in the prompt set

    def test_stats_forwarded(self):
        pipe, _gen = self._env("A [c_1].")
        answer = pipe.ask("q")
        assert "retrieval" in answer.stats
        assert "generate_ms" in answer.stats
        assert "usage" in answer.stats
