"""On-disk UI prefs (confirm skips / theme / sync destinations / settings UI).

GET  /api/v1/ui-prefs
PUT  /api/v1/ui-prefs  - merge patch into data/state/ui-prefs.json
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.events import publish
from apps.shared.paths import DATA_DIR

router = APIRouter(prefix="/ui-prefs", tags=["ui-prefs"])

_FILENAME = "ui-prefs.json"
UiTheme = Literal["dark", "light"]
PerfTierPref = Literal["auto", "low", "standard", "high"]
AppPosturePref = Literal["prep", "gig"]
_DEFAULT_THEME: UiTheme = "dark"
_DEFAULT_PERF_TIER: PerfTierPref = "auto"
_DEFAULT_APP_POSTURE: AppPosturePref = "prep"
_DEFAULT_APP_MODE: dict[str, Any] = {"last_gig_at": None}
_DEFAULT_AUTO_SYNC: dict[str, bool] = {
    "rekordbox": False,
    "djay": False,
    "open_dj": False,
}
# LIBUX-05: "smooth fade rather than instant appear/disappear" config option
# for technically-working mode's edge-reveal overlay. Default on (animated).
_DEFAULT_TECH_WORKING_ANIMATE = True
_DEFAULT_JOG_RADIAL_WAVEFORM = False
_DEFAULT_SHOW_AGENT_PINS = True
_DEFAULT_BEAT_SYNC_MAX = True
_DEFAULT_AUTO_PLAY_ENABLED = True
_DEFAULT_AUTO_PLAY_ENFORCE_ORDER = False
_DEFAULT_AUTO_PLAY_MAXIMIZE_REACH = True
_TOPBAR_BOOL_DEFAULTS: dict[str, bool] = {
    "beat_sync_max": _DEFAULT_BEAT_SYNC_MAX,
    "auto_play_enabled": _DEFAULT_AUTO_PLAY_ENABLED,
    "auto_play_enforce_order": _DEFAULT_AUTO_PLAY_ENFORCE_ORDER,
    "auto_play_maximize_reach": _DEFAULT_AUTO_PLAY_MAXIMIZE_REACH,
}

# Karaoke lyric prefs (PR-4 section C). Every surface ships ON; the loading
# strategy defaults to the middle setting because preloading a playlist of
# word timings is the RAM cost "in-view" opts into deliberately.
LyricsLoadStrategy = Literal["in-view", "hover", "off"]
_DEFAULT_LYRICS_LOAD_STRATEGY: LyricsLoadStrategy = "hover"
_DEFAULT_LYRICS_BOOLS: dict[str, bool] = {
    "lyrics_global": True,
    "lyrics_library_col": True,
    "lyrics_hover_scrub": True,
    "lyrics_waveform_overlay": True,
    "lyrics_deck_line": True,
}
_LYRICS_KEYS: tuple[str, ...] = (*_DEFAULT_LYRICS_BOOLS, "lyrics_load_strategy")


def _path(request: Request) -> Path:
    configured = getattr(request.app.state, "data_dir", None)
    root = Path(configured) if configured is not None else DATA_DIR
    return root / "state" / _FILENAME


# Level calibration is stored in dBFS, the same unit the meter reads. The
# bounds are deliberately wide: -60 is the meter's floor and +12 allows for the
# loudness-war masters that made calibration necessary in the first place
# (measured median true peak across the library: +1.0 dBTP). ceiling_dbfs gets
# a tighter upper bound: the ceiling is min(1, 10**(dbfs/20)) applied to the
# master gain, which is a no-op attenuation for any dbfs above 0 -- accepting
# one there would let the UI show M as enabled while the gain stays untouched.
_CAL_MIN_DBFS = -60.0
_CAL_MAX_DBFS = 12.0
_CAL_CEILING_MAX_DBFS = 0.0
_DEFAULT_LEVEL_CALIBRATION: dict[str, Any] = {
    "red_dbfs": None,
    "red_enabled": False,
    "ceiling_dbfs": None,
    "ceiling_enabled": False,
}


def _parse_cal_dbfs_fields(raw: dict[str, Any], out: dict[str, Any]) -> None:
    for key in ("red_dbfs", "ceiling_dbfs"):
        if key not in raw:
            continue
        val = raw[key]
        if val is None:
            out[key] = None
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": f"level_calibration.{key} must be a number or null",
                },
            )
        cal_max = _CAL_CEILING_MAX_DBFS if key == "ceiling_dbfs" else _CAL_MAX_DBFS
        if not _CAL_MIN_DBFS <= float(val) <= cal_max:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": (
                        f"level_calibration.{key} must be between {_CAL_MIN_DBFS} "
                        f"and {cal_max} dBFS, got {val}"
                    ),
                },
            )
        out[key] = float(val)


def _parse_cal_enabled_fields(raw: dict[str, Any], out: dict[str, Any]) -> None:
    for key in ("red_enabled", "ceiling_enabled"):
        if key not in raw:
            continue
        val = raw[key]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": f"level_calibration.{key} must be a boolean",
                },
            )
        out[key] = val


def _assert_cal_enabled_fields_have_a_level(out: dict[str, Any]) -> None:
    # Enabling a toggle with no captured level would silently do nothing, which
    # is worse than refusing: the user would think the calibration was applied.
    for flag, level in (("red_enabled", "red_dbfs"), ("ceiling_enabled", "ceiling_dbfs")):
        if out[flag] and out[level] is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": f"level_calibration.{flag} requires {level} to be set",
                },
            )


def _parse_level_calibration(raw: Any) -> dict[str, Any]:
    """Validate the by-ear level calibration. A null level is 'never set'."""
    if raw is None:
        return dict(_DEFAULT_LEVEL_CALIBRATION)
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "level_calibration must be an object"},
        )
    out = dict(_DEFAULT_LEVEL_CALIBRATION)
    _parse_cal_dbfs_fields(raw, out)
    _parse_cal_enabled_fields(raw, out)
    _assert_cal_enabled_fields_have_a_level(out)
    return out


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


def _lyrics_defaults() -> dict[str, Any]:
    return {**_DEFAULT_LYRICS_BOOLS, "lyrics_load_strategy": _DEFAULT_LYRICS_LOAD_STRATEGY}


def _parse_lyrics(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill the six lyric prefs from a stored blob. A missing key is a blob
    written before that key existed and takes its default; a present but
    wrong-typed key is refused rather than silently coerced."""
    out = _lyrics_defaults()
    for key in _DEFAULT_LYRICS_BOOLS:
        if key not in raw:
            continue
        if not isinstance(raw[key], bool):
            raise HTTPException(
                status_code=422,
                detail={"code": "UI_PREFS_INVALID", "message": f"{key} must be a boolean"},
            )
        out[key] = raw[key]
    if "lyrics_load_strategy" in raw:
        strategy = raw["lyrics_load_strategy"]
        if strategy not in ("in-view", "hover", "off"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": "lyrics_load_strategy must be in-view|hover|off",
                },
            )
        out["lyrics_load_strategy"] = strategy
    return out


def _parse_perf_tier(raw: dict[str, Any]) -> str:
    if "perf_tier" not in raw:
        return _DEFAULT_PERF_TIER
    value = raw["perf_tier"]
    if value not in ("auto", "low", "standard", "high"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "perf_tier must be auto|low|standard|high",
            },
        )
    return value


def _parse_topbar_bool_prefs(raw: dict[str, Any]) -> dict[str, bool]:
    out = dict(_TOPBAR_BOOL_DEFAULTS)
    for key in _TOPBAR_BOOL_DEFAULTS:
        if key not in raw:
            continue
        val = raw[key]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=422,
                detail={"code": "UI_PREFS_INVALID", "message": f"{key} must be a boolean"},
            )
        out[key] = val
    return out


def _parse_last_gig_at(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "app_mode.last_gig_at must be a UTC ISO string or null",
            },
        )
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("non-UTC offset")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": f"app_mode.last_gig_at is not a valid UTC ISO timestamp: {exc}",
            },
        ) from exc
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_app_mode(raw: Any) -> dict[str, Any]:
    if raw is None:
        return dict(_DEFAULT_APP_MODE)
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "app_mode must be an object"},
        )
    out = dict(_DEFAULT_APP_MODE)
    if "last_gig_at" not in raw:
        return out
    out["last_gig_at"] = _parse_last_gig_at(raw["last_gig_at"])
    return out


def _parse_app_posture(raw: dict[str, Any]) -> str:
    if "app_posture" not in raw:
        return _DEFAULT_APP_POSTURE
    value = raw["app_posture"]
    if value not in ("prep", "gig"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "app_posture must be prep|gig",
            },
        )
    return value


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "confirm": {},
            "theme": _DEFAULT_THEME,
            "hide_todo_settings": False,
            "auto_sync": dict(_DEFAULT_AUTO_SYNC),
            "technically_working_animate": _DEFAULT_TECH_WORKING_ANIMATE,
            "jog_radial_waveform": _DEFAULT_JOG_RADIAL_WAVEFORM,
            "show_agent_pins": _DEFAULT_SHOW_AGENT_PINS,
            "level_calibration": dict(_DEFAULT_LEVEL_CALIBRATION),
            "perf_tier": _DEFAULT_PERF_TIER,
            "app_posture": _DEFAULT_APP_POSTURE,
            "app_mode": dict(_DEFAULT_APP_MODE),
            **_TOPBAR_BOOL_DEFAULTS,
            **_lyrics_defaults(),
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
    jog_radial = raw.get("jog_radial_waveform", _DEFAULT_JOG_RADIAL_WAVEFORM)
    if not isinstance(jog_radial, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "jog_radial_waveform must be a boolean",
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
        "jog_radial_waveform": jog_radial,
        "show_agent_pins": show_agent_pins,
        "level_calibration": _parse_level_calibration(raw.get("level_calibration")),
        "perf_tier": _parse_perf_tier(raw),
        "app_posture": _parse_app_posture(raw),
        "app_mode": _parse_app_mode(raw.get("app_mode")),
        **_parse_topbar_bool_prefs(raw),
        **_parse_lyrics(raw),
    }


class AutoSyncOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    rekordbox: bool = False
    djay: bool = False
    open_dj: bool = False


class AppModeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    last_gig_at: str | None = None


class LevelCalibrationOut(BaseModel):
    """By-ear level calibration, captured from live playback.

    `red_dbfs` anchors the meter's first RED segment; `ceiling_dbfs` is the
    master output ceiling. They are independent: either can be set and toggled
    without the other.
    """

    model_config = ConfigDict(frozen=True)

    red_dbfs: float | None = None
    red_enabled: bool = False
    ceiling_dbfs: float | None = None
    ceiling_enabled: bool = False


class UiPrefsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirm: dict[str, Any] = Field(default_factory=dict)
    theme: UiTheme = _DEFAULT_THEME
    hide_todo_settings: bool = False
    auto_sync: AutoSyncOut = Field(default_factory=AutoSyncOut)
    technically_working_animate: bool = _DEFAULT_TECH_WORKING_ANIMATE
    jog_radial_waveform: bool = _DEFAULT_JOG_RADIAL_WAVEFORM
    show_agent_pins: bool = _DEFAULT_SHOW_AGENT_PINS
    level_calibration: LevelCalibrationOut = Field(default_factory=LevelCalibrationOut)
    lyrics_global: bool = _DEFAULT_LYRICS_BOOLS["lyrics_global"]
    lyrics_library_col: bool = _DEFAULT_LYRICS_BOOLS["lyrics_library_col"]
    lyrics_hover_scrub: bool = _DEFAULT_LYRICS_BOOLS["lyrics_hover_scrub"]
    lyrics_load_strategy: LyricsLoadStrategy = _DEFAULT_LYRICS_LOAD_STRATEGY
    lyrics_waveform_overlay: bool = _DEFAULT_LYRICS_BOOLS["lyrics_waveform_overlay"]
    lyrics_deck_line: bool = _DEFAULT_LYRICS_BOOLS["lyrics_deck_line"]
    perf_tier: PerfTierPref = _DEFAULT_PERF_TIER
    app_posture: AppPosturePref = _DEFAULT_APP_POSTURE
    app_mode: AppModeOut = Field(default_factory=AppModeOut)
    beat_sync_max: bool = _DEFAULT_BEAT_SYNC_MAX
    auto_play_enabled: bool = _DEFAULT_AUTO_PLAY_ENABLED
    auto_play_enforce_order: bool = _DEFAULT_AUTO_PLAY_ENFORCE_ORDER
    auto_play_maximize_reach: bool = _DEFAULT_AUTO_PLAY_MAXIMIZE_REACH


class UiPrefsPatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirm: dict[str, Any] | None = None
    theme: UiTheme | None = None
    hide_todo_settings: bool | None = None
    auto_sync: AutoSyncOut | None = None
    technically_working_animate: bool | None = None
    jog_radial_waveform: bool | None = None
    show_agent_pins: bool | None = None
    level_calibration: LevelCalibrationOut | None = None
    lyrics_global: bool | None = None
    lyrics_library_col: bool | None = None
    lyrics_hover_scrub: bool | None = None
    lyrics_load_strategy: LyricsLoadStrategy | None = None
    lyrics_waveform_overlay: bool | None = None
    lyrics_deck_line: bool | None = None
    perf_tier: PerfTierPref | None = None
    app_posture: AppPosturePref | None = None
    app_mode: AppModeOut | None = None
    beat_sync_max: bool | None = None
    auto_play_enabled: bool | None = None
    auto_play_enforce_order: bool | None = None
    auto_play_maximize_reach: bool | None = None


def _merge_topbar_bool_prefs(current: dict[str, Any], body: UiPrefsPatch) -> None:
    for key in _TOPBAR_BOOL_DEFAULTS:
        value = getattr(body, key)
        if value is not None:
            current[key] = value


def _merge_lyrics(current: dict[str, Any], body: UiPrefsPatch) -> None:
    """Apply the six lyric prefs a PATCH names, leaving the rest as stored.

    Its own helper rather than an inline loop in `put_ui_prefs`: that route
    already carries one branch per pref and the loop tipped it over the
    complexity ratchet's CC limit. Pydantic has already validated the values,
    so this only decides which keys the caller actually named.
    """
    for key in _LYRICS_KEYS:
        value = getattr(body, key)
        if value is not None:
            current[key] = value


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
    if body.jog_radial_waveform is not None:
        current["jog_radial_waveform"] = body.jog_radial_waveform
    if body.show_agent_pins is not None:
        current["show_agent_pins"] = body.show_agent_pins
    _merge_lyrics(current, body)
    if body.perf_tier is not None:
        current["perf_tier"] = body.perf_tier
    if body.app_posture is not None:
        current["app_posture"] = body.app_posture
    if body.app_mode is not None:
        current["app_mode"] = _parse_app_mode(
            {**current["app_mode"], **body.app_mode.model_dump(exclude_unset=True)}
        )
    _merge_topbar_bool_prefs(current, body)
    if body.level_calibration is not None:
        # R and M are independent (see LevelCalibrationOut docstring): merge onto
        # what's stored so a PUT naming only one half cannot silently wipe the
        # other, the way a bare replace against LevelCalibrationOut's own
        # per-field defaults would.
        current["level_calibration"] = _parse_level_calibration(
            {**current["level_calibration"], **body.level_calibration.model_dump(exclude_unset=True)}
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    publish("library.changed", {"kind": "ui_prefs", "ids": []})
    return UiPrefsOut.model_validate(current)
