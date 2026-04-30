"""Image enrichment configuration."""

from __future__ import annotations

from pydantic import BaseModel


class ImageEnrichmentConfig(BaseModel):
    enabled: bool = False
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    max_workers: int = 4
