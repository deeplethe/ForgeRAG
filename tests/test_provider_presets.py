"""Smoke tests for the provider presets catalogue."""

from __future__ import annotations

from config.provider_presets import PROVIDER_PRESETS, presets_for_type

REQUIRED_KEYS = {"id", "label", "provider_type", "model_name", "api_base", "note", "requires_api_key"}
VALID_TYPES = {"chat", "embedding", "reranker", "vlm"}


def test_all_presets_have_required_keys():
    for p in PROVIDER_PRESETS:
        missing = REQUIRED_KEYS - set(p.keys())
        assert not missing, f"preset {p.get('id')!r} missing keys: {missing}"


def test_all_preset_ids_unique():
    ids = [p["id"] for p in PROVIDER_PRESETS]
    assert len(ids) == len(set(ids)), f"duplicate preset ids: {ids}"


def test_all_preset_types_valid():
    for p in PROVIDER_PRESETS:
        assert p["provider_type"] in VALID_TYPES, f"{p['id']} has bad provider_type"


def test_each_type_has_at_least_one_preset():
    for t in VALID_TYPES:
        got = presets_for_type(t)
        assert len(got) >= 1, f"no presets for {t!r}"


def test_siliconflow_bge_uses_jina_ai_prefix():
    """Regression guard: the whole point of this preset is that SiliconFlow's
    rerank endpoint speaks Cohere-compat schema and LiteLLM's jina_ai/ adapter
    emits that schema. Using huggingface/ prefix hits TEI schema and fails.
    This test will catch anyone "fixing" the prefix back to huggingface/."""
    p = next(x for x in PROVIDER_PRESETS if x["id"] == "siliconflow_bge_reranker_v2_m3")
    assert p["model_name"].startswith("jina_ai/"), (
        f"SiliconFlow BGE preset must use jina_ai/ prefix for schema compat, got: {p['model_name']}"
    )
    assert "siliconflow.cn" in p["api_base"]


def test_filter_by_type():
    rerankers = presets_for_type("reranker")
    assert all(p["provider_type"] == "reranker" for p in rerankers)
    embeddings = presets_for_type("embedding")
    assert all(p["provider_type"] == "embedding" for p in embeddings)


def test_presets_mention_common_providers():
    """The catalogue should cover the majors so new users don't hunt for prefixes."""
    ids = {p["id"] for p in PROVIDER_PRESETS}
    # Reranker: at least one SiliconFlow + Cohere + Jina
    assert any("siliconflow" in i for i in ids)
    assert any("cohere" in i for i in ids)
    assert any("jina" in i for i in ids)
    # Chat: at least one OpenAI + Anthropic + DashScope
    assert any("openai" in i for i in ids)
    assert any("anthropic" in i for i in ids)
    assert any("dashscope" in i for i in ids)
