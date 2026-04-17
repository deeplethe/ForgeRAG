"""
Answer generation configuration.

Sits downstream of retrieval. The answering layer takes a
RetrievalResult, builds a grounded prompt, calls an LLM via
litellm (the same unified backend the reranker uses), and
returns an Answer with the subset of citations the LLM actually
referenced.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GeneratorConfig(BaseModel):
    provider_id: str | None = None  # resolved at startup from llm_providers table
    backend: Literal["litellm"] = "litellm"
    model: str = "openai/gpt-4o-mini"
    temperature: float = 0.1
    max_tokens: int = 2048
    timeout: float = 60.0
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
