"""Unit tests for ``graph.summarize`` — pure logic only.

LLM round-trips are mocked. The tests cover:
  * threshold gating in ``needs_summary`` (token + count)
  * fragment splitting / round-trip via ``split_fragments``
  * map-reduce chunking via ``_chunk_by_token``
  * end-to-end ``summarize_descriptions`` with a stub LLM

Live-LLM behaviour (real provider calls, retries on rate-limit) is
out of scope — covered by manual / integration testing in the ingest
pipeline tests.
"""

from __future__ import annotations

from unittest.mock import patch

from graph.summarize import (
    SummarizeConfig,
    _chunk_by_token,
    needs_summary,
    split_fragments,
    summarize_descriptions,
)

# ---------------------------------------------------------------------------
# Threshold gating
# ---------------------------------------------------------------------------


def test_needs_summary_disabled():
    cfg = SummarizeConfig(enabled=False, trigger_tokens=10, force_on_count=2)
    assert needs_summary(["a" * 1000] * 50, cfg) is False


def test_needs_summary_empty_input():
    cfg = SummarizeConfig()
    assert needs_summary([], cfg) is False


def test_needs_summary_count_threshold():
    cfg = SummarizeConfig(trigger_tokens=99999, force_on_count=3)
    # 2 fragments < 3 → not triggered
    assert needs_summary(["a", "b"], cfg) is False
    # 3 fragments == threshold → triggered
    assert needs_summary(["a", "b", "c"], cfg) is True


def test_needs_summary_token_threshold():
    # ~4 chars per token → 5000 chars ≈ 1250 tokens → above 1200 trigger
    cfg = SummarizeConfig(trigger_tokens=1200, force_on_count=99)
    fragments = ["x" * 5000]
    assert needs_summary(fragments, cfg) is True
    # Smaller fragment: ~250 chars → ~62 tokens, well below
    assert needs_summary(["x" * 200], cfg) is False


# ---------------------------------------------------------------------------
# split_fragments
# ---------------------------------------------------------------------------


def test_split_fragments_basic():
    desc = "First fragment.\nSecond fragment.\nThird."
    assert split_fragments(desc) == ["First fragment.", "Second fragment.", "Third."]


def test_split_fragments_empty_lines():
    desc = "First.\n\n  \nSecond.\n"
    assert split_fragments(desc) == ["First.", "Second."]


def test_split_fragments_empty_input():
    assert split_fragments("") == []
    assert split_fragments(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _chunk_by_token
# ---------------------------------------------------------------------------


def test_chunk_by_token_packs_under_limit():
    # Each fragment ~25 tokens (100 chars / 4); limit 100 → ~4 per chunk
    frags = ["x" * 100] * 8
    chunks = _chunk_by_token(frags, limit=100)
    assert len(chunks) >= 2
    # No chunk should exceed the limit (greedy packing)
    for c in chunks:
        # Token count of a chunk should fit (with the +1 minimum for empty)
        total = sum(max(1, int(len(f) * 0.25)) for f in c)
        assert total <= 100 + 25  # allow one fragment of slop


def test_chunk_by_token_merges_trailing_singleton():
    # 5 frags packing into chunks where the last would be alone
    # → it gets merged back
    frags = ["x" * 100] * 5
    chunks = _chunk_by_token(frags, limit=50)
    # Expect no chunk of size 1 except possibly when there's only 1 chunk total
    if len(chunks) > 1:
        assert all(len(c) >= 2 for c in chunks), f"got {[len(c) for c in chunks]}"


def test_chunk_by_token_single_fragment():
    chunks = _chunk_by_token(["only one"], limit=1000)
    assert chunks == [["only one"]]


def test_chunk_by_token_empty():
    assert _chunk_by_token([], limit=1000) == []


# ---------------------------------------------------------------------------
# summarize_descriptions — end-to-end with stubbed LLM
# ---------------------------------------------------------------------------


def test_summarize_empty_returns_empty():
    cfg = SummarizeConfig()
    assert summarize_descriptions(name="X", kind="entity", fragments=[], cfg=cfg) == ""


def test_summarize_single_fragment_passthrough():
    cfg = SummarizeConfig()
    out = summarize_descriptions(
        name="X", kind="entity", fragments=["only one description"], cfg=cfg
    )
    assert out == "only one description"


def test_summarize_calls_llm_when_multiple_fragments():
    cfg = SummarizeConfig()
    fragments = ["First mention.", "Second mention.", "Third mention."]

    with patch("graph.summarize._call_llm", return_value="MERGED SUMMARY") as mock:
        out = summarize_descriptions(
            name="Sustainability", kind="entity", fragments=fragments, cfg=cfg
        )
        assert out == "MERGED SUMMARY"
        assert mock.call_count == 1
        # LLM was called with the system prompt + user prompt
        args, _kwargs = mock.call_args
        system, user, passed_cfg = args
        assert "Knowledge Graph Specialist" in system
        assert "Sustainability" in user
        assert "First mention" in user
        assert passed_cfg is cfg


def test_summarize_map_reduce_when_oversized():
    """When fragments don't fit context_size, we go map-reduce.

    Setup: 6 fragments of ~50 tokens each (≈ 300 tokens total) with a
    tiny context_size (100) so two chunks form. First iteration calls
    LLM twice (one per chunk); second iteration consumes the chunk
    summaries (now < context_size) with a single final LLM call.
    """
    cfg = SummarizeConfig(context_size=100, force_on_count=99)
    fragments = ["x" * 200] * 6  # ~50 tokens each

    call_counter = {"n": 0}

    def fake_llm(_system, _user, _cfg):
        call_counter["n"] += 1
        # Each chunk-summary returns a smaller string so subsequent
        # iterations converge.
        return f"summary{call_counter['n']}"

    with patch("graph.summarize._call_llm", side_effect=fake_llm):
        out = summarize_descriptions(
            name="X", kind="entity", fragments=fragments, cfg=cfg
        )

    # Should have made >1 calls (map-reduce) and converged to a final string
    assert call_counter["n"] >= 2
    assert out.startswith("summary")


def test_summarize_chunk_failure_falls_back_to_verbatim():
    """If a single chunk LLM call fails, we keep that chunk's text
    verbatim and let later iterations try again. Non-fatal."""
    cfg = SummarizeConfig(context_size=80, force_on_count=99)
    fragments = ["x" * 200] * 4

    call_n = {"n": 0}

    def flaky_llm(_system, _user, _cfg):
        call_n["n"] += 1
        if call_n["n"] == 1:
            raise RuntimeError("transient")
        return f"summary{call_n['n']}"

    with patch("graph.summarize._call_llm", side_effect=flaky_llm):
        out = summarize_descriptions(
            name="X", kind="entity", fragments=fragments, cfg=cfg
        )

    # Despite the first-chunk failure we should have a non-empty output
    assert out
    # And we should have made >1 call (the retry / next iteration)
    assert call_n["n"] >= 2


def test_summarize_prompts_pass_kind_and_language():
    cfg = SummarizeConfig(language="Write the entire output in Chinese")
    captured = {}

    def capture_llm(_system, user, _cfg):
        captured["user"] = user
        return "OK"

    with patch("graph.summarize._call_llm", side_effect=capture_llm):
        summarize_descriptions(
            name="测试实体",
            kind="relation",
            fragments=["a", "b", "c"],
            cfg=cfg,
        )

    user = captured["user"]
    assert "relation Name" in user
    assert "测试实体" in user
    assert "Chinese" in user
