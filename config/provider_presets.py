"""
Curated LLM / embedder / reranker provider presets.

Exposed via GET /api/v1/llm-providers/presets so the Web UI "Add
Provider" flow can offer one-click templates. Each preset captures
the fiddly bits (correct LiteLLM model prefix, api_base, schema
caveats) that users would otherwise have to discover by trial and
error — the reason we're adding this at all is that someone today
spent 40 minutes debugging the "siliconflow/" vs "jina_ai/" prefix
mismatch for SiliconFlow's BGE rerank endpoint.

A preset fills in everything except the API key. The UI marks each
preset with a "recommended" / "self-hosted" / "free-tier" / etc.
badge and shows a human-readable note explaining non-obvious
choices (e.g. "Use jina_ai/ prefix because SiliconFlow speaks the
Cohere-compat rerank schema that Jina's adapter emits").
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Preset records
# ---------------------------------------------------------------------------

# Keys:
#   id              — stable identifier for this preset
#   label           — short display name in the UI dropdown
#   provider_type   — chat | embedding | reranker | vlm
#   model_name      — value for LLMProvider.model_name (LiteLLM-resolvable)
#   api_base        — value for LLMProvider.api_base ("" = LiteLLM default)
#   note            — user-facing description shown under the dropdown
#   requires_api_key— if true, UI prompts for it before Save
#   badge           — optional: "recommended" | "free-tier" | "self-hosted" | None


PROVIDER_PRESETS: list[dict] = [
    # ── Reranker ───────────────────────────────────────────────────────
    {
        "id": "siliconflow_bge_reranker_v2_m3",
        "label": "SiliconFlow · BGE Reranker v2 m3",
        "provider_type": "reranker",
        "model_name": "jina_ai/BAAI/bge-reranker-v2-m3",
        "api_base": "https://api.siliconflow.cn/v1",
        "note": (
            "Cross-encoder reranker hosted by SiliconFlow. IMPORTANT: use "
            "the jina_ai/ prefix — SiliconFlow speaks Cohere-compat rerank "
            "schema that LiteLLM's Jina adapter emits. The huggingface/ "
            "prefix will NOT work (it sends TEI schema which SiliconFlow "
            "rejects)."
        ),
        "requires_api_key": True,
        "badge": "recommended",
    },
    {
        "id": "cohere_rerank_v35",
        "label": "Cohere · Rerank v3.5",
        "provider_type": "reranker",
        "model_name": "cohere/rerank-v3.5",
        "api_base": "",
        "note": "Cohere's flagship multilingual reranker. Top quality, paid API.",
        "requires_api_key": True,
        "badge": None,
    },
    {
        "id": "jina_reranker_v2",
        "label": "Jina · Reranker v2 (multilingual)",
        "provider_type": "reranker",
        "model_name": "jina_ai/jina-reranker-v2-base-multilingual",
        "api_base": "",
        "note": "Jina's native reranker. Good multilingual coverage, free tier available.",
        "requires_api_key": True,
        "badge": "free-tier",
    },
    {
        "id": "voyage_rerank_2",
        "label": "Voyage · Rerank 2",
        "provider_type": "reranker",
        "model_name": "voyage/rerank-2",
        "api_base": "",
        "note": "Voyage AI's high-quality reranker.",
        "requires_api_key": True,
        "badge": None,
    },
    {
        "id": "local_tei_bge",
        "label": "Local TEI · BGE Reranker v2 m3",
        "provider_type": "reranker",
        "model_name": "huggingface/BAAI/bge-reranker-v2-m3",
        "api_base": "http://localhost:8080",
        "note": (
            "Self-hosted via HuggingFace Text-Embeddings-Inference (TEI). "
            "Run: docker run -p 8080:80 ghcr.io/huggingface/text-embeddings-inference:cpu-latest "
            "--model-id BAAI/bge-reranker-v2-m3 --revision main. Zero per-query cost."
        ),
        "requires_api_key": False,
        "badge": "self-hosted",
    },

    # ── Chat (answer generator / tree navigator / KG extractor) ────────
    {
        "id": "dashscope_qwen3_max",
        "label": "DashScope · Qwen3-Max",
        "provider_type": "chat",
        "model_name": "openai/qwen3-max",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "note": "Alibaba's flagship Qwen 3 model via DashScope compat endpoint. Strong Chinese + English.",
        "requires_api_key": True,
        "badge": "recommended",
    },
    {
        "id": "dashscope_qwen35_flash",
        "label": "DashScope · Qwen3.5-Flash",
        "provider_type": "chat",
        "model_name": "openai/qwen3.5-flash",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "note": "Fast / cheap Qwen variant, ideal for query understanding and tree nav.",
        "requires_api_key": True,
        "badge": None,
    },
    {
        "id": "openai_gpt4o",
        "label": "OpenAI · GPT-4o",
        "provider_type": "chat",
        "model_name": "openai/gpt-4o",
        "api_base": "",
        "note": "OpenAI's flagship GPT-4o. High quality, low latency.",
        "requires_api_key": True,
        "badge": "recommended",
    },
    {
        "id": "openai_gpt4o_mini",
        "label": "OpenAI · GPT-4o Mini",
        "provider_type": "chat",
        "model_name": "openai/gpt-4o-mini",
        "api_base": "",
        "note": "Cheap and fast — good default for query understanding / tree nav.",
        "requires_api_key": True,
        "badge": None,
    },
    {
        "id": "anthropic_claude_sonnet",
        "label": "Anthropic · Claude 3.5 Sonnet",
        "provider_type": "chat",
        "model_name": "anthropic/claude-3-5-sonnet-20241022",
        "api_base": "",
        "note": "Claude 3.5 Sonnet. Strong long-context and reasoning.",
        "requires_api_key": True,
        "badge": "recommended",
    },
    {
        "id": "anthropic_claude_haiku",
        "label": "Anthropic · Claude 3.5 Haiku",
        "provider_type": "chat",
        "model_name": "anthropic/claude-3-5-haiku-20241022",
        "api_base": "",
        "note": "Cheaper / faster Claude for auxiliary calls.",
        "requires_api_key": True,
        "badge": None,
    },

    # ── Embedding ──────────────────────────────────────────────────────
    {
        "id": "dashscope_text_embedding_v4",
        "label": "DashScope · text-embedding-v4",
        "provider_type": "embedding",
        "model_name": "openai/text-embedding-v4",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "note": "Alibaba embedding model via DashScope. 1024 or 2048 dims (check model card). Strong for Chinese.",
        "requires_api_key": True,
        "badge": "recommended",
    },
    {
        "id": "openai_text_embedding_3_large",
        "label": "OpenAI · text-embedding-3-large",
        "provider_type": "embedding",
        "model_name": "openai/text-embedding-3-large",
        "api_base": "",
        "note": "OpenAI's high-quality embedding, 3072 dims.",
        "requires_api_key": True,
        "badge": None,
    },
    {
        "id": "openai_text_embedding_3_small",
        "label": "OpenAI · text-embedding-3-small",
        "provider_type": "embedding",
        "model_name": "openai/text-embedding-3-small",
        "api_base": "",
        "note": "Cheap OpenAI embedding, 1536 dims.",
        "requires_api_key": True,
        "badge": None,
    },
    {
        "id": "siliconflow_bge_m3_embed",
        "label": "SiliconFlow · BGE-M3 Embedding",
        "provider_type": "embedding",
        "model_name": "openai/BAAI/bge-m3",
        "api_base": "https://api.siliconflow.cn/v1",
        "note": "Multilingual BGE-M3 embedding via SiliconFlow. 1024 dims. Free tier available.",
        "requires_api_key": True,
        "badge": "free-tier",
    },

    # ── VLM ────────────────────────────────────────────────────────────
    {
        "id": "dashscope_qwen3_vl_flash",
        "label": "DashScope · Qwen3-VL-Flash",
        "provider_type": "vlm",
        "model_name": "openai/qwen3-vl-flash",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "note": "Fast visual-language model for image enrichment / scanned PDFs.",
        "requires_api_key": True,
        "badge": "recommended",
    },
    {
        "id": "openai_gpt4o_vlm",
        "label": "OpenAI · GPT-4o (vision)",
        "provider_type": "vlm",
        "model_name": "openai/gpt-4o",
        "api_base": "",
        "note": "GPT-4o for visual understanding. Same provider as the chat preset.",
        "requires_api_key": True,
        "badge": None,
    },
]


def presets_for_type(provider_type: str) -> list[dict]:
    """Filter presets by provider_type (chat / embedding / reranker / vlm)."""
    return [p for p in PROVIDER_PRESETS if p["provider_type"] == provider_type]
