"""
Benchmark configuration.

Keeps the LLM judge separated from the answer generator so that we
don't suffer self-preference bias (a model scoring its own answers).
If no separate judge provider is configured, we fall back to the
generator's provider — but the UI warns the user about the bias.
"""

from __future__ import annotations

from pydantic import BaseModel


class BenchmarkConfig(BaseModel):
    """Benchmark settings. Lives alongside answering / retrieval etc."""

    # LLM used to score generated answers. Set to a DIFFERENT provider
    # than answering.generator.provider_id to avoid self-preference bias.
    # When None, metrics.py falls back to answering.generator with a
    # warning in the trace.
    judge_provider_id: str | None = None

    # Resolved at startup from llm_providers (same pattern as other
    # provider-backed configs; see config.settings_manager.resolve_providers).
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None

    # Judge request timeout.
    timeout: float = 30.0
