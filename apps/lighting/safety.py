"""Photosensitive flash limiter (LIGHT-10578, spec item L52).

Runs on every frame after compositing and before DMX rendering.

Sources (public documents only):
- WCAG 2.x: general and red flash thresholds, relative luminance (sRGB).
- Ofcom guidance on flashing images (3 flashes per second).
- UK HSE venue guidance (4 Hz, looser; cap here stays at WCAG / Ofcom 3).
- Epilepsy Society notes on photosensitive epilepsy triggers.

Choices:
- Relative luminance is the WCAG / sRGB formula on rendered colour scaled by intensity.
- Rig-wide check uses mean luminance plus the sum of per-fixture hardware rates (strict).
- When a rig-wide change would exceed the budget, hold every fixture driving it, including
  ones with no individual change, and recheck the mean before committing.
- ``flash_limit_per_second`` can only be lowered from 3, never raised.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MAX_FLASHES_PER_SECOND = 3.0
WINDOW_S = 1.0
GENERAL_FLASH_DELTA = 0.10
GENERAL_FLASH_DARK_MAX = 0.80
RED_SATURATION_MIN = 0.80
RED_DELTA_SCALE = 320.0
RED_DELTA_MIN = 20.0
SETTINGS_KEY = "flash_limit_per_second"
_EPS = 1e-9


class SafetySettingsError(ValueError):
    """Invalid lighting safety settings."""


@dataclass(frozen=True)
class SafetySettings:
    flash_limit_per_second: float = 3.0

    def __post_init__(self) -> None:
        v = self.flash_limit_per_second
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SafetySettingsError(f"flash_limit_per_second must be a number, got {type(v).__name__}")
        fv = float(v)
        if not math.isfinite(fv):
            raise SafetySettingsError(f"flash_limit_per_second must be finite, got {fv}")
        if fv < 0:
            raise SafetySettingsError(f"flash_limit_per_second must be non-negative, got {fv}")
        if fv > MAX_FLASHES_PER_SECOND:
            raise SafetySettingsError(
                f"flash_limit_per_second is {fv}; the maximum is 3 flashes per second "
                "(WCAG 2.3.1), it can only be lowered"
            )
        if fv != self.flash_limit_per_second:
            object.__setattr__(self, "flash_limit_per_second", fv)


def load_safety_settings(settings_path: Path) -> SafetySettings:
    """Read ``flash_limit_per_second`` from the lighting settings file (default 3)."""
    if not settings_path.exists():
        return SafetySettings()
    raw = json.loads(settings_path.read_text())
    if not isinstance(raw, dict):
        raise SafetySettingsError(f"{settings_path} must hold a JSON object")
    if SETTINGS_KEY not in raw:
        return SafetySettings()
    return SafetySettings(raw[SETTINGS_KEY])


@dataclass(frozen=True)
class FixtureLight:
    rgb: tuple[int, int, int]
    intensity: float
    white: float | None = None
    strobe_hz: float | None = None

    def __post_init__(self) -> None:
        for i, c in enumerate(self.rgb):
            if not isinstance(c, int) or not 0 <= c <= 255:
                raise ValueError(f"rgb[{i}] must be an int 0..255, got {c!r}")
        if not 0.0 <= self.intensity <= 1.0:
            raise ValueError(f"intensity must be 0..1, got {self.intensity}")
        if self.white is not None and not 0.0 <= self.white <= 1.0:
            raise ValueError(f"white must be 0..1, got {self.white}")
        if self.strobe_hz is not None and (not math.isfinite(self.strobe_hz) or self.strobe_hz < 0):
            raise ValueError(f"strobe_hz must be finite and >= 0, got {self.strobe_hz}")


@dataclass(frozen=True)
class LimiterStatus:
    flash_limit_per_second: float
    limit_events: int
    fixture_flashes: Mapping[str, float]
    rig_flashes: float

    def as_dict(self) -> dict[str, object]:
        return {
            "flash_limit_per_second": self.flash_limit_per_second,
            "limit_events": self.limit_events,
            "fixture_flashes": {k: self.fixture_flashes[k] for k in sorted(self.fixture_flashes)},
            "rig_flashes": self.rig_flashes,
        }


# ----- colour math


def _linear_unit(s: float) -> float:
    if s <= 0.04045:
        return s / 12.92
    return ((s + 0.055) / 1.055) ** 2.4


def _linear_rgb(light: FixtureLight) -> tuple[float, float, float]:
    w = light.white or 0.0
    intensity = light.intensity
    r, g, b = light.rgb
    ru = min(1.0, r / 255.0 + w) * intensity
    gu = min(1.0, g / 255.0 + w) * intensity
    bu = min(1.0, b / 255.0 + w) * intensity
    return (_linear_unit(ru), _linear_unit(gu), _linear_unit(bu))


def relative_luminance(light: FixtureLight) -> float:
    """WCAG relative luminance of the rendered fixture output."""
    r, g, b = _linear_rgb(light)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _red_value_linear(lin: tuple[float, float, float]) -> float:
    r, g, b = lin
    return max(0.0, r - g - b) * RED_DELTA_SCALE


def _saturated_linear(lin: tuple[float, float, float]) -> bool:
    r, g, b = lin
    total = r + g + b
    return total > 0 and r / total >= RED_SATURATION_MIN


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


# ----- change detection


class _ChangeDetector:
    def __init__(self, *, red: bool = False) -> None:
        self._red = red
        self.ref = 0.0
        self.ref_sat = False
        self.direction = 0
        self._times: deque[float] = deque()

    def _threshold_met(self, delta: float, value: float, sat: bool) -> bool:
        if self._red:
            return abs(delta) > RED_DELTA_MIN and (self.ref_sat or sat) and _sign(delta) != self.direction
        return (
            abs(delta) >= GENERAL_FLASH_DELTA
            and min(value, self.ref) < GENERAL_FLASH_DARK_MAX
            and _sign(delta) != self.direction
        )

    def would_change(self, value: float, sat: bool = False) -> bool:
        if self.direction != 0 and (value - self.ref) * self.direction > 0:
            return False
        return self._threshold_met(value - self.ref, value, sat)

    def observe(self, now: float, value: float, sat: bool = False) -> bool:
        if self.direction != 0 and (value - self.ref) * self.direction > 0:
            self.ref = value
            if self._red:
                self.ref_sat = self.ref_sat or sat
            return False
        delta = value - self.ref
        if self._threshold_met(delta, value, sat):
            self._times.append(now)
            self.direction = _sign(delta)
            self.ref = value
            self.ref_sat = sat
            return True
        return False

    def count(self, now: float) -> int:
        cutoff = now - WINDOW_S
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()
        return len(self._times)


def _max_hz_in_window(history: deque[tuple[float, float]], now: float) -> float:
    cutoff = now - WINDOW_S
    best = 0.0
    for t, hz in history:
        if t > cutoff:
            best = max(best, hz)
    return best


def _prune_hw(history: deque[tuple[float, float]], now: float) -> None:
    cutoff = now - WINDOW_S
    while history and history[0][0] <= cutoff:
        history.popleft()


def _mean_over_fixtures(lights: Mapping[str, FixtureLight]) -> tuple[float, tuple[float, float, float]]:
    if not lights:
        return 0.0, (0.0, 0.0, 0.0)
    lums: list[float] = []
    rs: list[float] = []
    gs: list[float] = []
    bs: list[float] = []
    for light in lights.values():
        lin = _linear_rgb(light)
        lums.append(0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2])
        rs.append(lin[0])
        gs.append(lin[1])
        bs.append(lin[2])
    n = float(len(lights))
    return sum(lums) / n, (sum(rs) / n, sum(gs) / n, sum(bs) / n)


# ----- limiter


class FlashLimiter:
    """WCAG 2.3.1 flash limiter over a sliding 1 s window."""

    def __init__(self, settings: SafetySettings | None = None) -> None:
        self._settings = settings or SafetySettings()
        self._lum: dict[str, _ChangeDetector] = {}
        self._red: dict[str, _ChangeDetector] = {}
        self._rig_lum = _ChangeDetector()
        self._rig_red = _ChangeDetector(red=True)
        self._last: dict[str, FixtureLight] = {}
        self._hw_fixture: dict[str, deque[tuple[float, float]]] = {}
        self._hw_rig: deque[tuple[float, float]] = deque()
        self._limit_events = 0
        self._last_now: float | None = None

    def _detectors(self, fid: str) -> tuple[_ChangeDetector, _ChangeDetector]:
        if fid not in self._lum:
            self._lum[fid] = _ChangeDetector()
            self._red[fid] = _ChangeDetector(red=True)
        return self._lum[fid], self._red[fid]

    def _change_count(self, fid: str, now: float) -> int:
        lum, red = self._detectors(fid)
        return max(lum.count(now), red.count(now))

    def _hold_color(self, fid: str) -> FixtureLight:
        prev = self._last.get(fid)
        if prev is None:
            return FixtureLight(rgb=(0, 0, 0), intensity=0.0)
        return FixtureLight(rgb=prev.rgb, intensity=prev.intensity, white=prev.white)

    def _hw_hz_at(self, now: float, frame: Mapping[str, FixtureLight]) -> tuple[float, dict[str, float]]:
        h_rig = _max_hz_in_window(self._hw_rig, now)
        per: dict[str, float] = {}
        for fid in frame:
            hist = self._hw_fixture.setdefault(fid, deque())
            per[fid] = _max_hz_in_window(hist, now)
        return h_rig, per

    def _tentative_colors(
        self,
        now: float,
        frame: Mapping[str, FixtureLight],
        cap: float,
        h_rig: float,
        h_fid: Mapping[str, float],
    ) -> tuple[dict[str, FixtureLight], list[str]]:
        sum_c = sum(self._change_count(fid, now) for fid in frame)
        accepted = 0
        tentative: dict[str, FixtureLight] = {}
        accepted_fids: list[str] = []
        for fid in sorted(frame.keys()):
            candidate = frame[fid]
            lum_det, red_det = self._detectors(fid)
            ci = self._change_count(fid, now)
            lin = _linear_rgb(candidate)
            sat = _saturated_linear(lin)
            rv = _red_value_linear(lin)
            lum_v = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
            would = lum_det.would_change(lum_v) or red_det.would_change(rv, sat)
            if not would:
                tentative[fid] = candidate
                continue
            fixture_ok = (ci + 1) / 2 + h_fid.get(fid, 0.0) <= cap + _EPS
            rig_ok = (sum_c + accepted + 1) / 2 + h_rig <= cap + _EPS
            if fixture_ok and rig_ok:
                tentative[fid] = candidate
                accepted += 1
                accepted_fids.append(fid)
            else:
                tentative[fid] = self._hold_color(fid)
        return tentative, accepted_fids

    def _tentative_differs_from_hold(self, fid: str, light: FixtureLight) -> bool:
        held = self._hold_color(fid)
        return light.rgb != held.rgb or light.intensity != held.intensity or light.white != held.white

    def _rig_unsafe(
        self,
        now: float,
        tentative: Mapping[str, FixtureLight],
        cap: float,
        h_rig: float,
    ) -> tuple[bool, bool, bool]:
        c_mean_before = max(self._rig_lum.count(now), self._rig_red.count(now))
        mean_lum, mean_lin = _mean_over_fixtures(tentative)
        mean_sat = _saturated_linear(mean_lin)
        mean_rv = _red_value_linear(mean_lin)
        lum_fires = self._rig_lum.would_change(mean_lum)
        red_fires = self._rig_red.would_change(mean_rv, mean_sat)
        unsafe = (lum_fires or red_fires) and (c_mean_before + 1) / 2 + h_rig > cap + _EPS
        return unsafe, lum_fires, red_fires

    def _rig_mean_hold_pick(
        self,
        tentative: Mapping[str, FixtureLight],
        lum_fires: bool,
    ) -> str | None:
        candidates = [fid for fid in sorted(tentative.keys()) if self._tentative_differs_from_hold(fid, tentative[fid])]
        if not candidates:
            return None

        def pull_key(fid: str) -> tuple[float, str]:
            held = self._hold_color(fid)
            if lum_fires:
                delta = relative_luminance(tentative[fid]) - relative_luminance(held)
                direction = self._rig_lum.direction
                if direction == 0:
                    return (-abs(delta), fid)
                return (delta * direction, fid)
            delta = _red_value_linear(_linear_rgb(tentative[fid])) - _red_value_linear(_linear_rgb(held))
            direction = self._rig_red.direction
            if direction == 0:
                return (-abs(delta), fid)
            return (delta * direction, fid)

        return min(candidates, key=pull_key)

    def _apply_rig_mean_hold(
        self,
        now: float,
        tentative: dict[str, FixtureLight],
        accepted_fids: list[str],
        cap: float,
        h_rig: float,
    ) -> dict[str, FixtureLight]:
        unsafe, _, _ = self._rig_unsafe(now, tentative, cap, h_rig)
        if not unsafe:
            return tentative
        for fid in accepted_fids:
            tentative[fid] = self._hold_color(fid)
        while True:
            unsafe, lum_fires, _ = self._rig_unsafe(now, tentative, cap, h_rig)
            if not unsafe:
                break
            pick = self._rig_mean_hold_pick(tentative, lum_fires)
            if pick is None:
                break
            tentative[pick] = self._hold_color(pick)
        return tentative

    def _observe_tentative(self, now: float, tentative: Mapping[str, FixtureLight]) -> None:
        for fid in sorted(tentative.keys()):
            light = tentative[fid]
            lin = _linear_rgb(light)
            sat = _saturated_linear(lin)
            rv = _red_value_linear(lin)
            lum_v = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
            lum_det, red_det = self._detectors(fid)
            lum_det.observe(now, lum_v)
            red_det.observe(now, rv, sat)
        mean_lum, mean_lin = _mean_over_fixtures(tentative)
        mean_sat = _saturated_linear(mean_lin)
        mean_rv = _red_value_linear(mean_lin)
        self._rig_lum.observe(now, mean_lum)
        self._rig_red.observe(now, mean_rv, mean_sat)

    def _strobe_grants(
        self,
        frame: Mapping[str, FixtureLight],
        tentative: Mapping[str, FixtureLight],
        cap: float,
        now: float,
    ) -> dict[str, float]:
        f_i = {fid: self._change_count(fid, now) / 2 for fid in tentative}
        c_mean_after = max(self._rig_lum.count(now), self._rig_red.count(now))
        f_rig = max(c_mean_after / 2, sum(f_i.values()))
        grants: dict[str, float] = {}
        for fid in tentative:
            inp = frame[fid]
            requested = 0.0 if inp.strobe_hz is None else inp.strobe_hz
            grants[fid] = min(requested, max(0.0, cap - f_i[fid]))
        sum_grant = sum(grants.values())
        rig_budget = max(0.0, cap - f_rig)
        if sum_grant > rig_budget + _EPS and sum_grant > 0:
            scale = rig_budget / sum_grant
            for fid in grants:
                grants[fid] *= scale
        return grants

    def _build_result(
        self,
        now: float,
        frame: Mapping[str, FixtureLight],
        tentative: Mapping[str, FixtureLight],
        grants: Mapping[str, float],
    ) -> dict[str, FixtureLight]:
        result: dict[str, FixtureLight] = {}
        sum_hz = 0.0
        for fid, g in grants.items():
            sum_hz += g
            hw = self._hw_fixture.setdefault(fid, deque())
            hw.append((now, g))
            _prune_hw(hw, now)
            inp = frame[fid]
            tent = tentative[fid]
            out_strobe: float | None = None if inp.strobe_hz is None or g == 0 else g
            out = FixtureLight(rgb=tent.rgb, intensity=tent.intensity, white=tent.white, strobe_hz=out_strobe)
            result[fid] = out
            if out != inp:
                self._limit_events += 1
        self._hw_rig.append((now, sum_hz))
        _prune_hw(self._hw_rig, now)
        return result

    def limit(self, now: float, frame: Mapping[str, FixtureLight]) -> dict[str, FixtureLight]:
        if not math.isfinite(now):
            raise ValueError(f"now must be finite, got {now}")
        if self._last_now is not None and now < self._last_now:
            raise ValueError(f"now must be non-decreasing, got {now} after {self._last_now}")

        cap = self._settings.flash_limit_per_second
        h_rig, h_fid = self._hw_hz_at(now, frame)
        tentative, accepted_fids = self._tentative_colors(now, frame, cap, h_rig, h_fid)
        tentative = self._apply_rig_mean_hold(now, tentative, accepted_fids, cap, h_rig)
        self._observe_tentative(now, tentative)
        grants = self._strobe_grants(frame, tentative, cap, now)
        result = self._build_result(now, frame, tentative, grants)
        self._last = dict(result)
        self._last_now = now
        return result

    def status(self) -> LimiterStatus:
        now = self._last_now if self._last_now is not None else 0.0
        cap = self._settings.flash_limit_per_second
        all_fids = sorted(set(self._lum.keys()) | set(self._last.keys()))
        fixture_flashes: dict[str, float] = {}
        sum_half = 0.0
        for fid in all_fids:
            ci = self._change_count(fid, now)
            hi = _max_hz_in_window(self._hw_fixture.get(fid, deque()), now)
            fixture_flashes[fid] = ci / 2 + hi
            if fid in self._last:
                sum_half += ci / 2
        C_mean = max(self._rig_lum.count(now), self._rig_red.count(now))
        H_rig = _max_hz_in_window(self._hw_rig, now)
        rig_flashes = max(C_mean / 2, sum_half) + H_rig
        return LimiterStatus(
            flash_limit_per_second=cap,
            limit_events=self._limit_events,
            fixture_flashes=fixture_flashes,
            rig_flashes=rig_flashes,
        )
