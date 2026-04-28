"""
Answer generation configuration.

Sits downstream of retrieval. The answering layer takes a
RetrievalResult, builds a grounded prompt, calls an LLM via
litellm (the same unified backend the reranker uses), and
returns an Answer with the subset of citations the LLM actually
referenced.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class GeneratorConfig(BaseModel):
    backend: Literal["litellm"] = "litellm"
    model: str = "openai/gpt-4o-mini"
    temperature: float = 0.1
    # ``None`` (the default) means "don't pass max_tokens to the
    # provider" — the model's own maximum output length applies. This
    # matters for thinking-mode models (V4-Pro / o1 / DeepSeek-R1)
    # where the reasoning trace itself counts toward the cap; a hard
    # 2048 ceiling caused visible answers to truncate mid-sentence
    # while the model burned most of the budget on reasoning. Set an
    # explicit positive int to enforce a hard cap.
    max_tokens: int | None = None
    timeout: float = 60.0

    # ── Provider-specific reasoning / thinking-mode controls ──────
    # Passed through verbatim to ``litellm.completion`` as kwargs.
    # ``extra_body`` (when present) is deep-merged with any existing
    # value rather than replaced. Examples per provider:
    #
    #   # DeepSeek V4-Pro (hybrid) — TURN OFF thinking for fast path:
    #   reasoning:
    #     extra_body:
    #       thinking:
    #         type: disabled
    #
    #   # DeepSeek V4-Pro — explicit thinking ON with high effort:
    #   reasoning:
    #     reasoning_effort: high
    #     extra_body:
    #       thinking:
    #         type: enabled
    #
    #   # Anthropic Claude (3.7+ / 4) — extended thinking with budget:
    #   reasoning:
    #     thinking:
    #       type: enabled
    #       budget_tokens: 16000
    #
    #   # OpenAI o-series / xAI Grok — three-step effort dial:
    #   reasoning:
    #     reasoning_effort: high
    #
    #   # Gemini 2.5 — explicit thinking budget + expose thoughts:
    #   reasoning:
    #     thinking_config:
    #       thinking_budget: 8000
    #       include_thoughts: true
    #
    # Default ``{}`` = forward nothing → each provider's own default
    # behaviour applies. Note: DeepSeek-Reasoner (R1) ignores the
    # toggle and always thinks; switch to ``deepseek-v4-pro`` (hybrid)
    # or ``deepseek-chat`` (non-thinking) if you need control.
    reasoning: dict[str, Any] = Field(default_factory=dict)
    # Authentication: use ONE of api_key (plaintext in yaml) or
    # api_key_env (name of an env var). api_key takes precedence.
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None

    # Per-chunk char budget in the context block
    chunk_chars: int = 1500
    # Hard ceiling on total context characters; chunks get dropped
    # from the tail until the budget is satisfied
    max_context_chars: int = 20000

    # System prompt override. None -> use the default in prompts.py
    system_prompt: str | None = None
    # User message template. None -> use the default in prompts.py
    user_prompt_template: str | None = None

    # If True, instruct the model to refuse when context is thin
    refuse_when_unknown: bool = True
    refuse_message: str = "I don't know based on the provided documents."


class CORSConfig(BaseModel):
    allow_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Allowed origins. ['*'] for dev; restrict in production.",
    )
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])
    allow_credentials: bool = True


class AnsweringSection(BaseModel):
    generator: GeneratorConfig = Field(default_factory=GeneratorConfig)

    # Cap on how many merged-retrieval chunks are forwarded to the LLM.
    # Default 8 pairs well with rerank top_k=10: the generator sees only
    # the highest-ranked chunks so faithfulness stays high without padding
    # the context with marginal matches.
    max_chunks: int = 8

    # Whether to keep expansion (sibling/crossref) chunks in the prompt.
    # Disabling makes the context tighter and cheaper; enabling helps
    # the model pick up cross-page context.
    include_expanded_chunks: bool = True
