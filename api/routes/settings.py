"""
Settings routes: frontend-editable config overrides.

    GET    /settings                 -- all settings grouped
    GET    /settings/{group}         -- settings for one group
    PUT    /settings/{key}           -- update one setting
    PUT    /settings                 -- batch update
    DELETE /settings/{key}           -- reset to yaml default
    POST   /settings/apply          -- re-apply all DB overrides to live config
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import get_state
from ..state import AppState

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SettingOut(BaseModel):
    key: str
    value_json: Any
    group_name: str
    label: str
    description: str | None = None
    value_type: str
    enum_options: list | None = None
    default_value: str | None = None
    updated_at: Any = None


class SettingUpdate(BaseModel):
    value_json: Any = Field(..., description="New value for this setting")


class BatchUpdate(BaseModel):
    settings: list[dict[str, Any]] = Field(
        ...,
        description='List of {"key": "...", "value_json": ...} objects',
    )


class SettingsGrouped(BaseModel):
    groups: dict[str, list[SettingOut]]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=SettingsGrouped)
def list_all_settings(state: AppState = Depends(get_state)) -> SettingsGrouped:
    """All settings, organized by group for frontend tabs."""
    from config.settings_manager import EDITABLE_SETTINGS, PROMPT_DEFAULTS

    # Build a key→order index from the canonical definition order
    key_order = {key: i for i, (key, *_) in enumerate(EDITABLE_SETTINGS)}

    rows = state.store.get_all_settings()
    groups: dict[str, list[SettingOut]] = {}
    for r in rows:
        g = r["group_name"]
        if g not in groups:
            groups[g] = []
        out = SettingOut(**r)
        if out.key in PROMPT_DEFAULTS:
            out.default_value = PROMPT_DEFAULTS[out.key]
        groups[g].append(out)
    # Sort each group by canonical definition order
    for g in groups:
        groups[g].sort(key=lambda s: key_order.get(s.key, 9999))
    return SettingsGrouped(groups=groups)


@router.get("/group/{group_name}", response_model=list[SettingOut])
def list_group(
    group_name: str,
    state: AppState = Depends(get_state),
) -> list[SettingOut]:
    rows = state.store.get_settings_by_group(group_name)
    return [SettingOut(**r) for r in rows]


@router.get("/key/{key:path}", response_model=SettingOut)
def get_setting(
    key: str,
    state: AppState = Depends(get_state),
) -> SettingOut:
    row = state.store.get_setting(key)
    if not row:
        raise HTTPException(status_code=404, detail=f"setting {key!r} not found")
    return SettingOut(**row)


@router.put("/key/{key:path}", response_model=SettingOut)
def update_setting(
    key: str,
    body: SettingUpdate,
    state: AppState = Depends(get_state),
) -> SettingOut:
    existing = state.store.get_setting(key)
    if not existing:
        # Auto-seed if the key is in EDITABLE_SETTINGS (e.g. newly added config)
        from config.settings_manager import EDITABLE_SETTINGS

        meta = next((s for s in EDITABLE_SETTINGS if s[0] == key), None)
        if not meta:
            raise HTTPException(status_code=404, detail=f"setting {key!r} not found")
        state.store.upsert_setting(
            {
                "key": key,
                "value_json": body.value_json,
                "group_name": meta[1],
                "label": meta[2],
                "description": meta[3],
                "value_type": meta[4],
                "enum_options": meta[5],
            }
        )
    else:
        state.store.upsert_setting({"key": key, "value_json": body.value_json})

    # Apply immediately to live config
    from config.settings_manager import _set_dotted, resolve_providers

    try:
        _set_dotted(state.cfg, key, body.value_json)
    except Exception:
        pass  # setting saved but config path may not exist

    # If a provider_id changed, resolve it to model/key/base
    if key.endswith(".provider_id"):
        resolve_providers(state.cfg, state.store)

    # Invalidate caches that depend on changed config
    _invalidate_if_needed(state, key)

    row = state.store.get_setting(key)
    return SettingOut(**row)


@router.put("", response_model=list[SettingOut])
def batch_update(
    body: BatchUpdate,
    state: AppState = Depends(get_state),
) -> list[SettingOut]:
    """Update multiple settings at once."""
    from config.settings_manager import _set_dotted, resolve_providers

    results = []
    has_provider_change = False
    for item in body.settings:
        key = item.get("key")
        value = item.get("value_json")
        if not key:
            continue
        existing = state.store.get_setting(key)
        if not existing:
            continue
        state.store.upsert_setting({"key": key, "value_json": value})
        with contextlib.suppress(Exception):
            _set_dotted(state.cfg, key, value)
        if key.endswith(".provider_id"):
            has_provider_change = True
        row = state.store.get_setting(key)
        if row:
            results.append(SettingOut(**row))

    if has_provider_change:
        resolve_providers(state.cfg, state.store)
    _invalidate_if_needed(state, "")  # broad invalidation
    return results


@router.delete("/key/{key:path}")
def reset_setting(
    key: str,
    state: AppState = Depends(get_state),
):
    """Delete a DB override, reverting to the yaml default."""
    state.store.delete_setting(key)
    # Re-seed from yaml so the frontend still sees the key
    from config.settings_manager import seed_defaults

    seed_defaults(state.cfg, state.store)
    return {"reset": key}


@router.post("/reset-all")
def reset_all_settings(state: AppState = Depends(get_state)):
    """Delete ALL DB overrides, re-seed from yaml defaults."""
    all_settings = state.store.get_all_settings()
    for s in all_settings:
        state.store.delete_setting(s["key"])
    from config.settings_manager import seed_defaults

    count = seed_defaults(state.cfg, state.store)
    _invalidate_if_needed(state, "")
    return {"reset": len(all_settings), "reseeded": count}


@router.post("/reset-group/{group_name}")
def reset_group(group_name: str, state: AppState = Depends(get_state)):
    """Delete DB overrides for one group, re-seed from yaml."""
    settings = state.store.get_settings_by_group(group_name)
    for s in settings:
        state.store.delete_setting(s["key"])
    from config.settings_manager import seed_defaults

    count = seed_defaults(state.cfg, state.store)
    _invalidate_if_needed(state, "")
    return {"group": group_name, "reset": len(settings), "reseeded": count}


@router.post("/apply")
def apply_all(state: AppState = Depends(get_state)):
    """Re-apply all DB overrides to the live config."""
    from config.settings_manager import apply_overrides, resolve_providers

    count = apply_overrides(state.cfg, state.store)
    resolved = resolve_providers(state.cfg, state.store)
    _invalidate_if_needed(state, "")
    return {"applied": count, "providers_resolved": resolved}


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def _invalidate_if_needed(state: AppState, key: str) -> None:
    """
    When certain settings change, internal caches must be rebuilt.
    """
    if not key:
        # Broad invalidation (batch update / reset)
        state._retrieval = None
        state._answering = None
        state._bm25 = None
        return
    prefixes = ("retrieval", "embedder", "answering", "image_enrichment", "parser")
    if any(key.startswith(p) for p in prefixes):
        state._retrieval = None
        state._answering = None
        if key.startswith("retrieval.bm25") or key.startswith("parser.chunker"):
            state._bm25 = None
