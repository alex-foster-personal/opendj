"""Beat-quantised strobe rate under the safety flash cap (issue #14512, spec 06 section 3).

A division is a note length as a fraction of a whole note: 1/4 is one flash per beat,
1/8 is two per beat. When the requested division would flash faster than the cap
(``SafetySettings.flash_limit_per_second``), it steps down to the next longer division.
Beat phase is tracked in beats, so a tempo change keeps the strobe on the beat.
"""

from __future__ import annotations

import math
from fractions import Fraction

from apps.lighting.effects import StrobeParams
from apps.lighting.safety import SafetySettings

DIVISIONS: tuple[Fraction, ...] = (
    Fraction(1, 32),
    Fraction(1, 16),
    Fraction(1, 8),
    Fraction(1, 4),
    Fraction(1, 2),
    Fraction(1, 1),
    Fraction(2, 1),
    Fraction(4, 1),
)
_EPS = 1e-9


def _check_bpm(bpm: float) -> None:
    if not math.isfinite(bpm) or bpm <= 0:
        raise ValueError(f"bpm must be finite and > 0, got {bpm}")


def _cap(settings: SafetySettings | None) -> float:
    return (settings or SafetySettings()).flash_limit_per_second


def division_hz(bpm: float, division: Fraction) -> float:
    """Flash rate in Hz for a note division at a tempo (1/4 is one flash per beat)."""
    _check_bpm(bpm)
    if division <= 0:
        raise ValueError(f"division must be > 0, got {division}")
    return (bpm / 60.0) / float(division * 4)


def quantise_division(bpm: float, division: Fraction, settings: SafetySettings | None = None) -> Fraction | None:
    """Return the requested division, stepped down to longer ones until under the cap, or None."""
    cap = _cap(settings)
    for d in DIVISIONS:
        if d < division:
            continue
        if division_hz(bpm, d) <= cap + _EPS:
            return d
    return None


def strobe_rate_hz(bpm: float, division: Fraction, settings: SafetySettings | None = None) -> float:
    """Return the beat-quantised strobe rate in Hz, never above the flash cap (0 when none fits)."""
    d = quantise_division(bpm, division, settings)
    if d is None:
        return 0.0
    return min(division_hz(bpm, d), _cap(settings))


def strobe_params(bpm: float, division: Fraction, settings: SafetySettings | None = None) -> StrobeParams | None:
    """Build effects.StrobeParams for the quantised rate, or None when no division fits the cap."""
    rate = strobe_rate_hz(bpm, division, settings)
    if rate <= 0:
        return None
    return StrobeParams(rate_hz=rate, bpm=bpm)


class StrobeClock:
    """Tracks beat position over time so tempo changes keep strobe phase on the beat."""

    def __init__(self, bpm: float, start_s: float = 0.0, beat: float = 0.0) -> None:
        _check_bpm(bpm)
        self._bpm = bpm
        self._t = start_s
        self._beat = beat

    def beat_at(self, now_s: float) -> float:
        """Beat position at a time, from the last anchor and current tempo."""
        if now_s < self._t:
            raise ValueError(f"now must be non-decreasing, got {now_s} after {self._t}")
        return self._beat + (now_s - self._t) * self._bpm / 60.0

    def set_bpm(self, now_s: float, bpm: float) -> None:
        """Change tempo at a time, keeping the beat position continuous."""
        _check_bpm(bpm)
        self._beat = self.beat_at(now_s)
        self._t = now_s
        self._bpm = bpm

    def phase(self, now_s: float, division: Fraction, settings: SafetySettings | None = None) -> float | None:
        """Strobe cycle phase 0..1 at a time for the quantised division, or None when none fits."""
        d = quantise_division(self._bpm, division, settings)
        if d is None:
            return None
        cycles = self.beat_at(now_s) / float(d * 4)
        return cycles - math.floor(cycles)
