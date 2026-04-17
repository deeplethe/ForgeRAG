"""Unit tests for the reranker backends + probe() + strict/passthrough."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config.retrieval import RerankConfig
from retrieval.rerank import (
    LlmAsReranker,
    PassthroughReranker,
    RerankApiReranker,
    RerankerError,
    _extract_results,
    _parse_order,
    _result_index,
    make_reranker,
)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_disabled_returns_passthrough():
    assert isinstance(make_reranker(RerankConfig(enabled=False)), PassthroughReranker)


def test_factory_explicit_passthrough():
    assert isinstance(
        make_reranker(RerankConfig(enabled=True, backend="passthrough")),
        PassthroughReranker,
    )


def test_factory_rerank_api():
    r = make_reranker(RerankConfig(enabled=True, backend="rerank_api"))
    assert isinstance(r, RerankApiReranker)


def test_factory_llm_as_reranker():
    r = make_reranker(RerankConfig(enabled=True, backend="llm_as_reranker"))
    assert isinstance(r, LlmAsReranker)


def test_factory_rejects_legacy_litellm_value():
    """The old 'litellm' backend name was renamed to 'llm_as_reranker'.
    Pydantic must reject the old string value so users can't keep using it."""
    with pytest.raises(Exception):  # pydantic ValidationError
        RerankConfig(enabled=True, backend="litellm")


# ---------------------------------------------------------------------------
# Strict mode vs passthrough on_failure
# ---------------------------------------------------------------------------


def test_default_on_failure_is_strict():
    cfg = RerankConfig()
    assert cfg.on_failure == "strict"


# ---------------------------------------------------------------------------
# Passthrough behavior
# ---------------------------------------------------------------------------


def _mk_candidate(content: str, score: float = 0.5):
    """Build a MergedChunk-like object without importing real types."""
    chunk = MagicMock()
    chunk.content = content
    chunk.section_path = []
    chunk.content_type = "text"
    chunk.page_start = 1
    return MagicMock(chunk=chunk, score=score)


def test_passthrough_rerank_returns_top_k():
    r = PassthroughReranker()
    cands = [_mk_candidate(f"doc{i}") for i in range(5)]
    out = r.rerank("q", cands, top_k=3)
    assert out == cands[:3]


def test_passthrough_probe_noop():
    PassthroughReranker().probe()  # should not raise


def test_passthrough_empty_candidates():
    r = PassthroughReranker()
    out = r.rerank("q", [], top_k=3)
    assert out == []


# ---------------------------------------------------------------------------
# Helper parsing
# ---------------------------------------------------------------------------


def test_parse_order_simple():
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="Here you go: [3, 1, 2]"))]
    assert _parse_order(resp) == [3, 1, 2]


def test_parse_order_missing_array():
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="I cannot help"))]
    assert _parse_order(resp) == []


def test_parse_order_invalid_content():
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=None))]
    assert _parse_order(resp) == []


def test_extract_results_attribute_access():
    resp = MagicMock()
    resp.results = [{"index": 0}, {"index": 1}]
    assert _extract_results(resp) == [{"index": 0}, {"index": 1}]


def test_extract_results_dict_response():
    resp = {"results": [{"index": 2}]}
    assert _extract_results(resp) == [{"index": 2}]


def test_extract_results_empty():
    assert _extract_results(None) == []
    assert _extract_results({}) == []


def test_result_index_dict():
    assert _result_index({"index": 3}) == 3


def test_result_index_missing():
    assert _result_index({"no_index": 1}) is None


# ---------------------------------------------------------------------------
# RerankApiReranker: strict-mode raising + passthrough fallback
# ---------------------------------------------------------------------------


def test_rerank_api_strict_raises_on_failure(monkeypatch):
    """When on_failure='strict' and the API call errors, rerank() must raise."""
    cfg = RerankConfig(
        enabled=True,
        backend="rerank_api",
        model="cohere/rerank-v3.5",
        api_key="dummy",
        on_failure="strict",
    )
    r = RerankApiReranker(cfg)
    # Force _ensure() to return a mock whose rerank() raises
    fake_litellm = MagicMock()
    fake_litellm.rerank.side_effect = RuntimeError("boom")
    r._litellm = fake_litellm
    r._api_key = "dummy"

    with pytest.raises(RerankerError):
        r.rerank("q", [_mk_candidate("a"), _mk_candidate("b")], top_k=2)


def test_rerank_api_passthrough_on_failure_returns_candidates(monkeypatch):
    """When on_failure='passthrough', rerank() must NOT raise; returns top_k RRF order."""
    cfg = RerankConfig(
        enabled=True,
        backend="rerank_api",
        model="cohere/rerank-v3.5",
        api_key="dummy",
        on_failure="passthrough",
    )
    r = RerankApiReranker(cfg)
    fake_litellm = MagicMock()
    fake_litellm.rerank.side_effect = RuntimeError("boom")
    r._litellm = fake_litellm
    r._api_key = "dummy"

    cands = [_mk_candidate("a"), _mk_candidate("b"), _mk_candidate("c")]
    out = r.rerank("q", cands, top_k=2)
    # Pass-through — first two candidates in original order
    assert len(out) == 2
    assert out[0] is cands[0]
    assert out[1] is cands[1]


def test_rerank_api_happy_path_reorders(monkeypatch):
    """When the API returns reordered indices, rerank() must apply them."""
    cfg = RerankConfig(enabled=True, backend="rerank_api", model="cohere/rerank-v3.5", api_key="x")
    r = RerankApiReranker(cfg)
    fake_litellm = MagicMock()
    resp = MagicMock()
    resp.results = [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5}]
    fake_litellm.rerank.return_value = resp
    r._litellm = fake_litellm
    r._api_key = "x"

    cands = [_mk_candidate("first"), _mk_candidate("second"), _mk_candidate("third")]
    out = r.rerank("q", cands, top_k=2)
    assert out[0] is cands[2]   # rank 0 in response
    assert out[1] is cands[0]


def test_rerank_api_probe_raises_on_bad_endpoint():
    """probe() must surface the API error (not silent-fallback)."""
    cfg = RerankConfig(enabled=True, backend="rerank_api", model="cohere/rerank-v3.5", api_key="x")
    r = RerankApiReranker(cfg)
    fake_litellm = MagicMock()
    fake_litellm.rerank.side_effect = RuntimeError("401 Unauthorized")
    r._litellm = fake_litellm
    r._api_key = "x"

    with pytest.raises(RerankerError):
        r.probe()
