"""Backend-owned runtime policy knobs (issue #284, ADR-0112).

Numeric thresholds that affect product behavior are resolved once at import
from environment variables with strict validation. Published read-only on
GET /api/v1/settings; the frontend hydrates from that surface for display
policy (not ui-prefs toggles).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

# ----- shipped defaults (single source of truth) ---------------------------

_DEFAULT_HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO = 0.3
_DEFAULT_ANLZ_POINTS_MIN = 100
_DEFAULT_ANLZ_POINTS_MAX = 38400
_DEFAULT_ANLZ_POINTS_DEFAULT = 38400
_DEFAULT_FILE_EXISTS_TTL_S = 30.0


@dataclass(frozen=True)
class RuntimePolicySetting:
    key: str
    value: Any
    note: str


def _env_float(
    name: str,
    default: float,
    *,
    gt: float | None = None,
    gt_zero: bool = False,
    le: float | None = None,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a float, got {raw!r}") from exc
    # float() accepts "nan" and "inf", and every range comparison below is
    # False for NaN, so a non-finite value would slip through them all.
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    if gt_zero and value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    if gt is not None and value <= gt:
        raise ValueError(f"{name} must be > {gt}, got {value}")
    if le is not None and value > le:
        raise ValueError(f"{name} must be <= {le}, got {value}")
    return value


def _env_int(name: str, default: int, *, ge: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an int, got {raw!r}") from exc
    if value < ge:
        raise ValueError(f"{name} must be >= {ge}, got {value}")
    return value


ANLZ_POINTS_MIN: int = _env_int(
    "MDT_ANLZ_POINTS_MIN",
    _DEFAULT_ANLZ_POINTS_MIN,
    ge=1,
)
ANLZ_POINTS_MAX: int = _env_int(
    "MDT_ANLZ_POINTS_MAX",
    _DEFAULT_ANLZ_POINTS_MAX,
    ge=ANLZ_POINTS_MIN,
)
ANLZ_POINTS_DEFAULT: int = _env_int(
    "MDT_ANLZ_POINTS_DEFAULT",
    _DEFAULT_ANLZ_POINTS_DEFAULT,
    ge=ANLZ_POINTS_MIN,
)
if ANLZ_POINTS_DEFAULT > ANLZ_POINTS_MAX:
    raise ValueError(
        "MDT_ANLZ_POINTS_DEFAULT must be <= MDT_ANLZ_POINTS_MAX "
        f"({ANLZ_POINTS_DEFAULT} > {ANLZ_POINTS_MAX})"
    )

HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO: float = _env_float(
    "MDT_HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO",
    _DEFAULT_HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO,
    gt=0.0,
    le=1.0,
)

FILE_EXISTS_TTL_S: float = _env_float(
    "MDT_FILE_EXISTS_TTL_S",
    _DEFAULT_FILE_EXISTS_TTL_S,
    gt_zero=True,
)


def mostly_broken_playlist(available_count: int, track_count: int) -> bool:
    """True when a playlist is below the min-available ratio (hide-broken policy)."""
    if available_count < 0:
        return False
    if track_count == 0:
        return available_count == 0
    return (
        available_count / track_count
        < HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO
    )


def settings_items() -> list[RuntimePolicySetting]:
    return [
        RuntimePolicySetting(
            key="hide_broken_playlist_min_available_ratio",
            value=HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO,
            note="MDT_HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO env var.",
        ),
        RuntimePolicySetting(
            key="anlz_points_default",
            value=ANLZ_POINTS_DEFAULT,
            note="MDT_ANLZ_POINTS_DEFAULT env var.",
        ),
        RuntimePolicySetting(
            key="anlz_points_min",
            value=ANLZ_POINTS_MIN,
            note="MDT_ANLZ_POINTS_MIN env var.",
        ),
        RuntimePolicySetting(
            key="anlz_points_max",
            value=ANLZ_POINTS_MAX,
            note="MDT_ANLZ_POINTS_MAX env var.",
        ),
        RuntimePolicySetting(
            key="file_exists_ttl_s",
            value=FILE_EXISTS_TTL_S,
            note="MDT_FILE_EXISTS_TTL_S env var.",
        ),
    ]


__all__ = [
    "ANLZ_POINTS_DEFAULT",
    "ANLZ_POINTS_MAX",
    "ANLZ_POINTS_MIN",
    "FILE_EXISTS_TTL_S",
    "HIDE_BROKEN_PLAYLIST_MIN_AVAILABLE_RATIO",
    "RuntimePolicySetting",
    "mostly_broken_playlist",
    "settings_items",
]
