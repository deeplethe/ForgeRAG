"""
Configuration loader.

Reads a YAML file into AppConfig. Missing file -> pydantic defaults
for every section. Each module that calls an LLM (answering.generator,
embedder, retrieval.rerank, etc.) carries its own ``model``,
``api_key`` / ``api_key_env``, and ``api_base`` fields — fill them in
inline per module before that module will function.
"""

from __future__ import annotations

from pathlib import Path

from .app import AppConfig


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate forgerag.yaml (or return defaults)."""
    if path is None:
        return AppConfig()

    import yaml  # lazy import

    p = Path(path)
    if not p.exists():
        return AppConfig()
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)
