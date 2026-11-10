"""Per-lane analyze-on-import toggles (#10259), read from ui-prefs.json."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.analysis.backlog import AUTO_LANES, AutoLane

AUTO_ANALYZE_BOOL_DEFAULTS: dict[str, bool] = {
    "auto_analyze_grid": True,
    "auto_analyze_key": True,
    "auto_analyze_sections": True,
    "auto_analyze_vocals": False,
}

PREF_KEY_BY_LANE: dict[AutoLane, str] = {
    "grid": "auto_analyze_grid",
    "key": "auto_analyze_key",
    "sections": "auto_analyze_sections",
    "vocals": "auto_analyze_vocals",
}


def lanes_from_prefs(prefs: Mapping[str, Any]) -> frozenset[AutoLane]:
    """Enabled lanes from a parsed ui-prefs dict. Missing keys are errors."""
    out: set[AutoLane] = set()
    for lane in AUTO_LANES:
        key = PREF_KEY_BY_LANE[lane]
        if key not in prefs:
            raise KeyError(key)
        if prefs[key]:
            out.add(lane)
    return frozenset(out)


DEFAULT_AUTO_LANES: frozenset[AutoLane] = lanes_from_prefs(AUTO_ANALYZE_BOOL_DEFAULTS)


def lanes_out_dict(prefs: Mapping[str, Any]) -> dict[str, bool]:
    """The four booleans for GET /analysis-queue auto.lanes."""
    return {
        lane: bool(prefs.get(PREF_KEY_BY_LANE[lane], AUTO_ANALYZE_BOOL_DEFAULTS[PREF_KEY_BY_LANE[lane]]))
        for lane in AUTO_LANES
    }


__all__ = [
    "AUTO_ANALYZE_BOOL_DEFAULTS",
    "DEFAULT_AUTO_LANES",
    "PREF_KEY_BY_LANE",
    "lanes_from_prefs",
    "lanes_out_dict",
]
