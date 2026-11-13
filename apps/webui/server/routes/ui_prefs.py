"""On-disk UI prefs (confirm skips / theme / sync destinations / settings UI).

GET  /api/v1/ui-prefs
PUT  /api/v1/ui-prefs  - merge patch into (private path)
POST /api/v1/ui-prefs/library-key-filter - the library key wheel's actions
     (toggle / compatible / reset / set), the HTTP twin of each wheel control
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, get_args

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from apps.analysis.backlog import AutoLane
from apps.lighting.contract import ClockSourceError, parse_clock_source
from apps.lighting.http import bind_lighting_runtime, lighting_refusal
from apps.shared.events import publish
from apps.shared.harmonic import CAMELOT_RULE_IDS, DEFAULT_CAMELOT_RULE_IDS, camelot_rules_allow
from apps.shared.paths import DATA_DIR

from ..auto_analyze_prefs import AUTO_ANALYZE_BOOL_DEFAULTS, lanes_from_prefs
from ..auto_gain_prefs import DEFAULT_AUTO_GAIN, AutoGainPrefsOut, merge_auto_gain, parse_auto_gain
from ..library_related_prefs import (
    LibraryRelatedCriteriaOut,
    merge_library_related_criteria,
    parse_library_related_criteria,
)
from ..library_tabs_prefs import (
    LibraryTabsActionIn,
    LibraryTabsActionOut,
    LibraryTabsOut,
    apply_library_tabs_action,
    merge_library_tabs,
    parse_library_tabs,
)
from ..lighting_clock import LightingClockController, clock_snapshot

router = APIRouter(prefix="/ui-prefs", tags=["ui-prefs"])
MOUNT_UNDER_API_PREFIX = True

_write_lock = threading.Lock()

_FILENAME = "ui-prefs.json"
UiTheme = Literal["dark", "light"]
PerfTierPref = Literal["auto", "low", "standard", "high"]
AppPosturePref = Literal["prep", "gig"]
GigHelperPref = Literal["unset", "off", "on"]
_DEFAULT_GIG_HELPER: GigHelperPref = "unset"
AppModePref = Literal["performance", "library-management", "library", "music-player"]
_DEFAULT_THEME: UiTheme = "dark"
_DEFAULT_PERF_TIER: PerfTierPref = "auto"
_DEFAULT_APP_POSTURE: AppPosturePref = "prep"
_DEFAULT_APP_MODE: dict[str, Any] = {"last_gig_at": None, "id": "performance"}
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
_DEFAULT_SHOW_STEMS = False
# STEMPERF: main waveform follows the sole audible stem (rekordbox ACTIVE STEM, mimicked).
_DEFAULT_WAVE_STEM_FOLLOW = False
# A11Y-11811: Appearance > Reduce motion; OR'd with the OS media query in the browser.
_DEFAULT_REDUCE_MOTION = False
_DEFAULT_DECK_HOVER_LINK = True
# LIBUX-11716: shrink 4-deck view so the library shows more rows. Off by default.
_DEFAULT_COMPACT_DECKS = False
_DEFAULT_BEAT_SYNC_MAX = True
_DEFAULT_AUTO_PLAY_ENABLED = False
_DEFAULT_AUTO_PLAY_ENFORCE_ORDER = False
_DEFAULT_AUTO_PLAY_MAXIMIZE_REACH = True
_DEFAULT_MASTER_MUTED = False
# DECKCTL-6235 (alpha status I11): the deck AU button. Off by default so a load
# still lands at 0:00 until the DJ turns it on.
_DEFAULT_DECK_AUTOCUE = False
# TOPVIEW-04 / TOPVIEW-06: the PAD toggle and two-deck AutoPlay.
_DEFAULT_SHOW_PADS = True
_DEFAULT_AUTO_PLAY_TWO_DECK = False
_TOPBAR_BOOL_DEFAULTS: dict[str, bool] = {
    "beat_sync_max": _DEFAULT_BEAT_SYNC_MAX,
    "auto_play_enabled": _DEFAULT_AUTO_PLAY_ENABLED,
    "auto_play_enforce_order": _DEFAULT_AUTO_PLAY_ENFORCE_ORDER,
    "auto_play_maximize_reach": _DEFAULT_AUTO_PLAY_MAXIMIZE_REACH,
    "master_muted": _DEFAULT_MASTER_MUTED,
    "deck_autocue": _DEFAULT_DECK_AUTOCUE,
    "show_pads": _DEFAULT_SHOW_PADS,
    "auto_play_two_deck": _DEFAULT_AUTO_PLAY_TWO_DECK,
}
# TOPVIEW-01..03: what the /performance wavestack area shows.
PerfView = Literal["waveform", "list", "phase-scope", "phase-meter"]
_DEFAULT_PERF_VIEW: PerfView = "waveform"
# SETSTUB-02: the Quantize state a deck starts with when the page opens or the
# deck is ejected ("Quantize on new decks"). Default on, the value every deck
# was hardcoded to before this pref existed.
_DEFAULT_DECK_DEFAULT_QUANTIZE = True
# DECKCTL-10421: Needle Lock, a playing deck's overview strip ignores clicks (rekordbox). Default off.
_DEFAULT_DECK_NEEDLE_LOCK = False
_DECK_DEFAULT_BOOL_DEFAULTS: dict[str, bool] = {
    "deck_default_quantize": _DEFAULT_DECK_DEFAULT_QUANTIZE,
    "deck_needle_lock": _DEFAULT_DECK_NEEDLE_LOCK,
}
# STRUCT-05: translucent labeled section bands over the deck strip, the main
# scrolling waveform and the browser preview. Default off so nothing changes
# for an existing user until they opt in.
_DEFAULT_SHOW_SECTIONS = False
# STRUCTUI-12787 (issue #11436): per-surface section display styles (defaults M1/O1/hide).
SectionsMainStyle = Literal["m1_tint", "m2_recolor", "m3_lines", "m4_base"]
_DEFAULT_SECTIONS_MAIN_STYLE: SectionsMainStyle = "m1_tint"
SectionsOverviewStyle = Literal["o1_strip", "o2_tint", "o3_recolor"]
_DEFAULT_SECTIONS_OVERVIEW_STYLE: SectionsOverviewStyle = "o1_strip"
SectionsUncertain = Literal["hide", "show_faded"]
_DEFAULT_SECTIONS_UNCERTAIN: SectionsUncertain = "hide"
_DEFAULT_SECTIONS_STICKY_TITLE = True
_DEFAULT_SECTIONS_NEXT_COUNTDOWN = True
_DEFAULT_SECTIONS_LIBRARY_STRUCTURE_COLUMN = True
_DEFAULT_SECTIONS_PREVIEW_CURSOR_LABEL = True
# Tempo-change lane (PR #8342): the deck strip's tempo lane and the main
# waveform's tempo curve. Default on: it only paints where the beatgrid
# actually moves, so a steady track shows nothing new.
_DEFAULT_SHOW_TEMPO_LANE = True
# issue #10238: the blue vocal bars on the deck rows and overview strip, default on.
_DEFAULT_SHOW_VOCAL_LANE = True
# rekordbox waveform view options (issue #10248): hot, memory and loop cue
# markers each default on so existing users see no change.
_WAVE_OVERLAY_BOOL_DEFAULTS: dict[str, bool] = {
    "show_sections": _DEFAULT_SHOW_SECTIONS,
    "show_tempo_lane": _DEFAULT_SHOW_TEMPO_LANE,
    "show_hot_cue_markers": True,
    "show_memory_cue_markers": True,
    "show_loop_markers": True,
    "show_vocal_lane": _DEFAULT_SHOW_VOCAL_LANE,
}
# The tempo curve's vertical scale: fixed_10 = full height is 10 percent off
# nominal BPM; relative = full height is the track's own largest change,
# floored at 2 percent so jitter on a steady grid never fills the lane.
TempoLaneScale = Literal["fixed_10", "relative"]
_DEFAULT_TEMPO_LANE_SCALE: TempoLaneScale = "fixed_10"
# WAVEQS-11733: deck waveform mirrored (full) or single-sided from the bottom baseline (half).
WaveHeight = Literal["full", "half"]
_DEFAULT_WAVE_HEIGHT: WaveHeight = "full"
# Issue #10244: the deck waveforms' beat grid lines. beats_bars = every beat
# plus stronger downbeats (default); bars = downbeats only; off = none.
WaveGridLines = Literal["beats_bars", "bars", "off"]
_DEFAULT_WAVE_GRID_LINES: WaveGridLines = "beats_bars"
# AUDIO-DEVICE-06: auto-reconnect a stalled Bluetooth main speaker. auto = at
# home (prep posture) only, never in gig posture; on = always; off = never.
SpeakerAutoRecover = Literal["auto", "on", "off"]
_DEFAULT_SPEAKER_AUTO_RECOVER: SpeakerAutoRecover = "auto"
LightingProtocol = Literal["off", "artnet", "sacn", "osc"]
_DEFAULT_LIGHTING: dict[str, Any] = {"enabled": False, "protocol": "off", "clock_source": "master"}

# Issue #2854: library browser prefs, wheel sensitivity, MIDI enabled choice.
LibraryDensity = Literal["compact", "cosy"]
# One notation for every key cell (operator report, Sat 24 Oct 2026): keys reach the
# browser as canonical Camelot, and this picks how every row renders them.
KeyNotation = Literal["camelot_musical", "camelot", "open_key", "musical"]
_KEY_NOTATIONS: tuple[str, ...] = ("camelot_musical", "camelot", "open_key", "musical")
PlaylistTreeView = Literal["tree", "column"]
# Library columns the header chooser can hide, in table order. Mirrors
# HIDEABLE_COLUMNS in apps/webui/frontend/src/lib/rb/library-columns.ts.
LibraryHideableColumn = Literal[
    "preview",
    "art",
    "artist",
    "album",
    "key",
    "bpm",
    "tempostab",
    "plays",
    "rating",
    "colour",
    "comments",
    "time",
    "quality",
    "energy",
    "genre",
    "stems",
    "tags",
    "filename",
    "location",
    "date_added",
    "bitrate",
    "kind",
    "last_played",
]
_LIBRARY_HIDEABLE_COLUMNS: tuple[str, ...] = get_args(LibraryHideableColumn)
_DEFAULT_LIBRARY_HIDDEN_COLUMNS: list[str] = ["album", "filename", "location", "date_added", "bitrate", "kind", "last_played"]
# Off by default, so "hidden" cannot express them. Table order, mirrored by
# OPTIONAL_COLUMNS in frontend/src/lib/rb/library-works.ts.
LibraryOptionalColumn = Literal["work", "composer", "ensemble", "label", "year"]
_LIBRARY_OPTIONAL_COLUMNS: tuple[str, ...] = get_args(LibraryOptionalColumn)
CompatibleBpmDirection = Literal["both", "above", "below", "same"]
_DEFAULT_HIDE_BROKEN_LINKS = False
_DEFAULT_DECK_RIGHT_MIRROR = False
_DEFAULT_PLAYLIST_TREE_VIEW: PlaylistTreeView = "tree"
_DEFAULT_LIBRARY_DENSITY: LibraryDensity = "compact"
_DEFAULT_KEY_NOTATION: KeyNotation = "camelot_musical"
_DEFAULT_LIBRARY_FILTER_BOOLS: dict[str, bool] = {
    "next_only_filter": False,
    "remixes_filter": False,
    "vocals_filter": False,
    "available_offline_filter": False,
}
_DEFAULT_WHEEL_SENSITIVITY: dict[str, float] = {
    "mouse": 1.0,
    "trackpad": 1.0 / 3.0,
    "library_mouse": 0.5,
    "library_trackpad": 0.5 / 3.0,
}
_LIBRARY_WHEEL_MIN = 0.05
_LIBRARY_WHEEL_MAX = 1.0
_DEFAULT_MIDI_ENABLED = False
# The docked Spotify player shrinks Open DJ to make room unless this is off.
_DEFAULT_SPOTIFY_SHRINK_MAIN = True
# ARTTHUMB-8592: playlist art thumbnails in the sidebar (library view option).
_DEFAULT_LIBRARY_SIDEBAR_ART = True
# DECKUX-10470: per-deck beat jump size (rekordbox / CDJ-3000 sizes, default 4).
_BEAT_JUMP_SIZE_CHOICES: tuple[float, ...] = (0.5, 1, 2, 4, 8, 16, 32, 64)
_BEAT_JUMP_DECK_KEYS: tuple[str, ...] = ("deck_1", "deck_2", "deck_3", "deck_4")
_DEFAULT_BEAT_JUMP_SIZES: dict[str, float] = dict.fromkeys(_BEAT_JUMP_DECK_KEYS, 4)
_WHEEL_MIN = 0.05
_WHEEL_MAX = 4.0
_LIBRARY_BROWSER_BOOL_KEYS: tuple[str, ...] = (
    "hide_broken_links",
    *tuple(_DEFAULT_LIBRARY_FILTER_BOOLS),
    "midi_enabled",
    "spotify_shrink_main_for_player",
    "library_sidebar_art",
)

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


def _path_for_app(app: FastAPI) -> Path:
    configured = getattr(app.state, "data_dir", None)
    root = Path(configured) if configured is not None else DATA_DIR
    return root / "state" / _FILENAME


def _path(request: Request) -> Path:
    return _path_for_app(request.app)


def read_auto_analyze_lanes(app: FastAPI) -> frozenset[AutoLane]:
    """Analyze-on-import lanes from ui-prefs (re-read every auto-drain tick)."""
    return lanes_from_prefs(_load(_path_for_app(app)))


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` via same-dir temp file and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


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
    return _parse_bool_prefs(raw, _TOPBAR_BOOL_DEFAULTS)


def _parse_deck_default_bool_prefs(raw: dict[str, Any]) -> dict[str, bool]:
    return _parse_bool_prefs(raw, _DECK_DEFAULT_BOOL_DEFAULTS)


def _parse_bool_prefs(raw: dict[str, Any], defaults: dict[str, bool]) -> dict[str, bool]:
    """Fill a group of boolean prefs: absent keys take their default, a
    present but non-boolean value is refused rather than coerced."""
    out = dict(defaults)
    for key in defaults:
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


def _parse_bool_pref(raw: dict[str, Any], key: str, default: bool) -> bool:
    if key not in raw:
        return default
    val = raw[key]
    if not isinstance(val, bool):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": f"{key} must be a boolean"},
        )
    return val


def _parse_perf_view(raw: dict[str, Any]) -> str:
    if "perf_view" not in raw:
        return _DEFAULT_PERF_VIEW
    value = raw["perf_view"]
    if value not in ("waveform", "list", "phase-scope", "phase-meter"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "perf_view must be waveform|list|phase-scope|phase-meter",
            },
        )
    return value


KeymapPreset = Literal["opendj", "rekordbox", "serato"]
_DEFAULT_KEYBOARD: dict[str, Any] = {"preset": "opendj", "overrides": {}}


def _parse_keyboard(raw: dict[str, Any]) -> dict[str, Any]:
    if "keyboard" not in raw:
        return dict(_DEFAULT_KEYBOARD)
    value = raw["keyboard"]
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "keyboard must be an object"},
        )
    allowed = frozenset({"preset", "overrides"})
    for key in value:
        if key not in allowed:
            raise HTTPException(
                status_code=422,
                detail={"code": "UI_PREFS_INVALID", "message": f"keyboard has unknown key: {key}"},
            )
    preset = value.get("preset", _DEFAULT_KEYBOARD["preset"])
    if preset not in get_args(KeymapPreset):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "keyboard.preset must be " + "|".join(get_args(KeymapPreset)),
            },
        )
    overrides = value.get("overrides", {})
    if not isinstance(overrides, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "keyboard.overrides must be an object"},
        )
    parsed_overrides: dict[str, list[str]] = {}
    for cmd_id, chords in overrides.items():
        if not isinstance(cmd_id, str) or not isinstance(chords, list) or not all(isinstance(c, str) for c in chords):
            raise HTTPException(
                status_code=422,
                detail={"code": "UI_PREFS_INVALID", "message": "keyboard.overrides values must be lists of strings"},
            )
        parsed_overrides[cmd_id] = chords
    return {"preset": preset, "overrides": parsed_overrides}


def _parse_speaker_auto_recover(raw: dict[str, Any]) -> str:
    if "speaker_auto_recover" not in raw:
        return _DEFAULT_SPEAKER_AUTO_RECOVER
    value = raw["speaker_auto_recover"]
    if value not in get_args(SpeakerAutoRecover):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "speaker_auto_recover must be " + "|".join(get_args(SpeakerAutoRecover)),
            },
        )
    return value


def _assert_lighting_enabled_requires_protocol(out: dict[str, Any]) -> None:
    if out["enabled"] and out["protocol"] == "off":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "lighting: protocol_required_when_enabled",
            },
        )


def _parse_lighting(raw: Any) -> dict[str, Any]:
    """Stored lighting output prefs (issue #11774): enable flag and wire protocol."""
    if raw is None:
        return dict(_DEFAULT_LIGHTING)
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "lighting must be an object"},
        )
    out = dict(_DEFAULT_LIGHTING)
    if "enabled" in raw:
        val = raw["enabled"]
        if not isinstance(val, bool):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": "lighting.enabled must be a boolean",
                },
            )
        out["enabled"] = val
    if "protocol" in raw:
        value = raw["protocol"]
        if value not in get_args(LightingProtocol):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": "lighting.protocol must be " + "|".join(get_args(LightingProtocol)),
                },
            )
        out["protocol"] = value
    if "clock_source" in raw:
        try:
            out["clock_source"] = parse_clock_source(raw["clock_source"])
        except ClockSourceError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "UI_PREFS_INVALID", "message": str(exc)},
            ) from exc
    _assert_lighting_enabled_requires_protocol(out)
    return out


def _parse_tempo_lane_scale(raw: dict[str, Any]) -> str:
    if "tempo_lane_scale" not in raw:
        return _DEFAULT_TEMPO_LANE_SCALE
    value = raw["tempo_lane_scale"]
    if value not in get_args(TempoLaneScale):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "tempo_lane_scale must be " + "|".join(get_args(TempoLaneScale)),
            },
        )
    return value


def _parse_wave_height(raw: dict[str, Any]) -> WaveHeight:
    if "wave_height" not in raw:
        return _DEFAULT_WAVE_HEIGHT
    value = raw["wave_height"]
    if value not in get_args(WaveHeight):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "wave_height must be " + "|".join(get_args(WaveHeight)),
            },
        )
    return value


def _parse_wave_grid_lines(raw: dict[str, Any]) -> str:
    if "wave_grid_lines" not in raw:
        return _DEFAULT_WAVE_GRID_LINES
    value = raw["wave_grid_lines"]
    if value not in get_args(WaveGridLines):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "wave_grid_lines must be " + "|".join(get_args(WaveGridLines)),
            },
        )
    return value


def _parse_sections_main_style(raw: dict[str, Any]) -> str:
    if "sections_main_style" not in raw:
        return _DEFAULT_SECTIONS_MAIN_STYLE
    value = raw["sections_main_style"]
    if value not in get_args(SectionsMainStyle):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "sections_main_style must be " + "|".join(get_args(SectionsMainStyle)),
            },
        )
    return value


def _parse_sections_overview_style(raw: dict[str, Any]) -> str:
    if "sections_overview_style" not in raw:
        return _DEFAULT_SECTIONS_OVERVIEW_STYLE
    value = raw["sections_overview_style"]
    if value not in get_args(SectionsOverviewStyle):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "sections_overview_style must be "
                + "|".join(get_args(SectionsOverviewStyle)),
            },
        )
    return value


def _parse_sections_uncertain(raw: dict[str, Any]) -> str:
    if "sections_uncertain" not in raw:
        return _DEFAULT_SECTIONS_UNCERTAIN
    value = raw["sections_uncertain"]
    if value not in get_args(SectionsUncertain):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "sections_uncertain must be " + "|".join(get_args(SectionsUncertain)),
            },
        )
    return value


def _parse_library_density(raw: dict[str, Any]) -> str:
    if "library_density" not in raw:
        return _DEFAULT_LIBRARY_DENSITY
    value = raw["library_density"]
    if value not in ("compact", "cosy"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "library_density must be compact|cosy",
            },
        )
    return value


def _parse_library_hidden_columns(raw: dict[str, Any]) -> list[str]:
    """Hidden library columns in table order; unknown ids and duplicates are refused."""
    if "library_hidden_columns" not in raw:
        return []
    value = raw["library_hidden_columns"]
    message = (
        "library_hidden_columns must be a list of distinct column ids from "
        + "|".join(_LIBRARY_HIDEABLE_COLUMNS)
    )
    # Types first: set() on an unhashable entry ({} or []) would raise
    # TypeError and turn a malformed file into a 500 (sol review P1, PR #7601).
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item in _LIBRARY_HIDEABLE_COLUMNS for item in value
    ):
        raise HTTPException(status_code=422, detail={"code": "UI_PREFS_INVALID", "message": message})
    if len(set(value)) != len(value):
        raise HTTPException(status_code=422, detail={"code": "UI_PREFS_INVALID", "message": message})
    return [col for col in _LIBRARY_HIDEABLE_COLUMNS if col in value]


def _parse_section_view_prefs(raw: dict[str, Any]) -> dict[str, Any]:
    within = raw.get("sort_within_sections", False)
    collapsed = raw.get("collapsed_sections", [])
    if not isinstance(within, bool):
        message = "sort_within_sections must be a boolean"
    elif not isinstance(collapsed, list) or not all(isinstance(s, str) for s in collapsed):
        message = "collapsed_sections must be a list of section ids"
    else:
        return {"sort_within_sections": within, "collapsed_sections": collapsed}
    raise HTTPException(status_code=422, detail={"code": "UI_PREFS_INVALID", "message": message})


def _parse_library_optional_columns(raw: dict[str, Any]) -> list[str]:
    """Optional library columns turned on, in table order; unknown ids are refused."""
    value = raw.get("library_optional_columns", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item in _LIBRARY_OPTIONAL_COLUMNS for item in value
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "library_optional_columns must be a list of column ids from "
                + "|".join(_LIBRARY_OPTIONAL_COLUMNS),
            },
        )
    return [col for col in _LIBRARY_OPTIONAL_COLUMNS if col in value]


def _parse_library_classical_layout(raw: dict[str, Any]) -> bool:
    value = raw.get("library_classical_layout", False)
    if not isinstance(value, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "library_classical_layout must be a boolean",
            },
        )
    return value


def _merge_library_enum_prefs(current: dict[str, Any], body: UiPrefsPatch) -> None:
    if body.library_density is not None:
        current["library_density"] = body.library_density
    if body.sort_within_sections is not None:
        current["sort_within_sections"] = body.sort_within_sections
    if body.collapsed_sections is not None:
        current["collapsed_sections"] = list(body.collapsed_sections)
    if body.key_notation is not None:
        current["key_notation"] = body.key_notation
    if body.library_hidden_columns is not None:
        current["library_hidden_columns"] = _parse_library_hidden_columns(
            {"library_hidden_columns": list(body.library_hidden_columns)}
        )
    if body.library_optional_columns is not None:
        current["library_optional_columns"] = _parse_library_optional_columns(
            {"library_optional_columns": list(body.library_optional_columns)}
        )
    if body.library_classical_layout is not None:
        current["library_classical_layout"] = body.library_classical_layout


def _parse_key_notation(raw: dict[str, Any]) -> str:
    if "key_notation" not in raw:
        return _DEFAULT_KEY_NOTATION
    value = raw["key_notation"]
    if value not in _KEY_NOTATIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "key_notation must be camelot_musical|camelot|open_key|musical",
            },
        )
    return value


def _library_browser_bool_defaults() -> dict[str, bool]:
    return {
        "hide_broken_links": _DEFAULT_HIDE_BROKEN_LINKS,
        **_DEFAULT_LIBRARY_FILTER_BOOLS,
        "midi_enabled": _DEFAULT_MIDI_ENABLED,
        "spotify_shrink_main_for_player": _DEFAULT_SPOTIFY_SHRINK_MAIN,
        "library_sidebar_art": _DEFAULT_LIBRARY_SIDEBAR_ART,
    }


def _parse_library_browser_bool_prefs(raw: dict[str, Any]) -> dict[str, bool]:
    out = _library_browser_bool_defaults()
    for key in out:
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


def _parse_library_list_wheel_factor(key: str, val: Any) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": f"wheel_sensitivity.{key} must be a number",
            },
        )
    factor = float(val)
    if not _LIBRARY_WHEEL_MIN < factor <= _LIBRARY_WHEEL_MAX:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": (
                    f"wheel_sensitivity.{key} must be between {_LIBRARY_WHEEL_MIN} "
                    f"and {_LIBRARY_WHEEL_MAX}, got {val}"
                ),
            },
        )
    return factor


def _parse_wheel_factor(key: str, val: Any) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": f"wheel_sensitivity.{key} must be a number",
            },
        )
    factor = float(val)
    if not _WHEEL_MIN < factor <= _WHEEL_MAX:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": (
                    f"wheel_sensitivity.{key} must be between {_WHEEL_MIN} "
                    f"and {_WHEEL_MAX}, got {val}"
                ),
            },
        )
    return factor


def _parse_wheel_sensitivity(raw: Any) -> dict[str, float]:
    if raw is None:
        return dict(_DEFAULT_WHEEL_SENSITIVITY)
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "wheel_sensitivity must be an object"},
        )
    out = dict(_DEFAULT_WHEEL_SENSITIVITY)
    for key in ("mouse", "trackpad"):
        if key not in raw:
            continue
        out[key] = _parse_wheel_factor(key, raw[key])
    for key in ("library_mouse", "library_trackpad"):
        if key not in raw:
            continue
        out[key] = _parse_library_list_wheel_factor(key, raw[key])
    return out


def _beat_jump_sizes_invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "UI_PREFS_INVALID", "message": message})


def _parse_beat_jump_sizes(raw: Any) -> dict[str, float]:
    if raw is None:
        return dict(_DEFAULT_BEAT_JUMP_SIZES)
    if not isinstance(raw, dict):
        raise _beat_jump_sizes_invalid("beat_jump_sizes must be an object")
    out = dict(_DEFAULT_BEAT_JUMP_SIZES)
    for key, val in raw.items():
        if key not in _BEAT_JUMP_DECK_KEYS:
            raise _beat_jump_sizes_invalid(f"beat_jump_sizes has unknown key {key!r}")
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val not in _BEAT_JUMP_SIZE_CHOICES:
            raise _beat_jump_sizes_invalid(
                f"beat_jump_sizes.{key} must be one of 0.5|1|2|4|8|16|32|64, got {val!r}"
            )
        out[key] = val
    return out


def _merge_beat_jump_sizes(current: dict[str, Any], body: UiPrefsPatch) -> None:
    """A PATCH naming some decks keeps the others' sizes (DECKUX-10470)."""
    if body.beat_jump_sizes is not None:
        current["beat_jump_sizes"] = _parse_beat_jump_sizes(
            {**current["beat_jump_sizes"], **body.beat_jump_sizes.model_dump(exclude_unset=True)}
        )


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
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_app_mode_id(value: Any) -> str:
    if value is None:
        return _DEFAULT_APP_MODE["id"]
    if not isinstance(value, str):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "app_mode.id must be a string",
            },
        )
    if value not in ("performance", "library-management", "library", "music-player"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": (
                    "app_mode.id must be performance|library-management|library|music-player"
                ),
            },
        )
    return value


def _parse_app_mode(raw: Any) -> dict[str, Any]:
    if raw is None:
        return dict(_DEFAULT_APP_MODE)
    if isinstance(raw, str):
        return {**dict(_DEFAULT_APP_MODE), "id": _parse_app_mode_id(raw)}
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "app_mode must be an object"},
        )
    out = dict(_DEFAULT_APP_MODE)
    if "last_gig_at" in raw:
        out["last_gig_at"] = _parse_last_gig_at(raw["last_gig_at"])
    if "id" in raw:
        out["id"] = _parse_app_mode_id(raw["id"])
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


def _parse_library_watcher_folders(raw: dict[str, Any]) -> list[str]:
    if "library_watcher_folders" not in raw:
        return []
    value = raw["library_watcher_folders"]
    if not isinstance(value, list):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "library_watcher_folders must be a list of strings",
            },
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": "library_watcher_folders must be a list of strings",
                },
            )
        out.append(item)
    return out


_MIDI_SETUP_DISMISSED_MESSAGE = (
    "midi_setup_dismissed_ports must be a list of non-empty port-name strings"
)


def _parse_midi_setup_dismissed_ports(raw: dict[str, Any]) -> list[str]:
    if "midi_setup_dismissed_ports" not in raw:
        return []
    value = raw["midi_setup_dismissed_ports"]
    if not isinstance(value, list):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": _MIDI_SETUP_DISMISSED_MESSAGE},
        )
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise HTTPException(
                status_code=422,
                detail={"code": "UI_PREFS_INVALID", "message": _MIDI_SETUP_DISMISSED_MESSAGE},
            )
        if item.strip() == "":
            raise HTTPException(
                status_code=422,
                detail={"code": "UI_PREFS_INVALID", "message": _MIDI_SETUP_DISMISSED_MESSAGE},
            )
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


_LIBRARY_KEY_WHEEL_ORDER: tuple[str, ...] = tuple(
    f"{number}{mode}" for number in range(1, 13) for mode in ("B", "A")
)
_LIBRARY_KEY_FILTER_CODE_RE = re.compile(r"^(?:1[0-2]|[1-9])[AB]$")


def _parse_library_key_filter(raw: dict[str, Any]) -> list[str]:
    if "library_key_filter" not in raw:
        return []
    value = raw["library_key_filter"]
    if not isinstance(value, list):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "library_key_filter must be a list of canonical Camelot codes",
            },
        )
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or _LIBRARY_KEY_FILTER_CODE_RE.fullmatch(item) is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": "library_key_filter entries must be canonical 1A..12B codes",
                },
            )
        if item in seen:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": "library_key_filter must not contain duplicates",
                },
            )
        seen.add(item)
    if len(seen) > 24:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "library_key_filter must contain at most 24 codes",
            },
        )
    return [code for code in _LIBRARY_KEY_WHEEL_ORDER if code in seen]


# LIBUX-32 compatible filter ranges. Mirrors COMPATIBLE_FILTER_DEFAULTS and
# validateCompatibleFilterPrefs in apps/webui/frontend/src/lib/rb/compatible-filter-prefs.ts.
_DEFAULT_COMPATIBLE_FILTER: dict[str, Any] = {
    "rules": list(DEFAULT_CAMELOT_RULE_IDS),
    "bpm_window_bpm": 20.0,
    "bpm_enabled": True,
    "allow_half_double": True,
    "bpm_direction": "both",
    # KEYSHIFT-01..05 (the maintainer, Tue 13 Oct 2026): Transition / Mashup modes and the
    # allowed key shift. Transition with no shift is the filter as it was; its
    # key rule is `rules` above (the relative major/minor is the "relative" rule).
    "mode": "transition",
    "key_shift_max": 1,
    "mashup_relative": False,
    # SETPREP-11947: the energy rule (within one MIK level, boost +1 to +2,
    # drop -1 to -2), after Mixed In Key's Energy Boost (mimicked). Off by default.
    "energy_mode": "off",
}


def _is_bpm_window(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _is_camelot_rule_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item in CAMELOT_RULE_IDS for item in value)
        and len(set(value)) == len(value)
    )


# The compatible filter's Camelot steps (0/1/2, letter-blind) became rule
# checkboxes (Tue 27 Oct 2026). A stored blob from before that carries
# camelot_steps and no rules; it reads as the nearest rule set, once.
_LEGACY_CAMELOT_STEPS_RULES: dict[int, tuple[str, ...]] = {
    0: ("same_key", "relative"),
    1: DEFAULT_CAMELOT_RULE_IDS,
    2: (*DEFAULT_CAMELOT_RULE_IDS, "energy_boost", "energy_drop"),
}


_COMPATIBLE_FILTER_CHECKS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "rules": (
        _is_camelot_rule_list,
        f"must be a list of distinct rule ids from {'|'.join(CAMELOT_RULE_IDS)}",
    ),
    "bpm_window_bpm": (_is_bpm_window, "must be a finite number >= 0"),
    "bpm_enabled": (lambda v: isinstance(v, bool), "must be a boolean"),
    "allow_half_double": (lambda v: isinstance(v, bool), "must be a boolean"),
    "bpm_direction": (
        lambda v: v in ("both", "above", "below", "same"),
        "must be both|above|below|same",
    ),
    "mode": (lambda v: v in ("transition", "mashup"), "must be transition|mashup"),
    "key_shift_max": (
        # type() is int, not isinstance: bool is an int, and 1.0 == 1 would pass a `v in` check.
        lambda v: type(v) is int and v in (0, 1, 2, 3),
        "must be the integer 0, 1, 2 or 3",
    ),
    "mashup_relative": (lambda v: isinstance(v, bool), "must be a boolean"),
    "energy_mode": (
        lambda v: isinstance(v, str) and v in ("off", "within_1", "boost", "drop"),
        "must be off|within_1|boost|drop",
    ),
}


def _reject_invalid_compatible_filter_rules(rules: list[str]) -> None:
    """Refuse unknown or duplicate rule ids with the same message shape as ``_parse_compatible_filter``."""
    _, rule = _COMPATIBLE_FILTER_CHECKS["rules"]
    if not _is_camelot_rule_list(rules):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": f"compatible_filter.rules {rule}",
            },
        )


def _validate_compatible_filter_rules(value: list[str]) -> list[str]:
    _, rule = _COMPATIBLE_FILTER_CHECKS["rules"]
    if not _is_camelot_rule_list(value):
        raise ValueError(rule)
    return value


def _legacy_camelot_steps_rules(steps: Any) -> tuple[str, ...]:
    """The rule set a stored camelot_steps of 0, 1 or 2 reads as; anything else is a 422."""
    if isinstance(steps, bool) or not isinstance(steps, int) or steps not in _LEGACY_CAMELOT_STEPS_RULES:
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "compatible_filter.camelot_steps must be 0, 1 or 2"},
        )
    return _LEGACY_CAMELOT_STEPS_RULES[steps]


def _parse_compatible_filter(raw: Any) -> dict[str, Any]:
    """Stored compatible-filter ranges over the defaults; a wrong type refuses (422)."""
    if raw is None:
        return dict(_DEFAULT_COMPATIBLE_FILTER)
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "UI_PREFS_INVALID", "message": "compatible_filter must be an object"},
        )
    out = dict(_DEFAULT_COMPATIBLE_FILTER)
    out["rules"] = list(DEFAULT_CAMELOT_RULE_IDS)
    if "rules" not in raw and "camelot_steps" in raw:
        out["rules"] = list(_legacy_camelot_steps_rules(raw["camelot_steps"]))
    for key, (check, rule) in _COMPATIBLE_FILTER_CHECKS.items():
        if key not in raw:
            continue
        value = raw[key]
        if not check(value):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UI_PREFS_INVALID",
                    "message": f"compatible_filter.{key} {rule}",
                },
            )
        if key == "bpm_window_bpm":
            out[key] = float(value)
        elif key == "rules":
            out[key] = list(value)
        else:
            out[key] = value
    return out


def _parse_gig_helper(raw: dict[str, Any]) -> str:
    if "gig_helper" not in raw:
        return _DEFAULT_GIG_HELPER
    value = raw["gig_helper"]
    if value not in ("unset", "off", "on"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "gig_helper must be unset|off|on",
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
            "show_stems": _DEFAULT_SHOW_STEMS,
            "wave_stem_follow": _DEFAULT_WAVE_STEM_FOLLOW,
            "reduce_motion": _DEFAULT_REDUCE_MOTION,
            "deck_hover_link": _DEFAULT_DECK_HOVER_LINK,
            "compact_decks": _DEFAULT_COMPACT_DECKS,
            "level_calibration": dict(_DEFAULT_LEVEL_CALIBRATION),
            "auto_gain": dict(DEFAULT_AUTO_GAIN),
            "perf_tier": _DEFAULT_PERF_TIER,
            "app_posture": _DEFAULT_APP_POSTURE,
            "gig_helper": _DEFAULT_GIG_HELPER,
            "app_mode": dict(_DEFAULT_APP_MODE),
            **_TOPBAR_BOOL_DEFAULTS,
            **_DECK_DEFAULT_BOOL_DEFAULTS,
            **_WAVE_OVERLAY_BOOL_DEFAULTS,
            **AUTO_ANALYZE_BOOL_DEFAULTS,
            **_lyrics_defaults(),
            **_library_browser_bool_defaults(),
            "library_density": _DEFAULT_LIBRARY_DENSITY,
            "sort_within_sections": False,
            "collapsed_sections": [],
            "perf_view": _DEFAULT_PERF_VIEW,
            "speaker_auto_recover": _DEFAULT_SPEAKER_AUTO_RECOVER,
            "keyboard": dict(_DEFAULT_KEYBOARD),
            "tempo_lane_scale": _DEFAULT_TEMPO_LANE_SCALE,
            "wave_height": _DEFAULT_WAVE_HEIGHT,
            "wave_grid_lines": _DEFAULT_WAVE_GRID_LINES,
            "sections_main_style": _DEFAULT_SECTIONS_MAIN_STYLE,
            "sections_sticky_title": _DEFAULT_SECTIONS_STICKY_TITLE,
            "sections_next_countdown": _DEFAULT_SECTIONS_NEXT_COUNTDOWN,
            "sections_overview_style": _DEFAULT_SECTIONS_OVERVIEW_STYLE,
            "sections_library_structure_column": _DEFAULT_SECTIONS_LIBRARY_STRUCTURE_COLUMN,
            "sections_preview_cursor_label": _DEFAULT_SECTIONS_PREVIEW_CURSOR_LABEL,
            "sections_uncertain": _DEFAULT_SECTIONS_UNCERTAIN,
            "key_notation": _DEFAULT_KEY_NOTATION,
            "library_hidden_columns": list(_DEFAULT_LIBRARY_HIDDEN_COLUMNS),
            "library_optional_columns": [],
            "library_classical_layout": False,
            "wheel_sensitivity": dict(_DEFAULT_WHEEL_SENSITIVITY),
            "beat_jump_sizes": dict(_DEFAULT_BEAT_JUMP_SIZES),
            "library_watcher_folders": [],
            "midi_setup_dismissed_ports": [],
            "library_key_filter": [],
            "compatible_filter": dict(_DEFAULT_COMPATIBLE_FILTER),
            "lighting": dict(_DEFAULT_LIGHTING),
            "library_tabs": parse_library_tabs(None),
            "library_related_criteria": parse_library_related_criteria(None),
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
    show_stems = raw.get("show_stems", _DEFAULT_SHOW_STEMS)
    if not isinstance(show_stems, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "show_stems must be a boolean",
            },
        )
    wave_stem_follow = raw.get("wave_stem_follow", _DEFAULT_WAVE_STEM_FOLLOW)
    if not isinstance(wave_stem_follow, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "wave_stem_follow must be a boolean",
            },
        )
    reduce_motion = raw.get("reduce_motion", _DEFAULT_REDUCE_MOTION)
    if not isinstance(reduce_motion, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "reduce_motion must be a boolean",
            },
        )
    deck_hover_link = raw.get("deck_hover_link", _DEFAULT_DECK_HOVER_LINK)
    if not isinstance(deck_hover_link, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "deck_hover_link must be a boolean",
            },
        )
    compact_decks = raw.get("compact_decks", _DEFAULT_COMPACT_DECKS)
    if not isinstance(compact_decks, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UI_PREFS_INVALID",
                "message": "compact_decks must be a boolean",
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
        "show_stems": show_stems,
        "wave_stem_follow": wave_stem_follow,
        "reduce_motion": reduce_motion,
        "deck_hover_link": deck_hover_link,
        "compact_decks": compact_decks,
        "level_calibration": _parse_level_calibration(raw.get("level_calibration")),
        "auto_gain": parse_auto_gain(raw.get("auto_gain")),
        "perf_tier": _parse_perf_tier(raw),
        "app_posture": _parse_app_posture(raw),
        "gig_helper": _parse_gig_helper(raw),
        "app_mode": _parse_app_mode(raw.get("app_mode")),
        **_parse_topbar_bool_prefs(raw),
        **_parse_deck_default_bool_prefs(raw),
        **_parse_bool_prefs(raw, _WAVE_OVERLAY_BOOL_DEFAULTS),
        **_parse_bool_prefs(raw, AUTO_ANALYZE_BOOL_DEFAULTS),
        **_parse_lyrics(raw),
        **_parse_library_browser_bool_prefs(raw),
        "library_density": _parse_library_density(raw),
        **_parse_section_view_prefs(raw),
        "perf_view": _parse_perf_view(raw),
        # DECKUX-8360: read back what PUT wrote, or every boot's GET resets the mirror.
        **_parse_bool_prefs(raw, {"deck_right_mirror": _DEFAULT_DECK_RIGHT_MIRROR}),
        "speaker_auto_recover": _parse_speaker_auto_recover(raw),
        "keyboard": _parse_keyboard(raw),
        "tempo_lane_scale": _parse_tempo_lane_scale(raw),
        "wave_height": _parse_wave_height(raw),
        "wave_grid_lines": _parse_wave_grid_lines(raw),
        "sections_main_style": _parse_sections_main_style(raw),
        "sections_sticky_title": _parse_bool_pref(
            raw, "sections_sticky_title", _DEFAULT_SECTIONS_STICKY_TITLE
        ),
        "sections_next_countdown": _parse_bool_pref(
            raw, "sections_next_countdown", _DEFAULT_SECTIONS_NEXT_COUNTDOWN
        ),
        "sections_overview_style": _parse_sections_overview_style(raw),
        "sections_library_structure_column": _parse_bool_pref(
            raw, "sections_library_structure_column", _DEFAULT_SECTIONS_LIBRARY_STRUCTURE_COLUMN
        ),
        "sections_preview_cursor_label": _parse_bool_pref(
            raw, "sections_preview_cursor_label", _DEFAULT_SECTIONS_PREVIEW_CURSOR_LABEL
        ),
        "sections_uncertain": _parse_sections_uncertain(raw),
        "key_notation": _parse_key_notation(raw),
        "library_hidden_columns": _parse_library_hidden_columns(raw),
        "library_optional_columns": _parse_library_optional_columns(raw),
        "library_classical_layout": _parse_library_classical_layout(raw),
        "wheel_sensitivity": _parse_wheel_sensitivity(raw.get("wheel_sensitivity")),
        "beat_jump_sizes": _parse_beat_jump_sizes(raw.get("beat_jump_sizes")),
        "library_watcher_folders": _parse_library_watcher_folders(raw),
        "midi_setup_dismissed_ports": _parse_midi_setup_dismissed_ports(raw),
        "library_key_filter": _parse_library_key_filter(raw),
        "compatible_filter": _parse_compatible_filter(raw.get("compatible_filter")),
        "lighting": _parse_lighting(raw.get("lighting")),
        "library_tabs": parse_library_tabs(raw.get("library_tabs")),
        "library_related_criteria": parse_library_related_criteria(raw.get("library_related_criteria")),
    }


class KeyboardPrefsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    preset: KeymapPreset = "opendj"
    overrides: dict[str, list[str]] = Field(default_factory=dict)


class KeyboardPrefsPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    preset: KeymapPreset | None = None
    overrides: dict[str, list[str]] | None = None


class AutoSyncOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    rekordbox: bool = False
    djay: bool = False
    open_dj: bool = False


class AppModeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    last_gig_at: str | None = None
    id: AppModePref = "performance"


class WheelSensitivityOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    mouse: float = _DEFAULT_WHEEL_SENSITIVITY["mouse"]
    trackpad: float = _DEFAULT_WHEEL_SENSITIVITY["trackpad"]
    library_mouse: float = _DEFAULT_WHEEL_SENSITIVITY["library_mouse"]
    library_trackpad: float = _DEFAULT_WHEEL_SENSITIVITY["library_trackpad"]


class BeatJumpSizesOut(BaseModel):
    """Per-deck beat jump size in beats (DECKUX-10470): 0.5|1|2|4|8|16|32|64."""

    model_config = ConfigDict(frozen=True)

    # Strict: a JSON `true` must not coerce to the legal size 1.0.
    deck_1: StrictFloat = 4
    deck_2: StrictFloat = 4
    deck_3: StrictFloat = 4
    deck_4: StrictFloat = 4


class LightingOut(BaseModel):
    """Lighting prefs on ui-prefs: output wire (#11774) and beat clock source (LIGHT-12848)."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    protocol: LightingProtocol = "off"
    clock_source: StrictStr = "master"

    @model_serializer(mode="plain")
    def _wire_minimal_when_output_off(self) -> dict[str, Any]:
        out: dict[str, Any] = {"clock_source": self.clock_source}
        if self.enabled or self.protocol != "off":
            out["enabled"] = self.enabled
            out["protocol"] = self.protocol
        return out


class LightingPatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool | None = None
    protocol: LightingProtocol | None = None
    clock_source: StrictStr | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def refuse_null_clock_source(self) -> LightingPatch:
        if "clock_source" in self.model_fields_set and self.clock_source is None:
            raise ValueError("clock_source must not be null")
        return self


class CompatibleFilterOut(BaseModel):
    """Compatible-filter ranges (LIBUX-32): Camelot rule checkboxes plus the BPM window,
    and the Transition / Mashup modes with the allowed key shift (KEYSHIFT-01..05).

    ``rules`` lists the enabled ids from apps/shared/harmonic.py CAMELOT_RULES and is
    Transition's key rule; Mashup is the same wheel number (plus the relative
    major/minor when ``mashup_relative``). ``energy_mode`` is the energy rule
    (SETPREP-11947): off, within one level, boost (+1 to +2) or drop (-1 to -2).
    """

    model_config = ConfigDict(frozen=True)

    rules: list[str] = Field(default_factory=lambda: list(DEFAULT_CAMELOT_RULE_IDS))
    bpm_window_bpm: float = Field(default=_DEFAULT_COMPATIBLE_FILTER["bpm_window_bpm"], ge=0)
    bpm_enabled: bool = _DEFAULT_COMPATIBLE_FILTER["bpm_enabled"]
    allow_half_double: bool = _DEFAULT_COMPATIBLE_FILTER["allow_half_double"]
    bpm_direction: CompatibleBpmDirection = _DEFAULT_COMPATIBLE_FILTER["bpm_direction"]
    mode: Literal["transition", "mashup"] = _DEFAULT_COMPATIBLE_FILTER["mode"]
    key_shift_max: Literal[0, 1, 2, 3] = _DEFAULT_COMPATIBLE_FILTER["key_shift_max"]
    mashup_relative: bool = _DEFAULT_COMPATIBLE_FILTER["mashup_relative"]
    energy_mode: Literal["off", "within_1", "boost", "drop"] = _DEFAULT_COMPATIBLE_FILTER["energy_mode"]

    @field_validator("key_shift_max", mode="before")
    @classmethod
    def _key_shift_max_is_an_integer(cls, value: Any) -> Any:
        # Literal[0, 1, 2, 3] alone accepts 1.0 and True, which compare equal to 1.
        if type(value) is not int:
            raise ValueError("key_shift_max must be the integer 0, 1, 2 or 3")
        return value

    @field_validator("rules")
    @classmethod
    def _rules_are_distinct_camelot_ids(cls, value: list[str]) -> list[str]:
        return _validate_compatible_filter_rules(value)


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
    show_stems: bool = _DEFAULT_SHOW_STEMS
    wave_stem_follow: bool = _DEFAULT_WAVE_STEM_FOLLOW
    reduce_motion: bool = _DEFAULT_REDUCE_MOTION
    deck_hover_link: bool = _DEFAULT_DECK_HOVER_LINK
    compact_decks: bool = _DEFAULT_COMPACT_DECKS
    level_calibration: LevelCalibrationOut = Field(default_factory=LevelCalibrationOut)
    auto_gain: AutoGainPrefsOut = Field(default_factory=AutoGainPrefsOut)
    lyrics_global: bool = _DEFAULT_LYRICS_BOOLS["lyrics_global"]
    lyrics_library_col: bool = _DEFAULT_LYRICS_BOOLS["lyrics_library_col"]
    lyrics_hover_scrub: bool = _DEFAULT_LYRICS_BOOLS["lyrics_hover_scrub"]
    lyrics_load_strategy: LyricsLoadStrategy = _DEFAULT_LYRICS_LOAD_STRATEGY
    lyrics_waveform_overlay: bool = _DEFAULT_LYRICS_BOOLS["lyrics_waveform_overlay"]
    lyrics_deck_line: bool = _DEFAULT_LYRICS_BOOLS["lyrics_deck_line"]
    perf_tier: PerfTierPref = _DEFAULT_PERF_TIER
    app_posture: AppPosturePref = _DEFAULT_APP_POSTURE
    gig_helper: GigHelperPref = _DEFAULT_GIG_HELPER
    app_mode: AppModeOut = Field(default_factory=AppModeOut)
    beat_sync_max: bool = _DEFAULT_BEAT_SYNC_MAX
    auto_play_enabled: bool = _DEFAULT_AUTO_PLAY_ENABLED
    auto_play_enforce_order: bool = _DEFAULT_AUTO_PLAY_ENFORCE_ORDER
    auto_play_maximize_reach: bool = _DEFAULT_AUTO_PLAY_MAXIMIZE_REACH
    master_muted: bool = _DEFAULT_MASTER_MUTED
    show_pads: bool = _DEFAULT_SHOW_PADS
    auto_play_two_deck: bool = _DEFAULT_AUTO_PLAY_TWO_DECK
    perf_view: PerfView = _DEFAULT_PERF_VIEW
    deck_default_quantize: bool = _DEFAULT_DECK_DEFAULT_QUANTIZE
    deck_needle_lock: bool = _DEFAULT_DECK_NEEDLE_LOCK
    show_sections: bool = _DEFAULT_SHOW_SECTIONS
    sections_main_style: SectionsMainStyle = _DEFAULT_SECTIONS_MAIN_STYLE
    sections_sticky_title: bool = _DEFAULT_SECTIONS_STICKY_TITLE
    sections_next_countdown: bool = _DEFAULT_SECTIONS_NEXT_COUNTDOWN
    sections_overview_style: SectionsOverviewStyle = _DEFAULT_SECTIONS_OVERVIEW_STYLE
    sections_library_structure_column: bool = _DEFAULT_SECTIONS_LIBRARY_STRUCTURE_COLUMN
    sections_preview_cursor_label: bool = _DEFAULT_SECTIONS_PREVIEW_CURSOR_LABEL
    sections_uncertain: SectionsUncertain = _DEFAULT_SECTIONS_UNCERTAIN
    auto_analyze_grid: bool = AUTO_ANALYZE_BOOL_DEFAULTS["auto_analyze_grid"]
    auto_analyze_key: bool = AUTO_ANALYZE_BOOL_DEFAULTS["auto_analyze_key"]
    auto_analyze_sections: bool = AUTO_ANALYZE_BOOL_DEFAULTS["auto_analyze_sections"]
    auto_analyze_vocals: bool = AUTO_ANALYZE_BOOL_DEFAULTS["auto_analyze_vocals"]
    speaker_auto_recover: SpeakerAutoRecover = _DEFAULT_SPEAKER_AUTO_RECOVER
    keyboard: KeyboardPrefsOut = Field(default_factory=KeyboardPrefsOut)
    show_tempo_lane: bool = _DEFAULT_SHOW_TEMPO_LANE
    show_vocal_lane: bool = _DEFAULT_SHOW_VOCAL_LANE
    tempo_lane_scale: TempoLaneScale = _DEFAULT_TEMPO_LANE_SCALE
    wave_height: WaveHeight = _DEFAULT_WAVE_HEIGHT
    wave_grid_lines: WaveGridLines = _DEFAULT_WAVE_GRID_LINES
    show_hot_cue_markers: bool = _WAVE_OVERLAY_BOOL_DEFAULTS["show_hot_cue_markers"]
    show_memory_cue_markers: bool = _WAVE_OVERLAY_BOOL_DEFAULTS["show_memory_cue_markers"]
    show_loop_markers: bool = _WAVE_OVERLAY_BOOL_DEFAULTS["show_loop_markers"]
    deck_autocue: bool = _DEFAULT_DECK_AUTOCUE
    hide_broken_links: bool = _DEFAULT_HIDE_BROKEN_LINKS
    library_density: LibraryDensity = _DEFAULT_LIBRARY_DENSITY
    sort_within_sections: bool = False
    collapsed_sections: list[str] = Field(default_factory=list)
    key_notation: KeyNotation = _DEFAULT_KEY_NOTATION
    library_hidden_columns: list[LibraryHideableColumn] = Field(default_factory=list)
    library_optional_columns: list[LibraryOptionalColumn] = Field(default_factory=list)
    library_classical_layout: bool = False
    next_only_filter: bool = _DEFAULT_LIBRARY_FILTER_BOOLS["next_only_filter"]
    remixes_filter: bool = _DEFAULT_LIBRARY_FILTER_BOOLS["remixes_filter"]
    vocals_filter: bool = _DEFAULT_LIBRARY_FILTER_BOOLS["vocals_filter"]
    available_offline_filter: bool = _DEFAULT_LIBRARY_FILTER_BOOLS["available_offline_filter"]
    wheel_sensitivity: WheelSensitivityOut = Field(default_factory=WheelSensitivityOut)
    beat_jump_sizes: BeatJumpSizesOut = Field(default_factory=BeatJumpSizesOut)
    midi_enabled: bool = _DEFAULT_MIDI_ENABLED
    spotify_shrink_main_for_player: bool = _DEFAULT_SPOTIFY_SHRINK_MAIN
    library_sidebar_art: bool = _DEFAULT_LIBRARY_SIDEBAR_ART
    deck_right_mirror: bool = _DEFAULT_DECK_RIGHT_MIRROR
    playlist_tree_view: PlaylistTreeView = _DEFAULT_PLAYLIST_TREE_VIEW
    library_watcher_folders: list[str] = Field(default_factory=list)
    # CTRL first-run prompt (#10076): port names the DJ dismissed or finished setting up.
    midi_setup_dismissed_ports: list[str] = Field(default_factory=list)
    library_key_filter: list[str] = Field(default_factory=list)
    compatible_filter: CompatibleFilterOut = Field(default_factory=CompatibleFilterOut)
    lighting: LightingOut = Field(default_factory=LightingOut)
    library_tabs: LibraryTabsOut = Field(default_factory=LibraryTabsOut)
    library_related_criteria: LibraryRelatedCriteriaOut = Field(default_factory=LibraryRelatedCriteriaOut)


class UiPrefsPatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirm: dict[str, Any] | None = None
    theme: UiTheme | None = None
    hide_todo_settings: bool | None = None
    auto_sync: AutoSyncOut | None = None
    technically_working_animate: bool | None = None
    jog_radial_waveform: bool | None = None
    show_agent_pins: bool | None = None
    show_stems: bool | None = None
    wave_stem_follow: bool | None = None
    reduce_motion: StrictBool | SkipJsonSchema[None] = None
    deck_hover_link: bool | None = None
    compact_decks: bool | None = None
    level_calibration: LevelCalibrationOut | None = None
    auto_gain: AutoGainPrefsOut | None = None
    lyrics_global: bool | None = None
    lyrics_library_col: bool | None = None
    lyrics_hover_scrub: bool | None = None
    lyrics_load_strategy: LyricsLoadStrategy | None = None
    lyrics_waveform_overlay: bool | None = None
    lyrics_deck_line: bool | None = None
    perf_tier: PerfTierPref | None = None
    app_posture: AppPosturePref | None = None
    gig_helper: GigHelperPref | None = None
    app_mode: AppModeOut | None = None
    beat_sync_max: bool | None = None
    auto_play_enabled: bool | None = None
    auto_play_enforce_order: bool | None = None
    auto_play_maximize_reach: bool | None = None
    master_muted: bool | None = None
    # StrictBool (Sol P1 4285228879): plain bool would coerce "false", 0 and 1
    # into a write; these toggles take a JSON boolean or a 422.
    # SkipJsonSchema[None] (Sol P1 4302581642): None is only "not sent" here,
    # and `_refuse_null_view_prefs` answers an explicit null with 422, so the
    # contract advertises these as omittable but never nullable.
    show_pads: StrictBool | SkipJsonSchema[None] = None
    auto_play_two_deck: StrictBool | SkipJsonSchema[None] = None
    perf_view: PerfView | SkipJsonSchema[None] = None
    # Strict: lax bool would coerce "yes" or 1 to True and seed every deck from it.
    deck_default_quantize: StrictBool | None = None
    deck_needle_lock: StrictBool | None = None
    show_sections: bool | None = None
    sections_main_style: SectionsMainStyle | None = None
    sections_sticky_title: StrictBool | None = None
    sections_next_countdown: StrictBool | None = None
    sections_overview_style: SectionsOverviewStyle | None = None
    sections_library_structure_column: StrictBool | None = None
    sections_preview_cursor_label: StrictBool | None = None
    sections_uncertain: SectionsUncertain | None = None
    auto_analyze_grid: StrictBool | None = None
    auto_analyze_key: StrictBool | None = None
    auto_analyze_sections: StrictBool | None = None
    auto_analyze_vocals: StrictBool | None = None
    speaker_auto_recover: SpeakerAutoRecover | None = None
    keyboard: KeyboardPrefsPatch | None = None
    # Strict: lax bool would coerce "yes" or 1 into a write that hides the lane.
    show_tempo_lane: StrictBool | None = None
    # Strict: lax bool would coerce "yes" or 1 into a write that hides the vocal bars.
    show_vocal_lane: StrictBool | None = None
    tempo_lane_scale: TempoLaneScale | None = None
    wave_height: WaveHeight | None = None
    wave_grid_lines: WaveGridLines | None = None
    # Strict: lax bool would coerce "yes" or 1 into hiding cue markers.
    show_hot_cue_markers: StrictBool | None = None
    show_memory_cue_markers: StrictBool | None = None
    show_loop_markers: StrictBool | None = None
    # Strict for the same reason: AU is deck-wide, a coerced 1 or "yes" would arm it.
    # Omitted means unchanged; an explicit null is refused (Sol P1, PR #6235),
    # so the contract advertises it as omittable but never nullable.
    deck_autocue: StrictBool | SkipJsonSchema[None] = None
    hide_broken_links: bool | None = None
    library_density: LibraryDensity | None = None
    sort_within_sections: bool | None = None
    collapsed_sections: list[str] | None = None
    key_notation: KeyNotation | None = None
    library_optional_columns: list[LibraryOptionalColumn] | None = None
    library_classical_layout: StrictBool | None = None
    library_hidden_columns: list[LibraryHideableColumn] | None = None
    next_only_filter: bool | None = None
    remixes_filter: bool | None = None
    vocals_filter: bool | None = None
    available_offline_filter: bool | None = None
    wheel_sensitivity: WheelSensitivityOut | None = None
    beat_jump_sizes: BeatJumpSizesOut | None = None
    midi_enabled: bool | None = None
    spotify_shrink_main_for_player: bool | None = None
    library_sidebar_art: bool | None = None
    deck_right_mirror: bool | None = None
    playlist_tree_view: PlaylistTreeView | None = None
    library_watcher_folders: list[str] | None = None
    midi_setup_dismissed_ports: list[StrictStr] | SkipJsonSchema[None] = None
    library_key_filter: list[str] | None = None
    compatible_filter: CompatibleFilterOut | None = None
    lighting: LightingPatch | None = None
    library_tabs: LibraryTabsOut | None = None
    library_related_criteria: LibraryRelatedCriteriaOut | None = None

    @model_validator(mode="after")
    def _refuse_null_view_prefs(self) -> UiPrefsPatch:
        # None is "not sent" for these three, so an explicit JSON null would
        # pass as a silent no-op; they take a boolean or a view name, else a
        # 422 (Sol P1 4301475288).
        for key in _NON_NULL_VIEW_PREF_KEYS:
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} must not be null")
        if "lighting" in self.model_fields_set and self.lighting is None:
            raise ValueError("lighting must not be null")
        return self


# library_key_filter (#7599): [] clears it, so an explicit null is never "clear" (Sol P1, #7743).
# midi_setup_dismissed_ports (#10076): same; [] clears dismissed ports.
# deck_autocue: omitted means unchanged, so an explicit null is refused too (Sol P1, PR #6235).
_NON_NULL_VIEW_PREF_KEYS = (
    "show_pads",
    "auto_play_two_deck",
    "perf_view",
    "library_key_filter",
    "deck_autocue",
    "reduce_motion",
    "midi_setup_dismissed_ports",
)


def _merge_topbar_bool_prefs(current: dict[str, Any], body: UiPrefsPatch) -> None:
    """Topbar booleans, deck defaults, wave overlays and analyze-on-import lanes."""
    for key in (
        *_TOPBAR_BOOL_DEFAULTS,
        *_DECK_DEFAULT_BOOL_DEFAULTS,
        *_WAVE_OVERLAY_BOOL_DEFAULTS,
        *AUTO_ANALYZE_BOOL_DEFAULTS,
    ):
        value = getattr(body, key)
        if value is not None:
            current[key] = value


def _merge_library_browser_bool_prefs(current: dict[str, Any], body: UiPrefsPatch) -> None:
    for key in _LIBRARY_BROWSER_BOOL_KEYS:
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


def _merge_midi_prefs(current: dict[str, Any], body: UiPrefsPatch) -> None:
    """Replace midi_setup_dismissed_ports when the PATCH names it (#10076)."""
    if body.midi_setup_dismissed_ports is None:
        return
    current["midi_setup_dismissed_ports"] = _parse_midi_setup_dismissed_ports(
        {"midi_setup_dismissed_ports": list(body.midi_setup_dismissed_ports)}
    )


def _merge_compatible_filter(current: dict[str, Any], body: UiPrefsPatch) -> None:
    """Merge the named compatible-filter fields onto what is stored (LIBUX-32)."""
    if body.compatible_filter is None:
        return
    patch = body.compatible_filter.model_dump(exclude_unset=True)
    rules = patch.get("rules")
    if isinstance(rules, list):
        _reject_invalid_compatible_filter_rules(rules)
    current["compatible_filter"] = _parse_compatible_filter(
        {
            **current["compatible_filter"],
            **patch,
        }
    )


def _merge_lighting(current: dict[str, Any], body: UiPrefsPatch) -> None:
    if body.lighting is None:
        return
    patch = body.lighting.model_dump(exclude_unset=True)
    current["lighting"] = _parse_lighting(
        {**current.get("lighting", _DEFAULT_LIGHTING), **patch}
    )


def read_library_related_criteria(request: Request) -> dict[str, Any]:
    """The stored Related Tracks criteria (LIBUX-10046), defaults when never saved."""
    return dict(_load(_path(request))["library_related_criteria"])


def persist_master_muted(request: Request, muted: bool) -> None:
    """Merge master_muted into ui-prefs.json synchronously (CLI/MCP belt)."""
    if not isinstance(muted, bool):
        raise TypeError("muted must be boolean")
    path = _path(request)
    with _write_lock:
        current = _load(path)
        current["master_muted"] = muted
        _write_atomic(path, json.dumps(current, indent=2) + "\n")
    publish("library.changed", {"kind": "ui_prefs", "ids": []})


@router.get("", response_model=UiPrefsOut)
def get_ui_prefs(request: Request) -> UiPrefsOut:
    return UiPrefsOut.model_validate(_load(_path(request)))


@router.put("", response_model=UiPrefsOut)
def put_ui_prefs(body: UiPrefsPatch, request: Request) -> UiPrefsOut:
    return _persist_ui_prefs(request.app, body)


def _persist_ui_prefs(app: FastAPI, body: UiPrefsPatch) -> UiPrefsOut:
    path = _path_for_app(app)
    with _write_lock:
        current = _load(path)
        if body.lighting is not None and body.lighting.clock_source is not None:
            _validate_lighting_source(app, body.lighting.clock_source)
        current = _merge_ui_prefs_patch(current, body)
        _write_atomic(path, json.dumps(current, indent=2) + "\n")
        app.state.lighting_clock_cache = (path, current["lighting"]["clock_source"])
    publish("library.changed", {"kind": "ui_prefs", "ids": []})
    return UiPrefsOut.model_validate(current)


def _validate_lighting_source(app: FastAPI, source: str) -> None:
    refusal = lighting_refusal(app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    try:
        parse_clock_source(source)
    except ClockSourceError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
    if source != "master" and clock_snapshot(app, source).resolve().effective_deck is None:
        raise HTTPException(409, detail={"code": "lighting_clock_deck_empty", "message": "selected deck is not loaded"})


def read_lighting_clock_source(app: FastAPI) -> str:
    """Cache only the preference; every frame still resolves the current mirror."""
    path = _path_for_app(app)
    with _write_lock:
        cached = getattr(app.state, "lighting_clock_cache", None)
        if cached is None or cached[0] != path:
            cached = (path, _load(path)["lighting"]["clock_source"])
            app.state.lighting_clock_cache = cached
        return str(cached[1])


def _persist_lighting_clock_source(app: FastAPI, source: str) -> None:
    _persist_ui_prefs(app, UiPrefsPatch(lighting=LightingPatch(clock_source=source)))


def lighting_clock_for_app(app: FastAPI) -> LightingClockController:
    controller = getattr(app.state, "lighting_clock", None)
    if controller is None:
        controller = LightingClockController(
            app,
            lambda: read_lighting_clock_source(app),
            lambda source: _persist_lighting_clock_source(app, source),
        )
        app.state.lighting_clock = controller
    bind_lighting_runtime(app).clock_provider = controller.snapshot
    return controller


LibraryKeyFilterAction = Literal["toggle", "compatible", "reset", "set"]


class LibraryKeyFilterActionIn(BaseModel):
    """One library key wheel control, for agents (issue #7599).

    ``toggle`` needs ``key`` (the segment click), ``compatible`` needs
    ``reference`` (the Compatible button), ``set`` needs ``keys``, ``reset``
    needs nothing. Every key is a canonical Camelot code 1A..12B.
    """

    model_config = ConfigDict(extra="forbid")

    action: LibraryKeyFilterAction
    key: str | None = None
    reference: str | None = None
    keys: list[str] | None = None

    @model_validator(mode="after")
    def _fields_match_action(self) -> LibraryKeyFilterActionIn:
        required = {"toggle": "key", "compatible": "reference", "set": "keys"}.get(self.action)
        for name in ("key", "reference", "keys"):
            present = getattr(self, name) is not None
            if name == required and not present:
                raise ValueError(f"action {self.action!r} requires {name!r}")
            if name != required and present:
                raise ValueError(f"action {self.action!r} does not take {name!r}")
        return self


class LibraryKeyFilterActionOut(BaseModel):
    library_key_filter: list[str]


def library_key_filter_compatible(reference: str) -> list[str]:
    """The Compatible button's set: reference, number +/- 1, and its A/B swap.

    The default-on Camelot rules, the same table the frontend's generated
    ``camelot-rules.generated.ts`` and ``compatible-filter-prefs.ts`` read.
    """
    canonical = _parse_library_key_filter({"library_key_filter": [reference]})
    return [
        code
        for code in _LIBRARY_KEY_WHEEL_ORDER
        if camelot_rules_allow(canonical[0], code, DEFAULT_CAMELOT_RULE_IDS)
    ]


def _apply_library_key_filter_action(current: list[str], body: LibraryKeyFilterActionIn) -> list[str]:
    if body.action == "reset":
        return []
    elif body.action == "set":
        return _parse_library_key_filter({"library_key_filter": body.keys})
    elif body.action == "compatible":
        assert body.reference is not None
        return library_key_filter_compatible(body.reference)
    elif body.action == "toggle":
        assert body.key is not None
        toggled = _parse_library_key_filter({"library_key_filter": [body.key]})[0]
        selected = set(current) ^ {toggled}
        return [code for code in _LIBRARY_KEY_WHEEL_ORDER if code in selected]
    raise AssertionError(f"unhandled library key filter action {body.action!r}")


@router.post("/library-key-filter", response_model=LibraryKeyFilterActionOut)
def post_library_key_filter(body: LibraryKeyFilterActionIn, request: Request) -> LibraryKeyFilterActionOut:
    path = _path(request)
    with _write_lock:
        current = _load(path)
        current["library_key_filter"] = _apply_library_key_filter_action(
            current["library_key_filter"], body
        )
        _write_atomic(path, json.dumps(current, indent=2) + "\n")
    publish("library.changed", {"kind": "ui_prefs", "ids": []})
    return LibraryKeyFilterActionOut(library_key_filter=current["library_key_filter"])


@router.post("/library-tabs", response_model=LibraryTabsActionOut)
def post_library_tabs(body: LibraryTabsActionIn, request: Request) -> LibraryTabsActionOut:
    """The library tab strip for agents: list, open, switch or close one tab."""
    path = _path(request)
    with _write_lock:
        current = _load(path)
        updated = apply_library_tabs_action(current["library_tabs"], body)
        if updated is not current["library_tabs"]:
            current["library_tabs"] = updated
            _write_atomic(path, json.dumps(current, indent=2) + "\n")
            publish("library.changed", {"kind": "ui_prefs", "ids": []})
    return LibraryTabsActionOut(library_tabs=LibraryTabsOut.model_validate(current["library_tabs"]))


_VIEW_SCALAR_PREFS = (
    "library_density",
    "perf_view",
    "deck_right_mirror",
    "playlist_tree_view",
    "speaker_auto_recover",
    "tempo_lane_scale",
    "wave_height",
    "wave_grid_lines",
    "sections_main_style",
    "sections_overview_style",
    "sections_uncertain",
)


def _merge_view_scalar_prefs(current: dict[str, Any], body: UiPrefsPatch) -> None:
    """Replace-on-set view prefs: each is validated by its Patch field type."""
    for key in _VIEW_SCALAR_PREFS:
        value = getattr(body, key)
        if value is not None:
            current[key] = value


def _merge_ui_prefs_patch(current: dict[str, Any], body: UiPrefsPatch) -> dict[str, Any]:
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
    if body.show_stems is not None:
        current["show_stems"] = body.show_stems
    if body.wave_stem_follow is not None:
        current["wave_stem_follow"] = body.wave_stem_follow
    if body.reduce_motion is not None:
        current["reduce_motion"] = body.reduce_motion
    if body.deck_hover_link is not None:
        current["deck_hover_link"] = body.deck_hover_link
    if body.compact_decks is not None:
        current["compact_decks"] = body.compact_decks
    _merge_lyrics(current, body)
    if body.perf_tier is not None:
        current["perf_tier"] = body.perf_tier
    if body.app_posture is not None:
        current["app_posture"] = body.app_posture
    if body.gig_helper is not None:
        current["gig_helper"] = body.gig_helper
    if body.app_mode is not None:
        current["app_mode"] = _parse_app_mode(
            {**current["app_mode"], **body.app_mode.model_dump(exclude_unset=True)}
        )
    _merge_topbar_bool_prefs(current, body)
    _merge_library_browser_bool_prefs(current, body)
    _merge_view_scalar_prefs(current, body)
    for key in (
        "sections_sticky_title",
        "sections_next_countdown",
        "sections_library_structure_column",
        "sections_preview_cursor_label",
    ):
        value = getattr(body, key)
        if value is not None:
            current[key] = value
    _merge_library_enum_prefs(current, body)
    if body.wheel_sensitivity is not None:
        current["wheel_sensitivity"] = _parse_wheel_sensitivity(
            {
                **current["wheel_sensitivity"],
                **body.wheel_sensitivity.model_dump(exclude_unset=True),
            }
        )
    _merge_beat_jump_sizes(current, body)
    if body.level_calibration is not None:
        # R and M are independent (see LevelCalibrationOut docstring): merge onto
        # what's stored so a PUT naming only one half cannot silently wipe the
        # other, the way a bare replace against LevelCalibrationOut's own
        # per-field defaults would.
        current["level_calibration"] = _parse_level_calibration(
            {**current["level_calibration"], **body.level_calibration.model_dump(exclude_unset=True)}
        )
    if body.library_watcher_folders is not None:
        current["library_watcher_folders"] = _parse_library_watcher_folders(
            {"library_watcher_folders": body.library_watcher_folders}
        )
    if body.library_key_filter is not None:
        current["library_key_filter"] = _parse_library_key_filter(
            {"library_key_filter": body.library_key_filter}
        )
    _merge_midi_prefs(current, body)
    _merge_compatible_filter(current, body)
    _merge_lighting(current, body)
    merge_auto_gain(current, body.auto_gain)
    merge_library_tabs(current, body.library_tabs)
    merge_library_related_criteria(current, body.library_related_criteria)
    if body.keyboard is not None:
        cur = current.setdefault("keyboard", dict(_DEFAULT_KEYBOARD))
        patch = body.keyboard.model_dump(exclude_unset=True)
        if "preset" in patch:
            cur["preset"] = patch["preset"]
        if "overrides" in patch:
            cur["overrides"] = patch["overrides"]
    return current


class WatcherFoldersValidateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    paths: list[str] = Field(default_factory=list)


@router.post("/watcher-folders:validate")
def validate_watcher_folders(body: WatcherFoldersValidateIn) -> dict[str, bool]:
    """LIBM-129: existence check before save. The folder-rescan scheduler watches them."""
    missing: list[str] = []
    for raw in body.paths:
        path = Path(raw)
        if not path.is_dir():
            missing.append(raw)
    if missing:
        raise HTTPException(
            status_code=400,
            detail={"message": "watcher folder path does not exist", "missing": missing},
        )
    return {"ok": True}
