"""
Configuration loader.

Reads a YAML file into AppConfig. Missing file -> pydantic defaults
for every section. Each module that calls an LLM (answering.generator,
embedder, retrieval.rerank, etc.) carries its own ``model``,
``api_key`` / ``api_key_env``, and ``api_base`` fields — fill them in
inline per module before that module will function.

Two-layer config:

    1. Authoritative yaml (the path passed to ``load_config``,
       typically ``opencraig.yaml`` at the repo root or
       ``/app/config.yaml`` inside the container).
    2. Optional overlay yaml at ``$OPENCRAIG_OVERLAY`` or
       ``./storage/setup-overlay.yaml`` (or ``/app/storage/setup-overlay.yaml``
       in docker). Created by the first-boot wizard. Layered on top
       of the base — keys present in both win from the overlay.

The overlay layer exists so the wizard can write LLM keys / model
choices without rewriting the operator's hand-edited yaml (which is
typically version-controlled and bind-mounted read-only). On first
boot the overlay is missing → pure-yaml behaviour, exactly as before.
"""

from __future__ import annotations

import os
from pathlib import Path

from .app import AppConfig


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate opencraig.yaml (or return defaults).

    If a setup-overlay file exists (``$OPENCRAIG_OVERLAY`` or the
    default location under ``storage/``), its values are deep-merged
    on top of the base yaml. Operators who skip the wizard see the
    pre-overlay behaviour unchanged — no overlay file means no
    merge.
    """
    import yaml  # lazy import

    raw: dict = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

    overlay_path = _overlay_path(path)
    if overlay_path is not None and overlay_path.exists():
        with open(overlay_path, encoding="utf-8") as f:
            overlay = yaml.safe_load(f) or {}
        if overlay:
            raw = _deep_merge(raw, overlay)

    return AppConfig.model_validate(raw)


def _overlay_path(base_path: str | Path | None) -> Path | None:
    """Resolve where the wizard-written overlay should live.

    Priority:
      1. ``$OPENCRAIG_OVERLAY`` env var (explicit operator choice).
      2. ``<storage_root>/setup-overlay.yaml`` next to the storage
         volume so the container can write there even when the main
         yaml is bind-mounted read-only. We default to ``./storage/``
         relative to the cwd; the docker image's working dir is
         ``/app`` so this matches the bind-mounted ``storage`` volume
         at ``/app/storage``.
    """
    explicit = os.environ.get("OPENCRAIG_OVERLAY")
    if explicit:
        return Path(explicit)
    # Don't try to be clever about the base_path's directory — the
    # storage volume is the rw filesystem and that's where we write.
    return Path("storage") / "setup-overlay.yaml"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursive dict merge — overlay wins on key collision; lists
    are replaced wholesale (not concatenated) since most config
    lists are exhaustive (allowed_mime_prefixes, public_paths, ...)
    and concatenation would silently re-add removed entries."""
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
