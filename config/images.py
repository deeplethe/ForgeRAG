"""Image enrichment configuration."""

from __future__ import annotations

import os

from pydantic import BaseModel


class ImageEnrichmentConfig(BaseModel):
    enabled: bool = False
    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    api_key_env: str | None = None
    api_base: str | None = None
    max_workers: int = 4


# Extensions accepted as image-as-document uploads. Mirrored on the
# frontend (``IMAGE_EXTS`` in ``DocDetail.vue`` / ``Workspace.vue``).
# Add to both sides if extending — or factor out into a shared
# capabilities endpoint payload, which is what we already do via
# ``HealthResponse.features.image_upload_extensions``.
IMAGE_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
)


def is_image_upload_configured(cfg: ImageEnrichmentConfig) -> bool:
    """``True`` iff the deployment can actually ingest image uploads.

    Image-as-document needs a VLM to convert the image into a text
    description; without one the IMAGE block stays text-empty, the
    chunk has no content, and the doc is un-retrievable. We refuse
    the upload up-front rather than silently storing un-searchable
    documents.

    Three conditions, all required:
      1. ``enabled`` switch is on
      2. A model name is set (provider may resolve credentials at
         call time, but the model has to be specified somewhere)
      3. Credentials are reachable — either inline ``api_key`` or
         ``api_key_env`` resolves to a non-empty environment
         variable. Some providers (Ollama, local LLMs) work with
         neither, so we treat "neither set" as configured-for-local
         and let the call fail at runtime if the local server isn't
         reachable. The check is conservative — false-positives
         (claim configured, then fail at VLM call time) are better
         than false-negatives (refuse uploads when a perfectly
         working local VLM is set up).
    """
    if not cfg.enabled:
        return False
    if not cfg.model:
        return False
    # If the env var is named, it has to actually exist with a value.
    # (No-env-var-and-no-inline-key is fine — local provider path.)
    if cfg.api_key_env:
        return bool(os.environ.get(cfg.api_key_env))
    return True
