"""On-disk UI prefs (confirm skips / theme / sync destinations / settings UI).

GET  /api/v1/ui-prefs
PUT  /api/v1/ui-prefs  - merge patch into data/state/ui-prefs.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.events import publish
from apps.shared.paths import DATA_DIR

router = APIRouter(prefix="/ui-prefs", tags=["ui-prefs"])

_FILENAME = "ui-prefs.json"
UiTheme = Literal["dark", "light"]
_DEFAULT_THEME: UiTheme = "dark"
_DEFAULT_AUTO_SYNC: dict[str, bool] = {
    "rekordbox": False,
    "djay": False,
    "open_dj": False,
}
# LIBUX-05: "smooth fade rather than instant appear/disappear" config option
# for technically-working mode's edge-reveal overlay. Default on (animated).
_DEFAULT_TECH_WORKING_ANIMATE = True
_DEFAULT_SHOW_AGENT_PINS = True


def _path(request: Request) -> Path:
    configured = getattr(request.app.state, "data_dir", None)
    root = Path(configured) if configured is not None else DATA_DIR
    return root / "state" / _FILENAME


def _parse_auto_sync(raw: Any) -> dict[str, bool]:
    if raw is None:
        return dict(_DEFAULT_AUTO_SYNC)
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "auto_sync must be an object"},
        )
    out = dict(_DEFAULT_AUTO_SYNC)
    for key in ("rekordbox", "djay", "open_dj"):
        if key not in raw:
            continue
        val = raw[key]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": f"auto_sync.{key} must be a boolean",
                },
            )
        out[key] = val
    return out


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "confirm": {},
            "theme": _DEFAULT_THEME,
            "hide_todo_settings": False,
            "auto_sync": dict(_DEFAULT_AUTO_SYNC),
            "technically_working_animate": _DEFAULT_TECH_WORKING_ANIMATE,
            "show_agent_pins": _DEFAULT_SHOW_AGENT_PINS,
        }
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": f"malformed {path}"},
        )
    confirm = raw.get("confirm", {})
    if not isinstance(confirm, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "confirm must be an object"},
        )
    theme = raw.get("theme", _DEFAULT_THEME)
    if theme not in ("dark", "light"):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "theme must be dark|light"},
        )
    hide_todo = raw.get("hide_todo_settings", False)
    if not isinstance(hide_todo, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "hide_todo_settings must be a boolean",
            },
        )
    animate = raw.get("technically_working_animate", _DEFAULT_TECH_WORKING_ANIMATE)
    if not isinstance(animate, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "technically_working_animate must be a boolean",
            },
        )
    show_agent_pins = raw.get("show_agent_pins", _DEFAULT_SHOW_AGENT_PINS)
    if not isinstance(show_agent_pins, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "show_agent_pins must be a boolean",
            },
        )
    return {
        "confirm": confirm,
        "theme": theme,
        "hide_todo_settings": hide_todo,
        "auto_sync": _parse_auto_sync(raw.get("auto_sync")),
        "technically_working_animate": animate,
        "show_agent_pins": show_agent_pins,
    }


class AutoSyncOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    rekordbox: bool = False
    djay: bool = False
    open_dj: bool = False


class UiPrefsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirm: dict[str, Any] = Field(default_factory=dict)
    theme: UiTheme = _DEFAULT_THEME
    hide_todo_settings: bool = False
    auto_sync: AutoSyncOut = Field(default_factory=AutoSyncOut)
    technically_working_animate: bool = _DEFAULT_TECH_WORKING_ANIMATE
    show_agent_pins: bool = _DEFAULT_SHOW_AGENT_PINS


class UiPrefsPatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirm: dict[str, Any] | None = None
    theme: UiTheme | None = None
    hide_todo_settings: bool | None = None
    auto_sync: AutoSyncOut | None = None
    technically_working_animate: bool | None = None
    show_agent_pins: bool | None = None


@router.get("", response_model=UiPrefsOut)
def get_ui_prefs(request: Request) -> UiPrefsOut:
    return UiPrefsOut.model_validate(_load(_path(request)))


@router.put("", response_model=UiPrefsOut)
def put_ui_prefs(body: UiPrefsPatch, request: Request) -> UiPrefsOut:
    path = _path(request)
    current = _load(path)
    if body.confirm is not None:
        merged = {**current["confirm"], **body.confirm}
        # Drop keys explicitly set to null.
        current["confirm"] = {k: v for k, v in merged.items() if v is not None}
    if body.theme is not None:
        current["theme"] = body.theme
    if body.hide_todo_settings is not None:
        current["hide_todo_settings"] = body.hide_todo_settings
    if body.auto_sync is not None:
        current["auto_sync"] = _parse_auto_sync(body.auto_sync.model_dump())
    if body.technically_working_animate is not None:
        current["technically_working_animate"] = body.technically_working_animate
    if body.show_agent_pins is not None:
        current["show_agent_pins"] = body.show_agent_pins
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    publish("library.changed", {"kind": "ui_prefs", "ids": []})
    return UiPrefsOut.model_validate(current)
