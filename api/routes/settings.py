"""
Settings routes — read-only view of the effective yaml configuration.

Yaml is the single source of truth; ``AppState`` mirrors cfg into the
``settings`` table at boot so these endpoints can return a static
snapshot with UI metadata (group, label, description, type, enums)
attached. All mutating routes have been removed — edit yaml and
restart to change configuration.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import get_state
from ..state import AppState

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


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


class SettingsGrouped(BaseModel):
    groups: dict[str, list[SettingOut]]


@router.get("", response_model=SettingsGrouped)
def list_all_settings(state: AppState = Depends(get_state)) -> SettingsGrouped:
    """All settings, grouped. Read-only snapshot of the yaml-loaded cfg."""
    from config.settings_manager import EDITABLE_SETTINGS, PROMPT_DEFAULTS

    key_order = {key: i for i, (key, *_) in enumerate(EDITABLE_SETTINGS)}
    rows = state.store.get_all_settings()
    groups: dict[str, list[SettingOut]] = {}
    for r in rows:
        g = r["group_name"]
        groups.setdefault(g, [])
        out = SettingOut(**r)
        if out.key in PROMPT_DEFAULTS:
            out.default_value = PROMPT_DEFAULTS[out.key]
        groups[g].append(out)
    for g in groups:
        groups[g].sort(key=lambda s: key_order.get(s.key, 9999))
    return SettingsGrouped(groups=groups)


@router.get("/group/{group_name}", response_model=list[SettingOut])
def list_group(group_name: str, state: AppState = Depends(get_state)) -> list[SettingOut]:
    rows = state.store.get_settings_by_group(group_name)
    return [SettingOut(**r) for r in rows]


@router.get("/key/{key:path}", response_model=SettingOut)
def get_setting(key: str, state: AppState = Depends(get_state)) -> SettingOut:
    row = state.store.get_setting(key)
    if not row:
        raise HTTPException(status_code=404, detail=f"setting {key!r} not found")
    return SettingOut(**row)
