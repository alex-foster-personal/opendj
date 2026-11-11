"""Recency term for suggest-next (issue #10397): rest tracks played in the last 14 days."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType

from apps.shared.harmonic import TrackFeature

REST_DAYS = 14.0
HISTORY_WEIGHT_BY_VARIANT: Mapping[str, float] = MappingProxyType(
    {"harmonic": 0.0, "energy_led": 0.0, "flow": 0.10}
)


def _ensure_aware_utc(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware UTC")


def days_since_played(candidate: TrackFeature, now: datetime) -> float | None:
    """Days since ``last_played_at``; None when unknown."""
    _ensure_aware_utc(now)
    if candidate.last_played_at is None:
        return None
    return max(0.0, (now - candidate.last_played_at).total_seconds() / 86400.0)


def history_term(candidate: TrackFeature, now: datetime) -> float | None:
    """1.0 when rested or never played; linear to 0.0 at 0 days; None when unscored."""
    _ensure_aware_utc(now)
    if candidate.never_played:
        return 1.0
    days = days_since_played(candidate, now)
    if days is None:
        return None
    return min(days / REST_DAYS, 1.0)
