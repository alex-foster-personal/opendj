"""Independent flash checker over rendered DMX frames (spec 06 section 5, issue #14383).

An oracle, not a limiter: it reads the frames a renderer produced and counts flashes, so it shares
no code with ``apps/lighting/safety.py``. It imports only the universe constants and the fixture
profile types.

Definitions (WCAG 2.3.1 / 2.2 "general flash and red flash thresholds"):
- Relative luminance uses the sRGB formula on the linearised colour times intensity. A fixture
  without colour channels uses intensity as white; one without an intensity channel is at full.
- A general flash is a pair of opposing changes in relative luminance of 0.1 or more where the
  darker state is below 0.8.
- A red flash is a pair of opposing transitions to or from saturated red: red ratio
  R / (R + G + B) >= 0.8, with a change in (R - G - B) x 320 above 20 (linearised values).
- A commanded hardware strobe at ``hz`` counts as ``hz`` flashes per second while it runs with
  light output above zero. The strobe channel's active range maps linearly onto
  ``strobe_hz_range``; a range whose meaning contains "off" is no strobe.
- Rig-wide uses the mean of every fixture's linear colour; the rig strobe count is the highest
  per-fixture strobe rate.
- Out of phase: in one frame more than one fixture strobes and their rates differ, or (for
  commanded strobes with a phase) their phases differ. DMX gives no phase, so equal DMX strobe
  rates count as in phase.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from apps.lighting.fixtures.profile import FixtureProfile, Mode
from apps.lighting.universe import SLOT_COUNT, SLOT_MAX_VALUE

WINDOW_S = 1.0
MAX_FLASHES_PER_WINDOW = 3
GENERAL_DELTA = 0.1
GENERAL_DARK_LIMIT = 0.8
RED_RATIO = 0.8
RED_DELTA = 20.0
DEFAULT_STROBE_HZ_RANGE = (1.0, 20.0)
RIG = "rig"
_EPS = 1e-9


@dataclass(frozen=True)
class PatchedFixture:
    """One fixture: its name, profile mode, universe number and 1-based start address."""

    name: str
    profile: FixtureProfile
    mode: str
    universe: int
    address: int

    def channels(self) -> Mode:
        """Return the profile mode this fixture runs in."""
        mode = self.profile.mode(self.mode)
        if self.address < 1 or self.address + len(mode.channels) - 1 > SLOT_COUNT:
            raise ValueError(f"fixture {self.name!r} does not fit universe {self.universe} at {self.address}")
        return mode


@dataclass(frozen=True)
class Strobe:
    """A commanded strobe: rate in Hz and phase in cycles (0..1)."""

    hz: float
    phase: float = 0.0


@dataclass(frozen=True)
class RenderedFrame:
    """One rendered frame at time ``t`` seconds: universe number to 512 slot bytes.

    ``strobes`` names fixtures whose strobe was commanded directly; it overrides the DMX channel.
    """

    t: float
    universes: Mapping[int, bytes]
    strobes: Mapping[str, Strobe] = field(default_factory=dict)


@dataclass(frozen=True)
class FlashCount:
    """Worst 1 s window for one subject (a fixture name or ``RIG``)."""

    subject: str
    general: int
    red: int
    strobe: int
    window_start: float

    @property
    def total(self) -> int:
        """Return the worst of the general and red counts, each with commanded strobes added."""
        return max(self.general, self.red)


@dataclass(frozen=True)
class FlashReport:
    """Per-fixture and rig-wide worst windows plus out-of-phase strobe times."""

    fixtures: Mapping[str, FlashCount]
    rig: FlashCount
    out_of_phase_at: tuple[float, ...]
    limit: int = MAX_FLASHES_PER_WINDOW

    @property
    def offenders(self) -> tuple[str, ...]:
        """Return the names of fixtures over the limit, sorted."""
        return tuple(sorted(name for name, count in self.fixtures.items() if count.total > self.limit))

    @property
    def ok(self) -> bool:
        """Return True when no fixture or the rig exceeds the limit and no strobes are out of phase."""
        return not self.offenders and self.rig.total <= self.limit and not self.out_of_phase_at


# ----------------------------------------------------------------------------- colour


def _linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@dataclass(frozen=True)
class _Sample:
    rgb: tuple[float, float, float]
    strobe: Strobe | None


def _strobe_hz(value: int, channel_ranges: Sequence, hz_range: tuple[float, float]) -> float:
    low, high = hz_range
    for item in channel_ranges:
        if item.start <= value <= item.end:
            if "off" in item.meaning.lower().split():
                return 0.0
            span = item.end - item.start
            frac = 1.0 if span == 0 else (value - item.start) / span
            return low + (high - low) * frac
    return 0.0 if value == 0 else low + (high - low) * value / SLOT_MAX_VALUE


def _sample(fixture: PatchedFixture, frame: RenderedFrame, hz_range: tuple[float, float]) -> _Sample:
    mode = fixture.channels()
    data = frame.universes.get(fixture.universe, bytes(SLOT_COUNT))
    if len(data) != SLOT_COUNT:
        raise ValueError(f"universe {fixture.universe} frame holds {len(data)} slots, not {SLOT_COUNT}")
    values: dict[str, float] = {}
    strobe_hz = 0.0
    for offset, channel in enumerate(mode.channels):
        raw = data[fixture.address - 1 + offset]
        if channel.capability == "strobe":
            strobe_hz = _strobe_hz(raw, channel.ranges, hz_range)
        elif channel.capability in ("intensity", "red", "green", "blue", "white") and channel.capability not in values:
            values[channel.capability] = raw / SLOT_MAX_VALUE
    level = values.get("intensity", 1.0)
    if any(c in values for c in ("red", "green", "blue", "white")):
        white = values.get("white", 0.0)
        rgb = tuple(min(1.0, values.get(c, 0.0) + white) for c in ("red", "green", "blue"))
    else:
        rgb = (1.0, 1.0, 1.0)
    linear = (_linear(rgb[0]) * level, _linear(rgb[1]) * level, _linear(rgb[2]) * level)
    strobe = frame.strobes.get(fixture.name)
    if strobe is None and strobe_hz > 0:
        strobe = Strobe(strobe_hz)
    if strobe is not None and (strobe.hz <= 0 or _luminance(linear) <= 0):
        strobe = None
    return _Sample(linear, strobe)


# ----------------------------------------------------------------------------- transitions


def _general_transitions(times: Sequence[float], lum: Sequence[float]) -> list[float]:
    out: list[float] = []
    if not lum:
        return out
    anchor = lum[0]
    direction = 0
    for t, value in zip(times[1:], lum[1:], strict=True):
        delta = value - anchor
        new_dir = 1 if delta > 0 else -1
        if direction and new_dir == direction:
            anchor = value
            continue
        if abs(delta) >= GENERAL_DELTA - _EPS and min(value, anchor) < GENERAL_DARK_LIMIT:
            out.append(t)
            direction = new_dir
            anchor = value
    return out


def _red_level(rgb: tuple[float, float, float]) -> float | None:
    r, g, b = rgb
    total = r + g + b
    if total <= 0 or r / total < RED_RATIO:
        return None
    return max(0.0, (r - g - b) * 320.0)


def _red_transitions(times: Sequence[float], rgbs: Sequence[tuple[float, float, float]]) -> list[float]:
    out: list[float] = []
    if not rgbs:
        return out
    anchor = _red_level(rgbs[0]) or 0.0
    direction = 0
    for t, rgb in zip(times[1:], rgbs[1:], strict=True):
        red = _red_level(rgb)
        value = red or 0.0
        delta = value - anchor
        new_dir = 1 if delta > 0 else -1
        if direction and new_dir == direction:
            anchor = value
            continue
        if abs(delta) > RED_DELTA:
            out.append(t)
            direction = new_dir
            anchor = value
    return out


def _worst(
    subject: str,
    times: Sequence[float],
    general: Sequence[float],
    red: Sequence[float],
    strobe_hz: Sequence[float],
) -> FlashCount:
    best = FlashCount(subject, 0, 0, 0, times[0] if times else 0.0)
    for start in sorted(set(times) | set(general) | set(red)):
        end = start + WINDOW_S
        strobe_flashes = 0.0
        for index, t in enumerate(times):
            nxt = times[index + 1] if index + 1 < len(times) else t + (t - times[index - 1] if index else 0.0)
            lo, hi = max(t, start), min(nxt, end)
            if hi > lo:
                strobe_flashes += strobe_hz[index] * (hi - lo)
        strobe = math.floor(strobe_flashes + _EPS)
        g = sum(1 for t in general if start <= t < end) // 2 + strobe
        r = sum(1 for t in red if start <= t < end) // 2 + strobe
        if max(g, r) > best.total:
            best = FlashCount(subject, g, r, strobe, start)
    return best


def audit(
    frames: Sequence[RenderedFrame],
    patch: Sequence[PatchedFixture],
    *,
    strobe_hz_range: tuple[float, float] = DEFAULT_STROBE_HZ_RANGE,
) -> FlashReport:
    """Count the worst 1 s window of flashes per fixture and rig-wide and find out-of-phase strobes."""
    ordered = sorted(frames, key=lambda f: f.t)
    times = [f.t for f in ordered]
    samples = {fx.name: [_sample(fx, f, strobe_hz_range) for f in ordered] for fx in patch}
    counts: dict[str, FlashCount] = {}
    for name, series in samples.items():
        rgbs = [s.rgb for s in series]
        counts[name] = _worst(
            name,
            times,
            _general_transitions(times, [_luminance(c) for c in rgbs]),
            _red_transitions(times, rgbs),
            [s.strobe.hz if s.strobe else 0.0 for s in series],
        )
    n = max(1, len(patch))
    rig_rgbs = [
        tuple(sum(samples[fx.name][i].rgb[c] for fx in patch) / n for c in range(3)) for i in range(len(ordered))
    ]
    rig_hz = [max((s[i].strobe.hz for s in samples.values() if s[i].strobe), default=0.0) for i in range(len(ordered))]
    rig = _worst(
        RIG,
        times,
        _general_transitions(times, [_luminance(c) for c in rig_rgbs]),
        _red_transitions(times, rig_rgbs),
        rig_hz,
    )
    out_of_phase: list[float] = []
    for i, t in enumerate(times):
        active = [s[i].strobe for s in samples.values() if s[i].strobe]
        if len(active) > 1 and any(
            abs(a.hz - active[0].hz) > _EPS
            or min((a.phase - active[0].phase) % 1.0, (active[0].phase - a.phase) % 1.0) > _EPS
            for a in active[1:]
        ):
            out_of_phase.append(t)
    return FlashReport(counts, rig, tuple(out_of_phase))
