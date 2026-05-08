"""
Setup wizard — one-key model-platform presets.

Each preset describes a "configure once, get chat + embedder + reranker"
deployment shape. The wizard renders these as tiles; on selection it
asks for the platform's API key (or a couple of small extras like an
endpoint URL for self-hosted setups), then writes a complete config
overlay so the operator never has to edit yaml by hand.

Adding a preset:
  * Add an entry below.
  * ``id`` is the stable internal slug (used in the wizard URL +
    audit logs).
  * ``inputs`` lists the freeform fields the wizard collects from
    the user. ``api_key`` is the conventional name for the platform
    key; ``api_base`` for the URL when applicable.
  * ``config`` is a flat dict of dotted-path → value templates.
    Templates can reference ``{api_key}``, ``{api_base}``, etc. by
    name; the wizard substitutes user input at commit time.
"""

from __future__ import annotations


# Preset list. Order = wizard tile display order (top-down). The
# first match-fit for a region should appear first; "Custom" is
# always last (advanced fallback).
PRESETS: list[dict] = [
    # ── SiliconFlow (国内首选) ─────────────────────────────────
    # Single API key covers Qwen / DeepSeek-V3 chat + BGE-M3
    # embedder + BGE-reranker. Pricing is the cheapest in the
    # Chinese market for the quality tier; the free embed tier
    # alone makes this a no-brainer for cost-sensitive deploys.
    {
        "id": "siliconflow",
        "name": "SiliconFlow",
        "tagline": "国内统一模型平台 · 一个 key 拿到 chat + embed + rerank",
        "tagline_en": "All-in-one Chinese model platform: chat + embedding + reranker via one API key",
        "logo_emoji": "🌟",
        "recommended_for": ["china", "cost_sensitive"],
        "inputs": [
            {
                "name": "api_key",
                "label": "SiliconFlow API Key",
                "placeholder": "sk-...",
                "secret": True,
                "help": "https://cloud.siliconflow.cn → API Keys",
            },
        ],
        "config": {
            "answering.generator.backend": "litellm",
            "answering.generator.model": "deepseek-ai/DeepSeek-V3",
            "answering.generator.api_base": "https://api.siliconflow.cn/v1",
            "answering.generator.api_key": "{api_key}",
            "embedder.backend": "litellm",
            "embedder.dimension": 1024,
            "embedder.litellm.model": "openai/BAAI/bge-m3",
            "embedder.litellm.api_base": "https://api.siliconflow.cn/v1",
            "embedder.litellm.api_key": "{api_key}",
            "retrieval.rerank.enabled": True,
            "retrieval.rerank.backend": "rerank_api",
            "retrieval.rerank.model": "BAAI/bge-reranker-v2-m3",
            "retrieval.rerank.api_base": "https://api.siliconflow.cn/v1",
            "retrieval.rerank.api_key": "{api_key}",
        },
        "test": {
            # The wizard pings these endpoints with a tiny payload
            # to confirm the key works. Each field is a dotted path
            # into the preset's ``config`` whose api_key/base it should
            # use. ``model`` is the model to test against (we test
            # only the chat path; embedder + rerank reuse the same key).
            "chat": {
                "model": "deepseek-ai/DeepSeek-V3",
                "api_base": "https://api.siliconflow.cn/v1",
                "api_key_input": "api_key",
            },
        },
    },

    # ── OpenAI (国际首选) ─────────────────────────────────────
    {
        "id": "openai",
        "name": "OpenAI",
        "tagline": "Global default · gpt-4o-mini + text-embedding-3-small",
        "tagline_en": "Global default: gpt-4o-mini + text-embedding-3-small",
        "logo_emoji": "🌍",
        "recommended_for": ["global", "production"],
        "inputs": [
            {
                "name": "api_key",
                "label": "OpenAI API Key",
                "placeholder": "sk-...",
                "secret": True,
                "help": "https://platform.openai.com/api-keys",
            },
        ],
        "config": {
            "answering.generator.backend": "litellm",
            "answering.generator.model": "gpt-4o-mini",
            "answering.generator.api_key": "{api_key}",
            "embedder.backend": "litellm",
            "embedder.dimension": 1024,
            "embedder.litellm.model": "openai/text-embedding-3-small",
            "embedder.litellm.requested_dimensions": 1024,
            "embedder.litellm.api_key": "{api_key}",
            # OpenAI doesn't sell a hosted reranker — keep rerank
            # off in this preset; admins can switch to a passthrough
            # ranker or wire Cohere / Jina later.
            "retrieval.rerank.enabled": False,
        },
        "test": {
            "chat": {
                "model": "gpt-4o-mini",
                "api_base": None,         # default OpenAI base
                "api_key_input": "api_key",
            },
        },
    },

    # ── DeepSeek 官方 (chat-only; pair with a separate embedder) ──
    # DeepSeek's official API is chat-only — no embedder, no
    # reranker. We default the embedder to a free-tier alternative
    # the user can reach with the same providers' SDK; if they
    # want a single key, SiliconFlow above is the better pick.
    {
        "id": "deepseek_official",
        "name": "DeepSeek 官方",
        "tagline": "DeepSeek-V3 chat · 配 BGE-M3 embedder（需另一个 key）",
        "tagline_en": "DeepSeek-V3 chat — pair with a separate embedder",
        "logo_emoji": "🔵",
        "recommended_for": ["china"],
        "inputs": [
            {
                "name": "api_key",
                "label": "DeepSeek API Key",
                "placeholder": "sk-...",
                "secret": True,
                "help": "https://platform.deepseek.com/api_keys",
            },
            {
                "name": "embedder_api_key",
                "label": "Embedder API Key (SiliconFlow recommended for free BGE-M3)",
                "placeholder": "sk-...",
                "secret": True,
                "help": "Use a SiliconFlow key for free BGE-M3 access",
            },
        ],
        "config": {
            "answering.generator.backend": "litellm",
            "answering.generator.model": "deepseek/deepseek-chat",
            "answering.generator.api_key": "{api_key}",
            "embedder.backend": "litellm",
            "embedder.dimension": 1024,
            "embedder.litellm.model": "openai/BAAI/bge-m3",
            "embedder.litellm.api_base": "https://api.siliconflow.cn/v1",
            "embedder.litellm.api_key": "{embedder_api_key}",
            "retrieval.rerank.enabled": False,
        },
        "test": {
            "chat": {
                "model": "deepseek/deepseek-chat",
                "api_base": None,
                "api_key_input": "api_key",
            },
        },
    },

    # ── Anthropic (Claude family) ─────────────────────────────
    {
        "id": "anthropic",
        "name": "Anthropic",
        "tagline": "Claude family · pair with a separate embedder",
        "tagline_en": "Claude family — pair with a separate embedder",
        "logo_emoji": "🧠",
        "recommended_for": ["global"],
        "inputs": [
            {
                "name": "api_key",
                "label": "Anthropic API Key",
                "placeholder": "sk-ant-...",
                "secret": True,
                "help": "https://console.anthropic.com/settings/keys",
            },
            {
                "name": "embedder_api_key",
                "label": "Embedder API Key (OpenAI / SiliconFlow / Voyage)",
                "secret": True,
                "help": "Anthropic doesn't sell embeddings — use any other provider",
            },
        ],
        "config": {
            "answering.generator.backend": "litellm",
            "answering.generator.model": "anthropic/claude-3-5-sonnet-latest",
            "answering.generator.api_key": "{api_key}",
            "embedder.backend": "litellm",
            "embedder.dimension": 1024,
            "embedder.litellm.model": "openai/text-embedding-3-small",
            "embedder.litellm.requested_dimensions": 1024,
            "embedder.litellm.api_key": "{embedder_api_key}",
            "retrieval.rerank.enabled": False,
        },
        "test": {
            "chat": {
                "model": "anthropic/claude-3-5-haiku-latest",   # cheap test model
                "api_base": None,
                "api_key_input": "api_key",
            },
        },
    },

    # ── Ollama (full self-host, zero data exits) ──────────────
    {
        "id": "ollama",
        "name": "Ollama (Self-hosted)",
        "tagline": "完全本地 · 数据零外发；需要先跑 Ollama 服务",
        "tagline_en": "Fully local — zero data leaves your network. Ollama service required.",
        "logo_emoji": "🏠",
        "recommended_for": ["high_compliance", "air_gapped"],
        "inputs": [
            {
                "name": "api_base",
                "label": "Ollama URL",
                "placeholder": "http://localhost:11434",
                "default": "http://localhost:11434",
                "help": "URL of your Ollama server. ``host.docker.internal`` if Ollama runs on the same host as docker.",
            },
        ],
        "config": {
            "answering.generator.backend": "litellm",
            "answering.generator.model": "ollama_chat/qwen2.5:7b",
            "answering.generator.api_base": "{api_base}",
            "embedder.backend": "litellm",
            "embedder.dimension": 1024,
            "embedder.litellm.model": "ollama/bge-m3",
            "embedder.litellm.api_base": "{api_base}",
            "retrieval.rerank.enabled": False,
        },
        "test": {
            "chat": {
                "model": "ollama_chat/qwen2.5:7b",
                "api_base_input": "api_base",
                "api_key_input": None,
            },
        },
    },

    # ── Custom (advanced, fall through to the previous Settings UI) ─
    {
        "id": "custom",
        "name": "Custom",
        "tagline": "Configure each provider separately (advanced)",
        "tagline_en": "Configure each provider separately",
        "logo_emoji": "⚙️",
        "recommended_for": [],
        "inputs": [],
        "config": {},
        "skip_test": True,
    },
]


def get_preset(preset_id: str) -> dict | None:
    """Look up a preset by id. None when unknown."""
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    return None


def render_preset_config(preset: dict, inputs: dict[str, str]) -> dict[str, object]:
    """Substitute ``{name}`` placeholders in the preset's config
    template with the user's inputs. Returns a flat dotted-path
    dict ready to be deep-merged into the runtime config."""
    out: dict[str, object] = {}
    for path, value in preset.get("config", {}).items():
        if isinstance(value, str) and "{" in value and "}" in value:
            try:
                out[path] = value.format(**inputs)
            except KeyError:
                # Missing input → drop the key. Better than substituting
                # an empty string which would silently misconfigure
                # ``api_key`` etc.
                continue
        else:
            out[path] = value
    return out
