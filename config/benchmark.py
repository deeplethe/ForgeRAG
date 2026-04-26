"""
Benchmark configuration.

Keeps the LLM judge separated from the answer generator so that we
don't suffer self-preference bias (a model scoring its own answers).
If no separate judge model is configured, we fall back to the
generator's model — but the UI warns the user about the bias.
"""

from __future__ import annotations

from pydantic import BaseModel


class BenchmarkConfig(BaseModel):
    """Benchmark settings. Lives alongside answering / retrieval etc.

    Leave ``model`` empty to disable the dedicated judge — metrics.py will
    fall back to ``answering.generator`` with a self-preference warning.
    Set to a DIFFERENT model than the generator for rigorous scoring.
    """

    model: str = ""  # empty → fall back to answering.generator
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None

    # Judge request timeout.
    timeout: float = 30.0
