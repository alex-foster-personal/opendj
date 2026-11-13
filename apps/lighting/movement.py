"""LIGHT-14519: pure pan/tilt movement path generators (circle, figure8, sweep, ballyhoo).

Paths run on ``bar_phase`` like ``effects.sweep``: one cycle every ``MovementParams.bars`` bars,
so phase 0 and phase 1 (in cycle units) land on the same point. Output pan and tilt are
normalised 0..1 (0 = one end of the fixture's travel, 1 = the other) and are first scaled into
the fixture's ``MovementLimits`` box, so they never leave either range.

``fixture_offset`` spreads N fixtures evenly around the cycle (``index / count``); ``offsets_for``
does the same for a patch group ordered by stage x. ``slew_limit`` caps the step between
consecutive frames (for example 30 Hz) so a mover is never asked to jump faster than it can.

Standard library only; no clock reads.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from apps.lighting.effects import EffectOutput, _check_fixture
from apps.lighting.patch import PatchedFixture

Point = tuple[float, float]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class MovementLimits:
    """Allowed normalised pan/tilt box for one fixture."""

    pan_min: float = 0.0
    pan_max: float = 1.0
    tilt_min: float = 0.0
    tilt_max: float = 1.0

    def __post_init__(self) -> None:
        for name in ("pan_min", "pan_max", "tilt_min", "tilt_max"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in 0..1, got {value}")
        if self.pan_min > self.pan_max:
            raise ValueError("pan_min must be <= pan_max")
        if self.tilt_min > self.tilt_max:
            raise ValueError("tilt_min must be <= tilt_max")


@dataclass(frozen=True)
class MovementParams:
    bars: float = 4.0
    size: float = 1.0
    sweep_axis: Literal["pan", "tilt"] = "pan"
    limits: MovementLimits = MovementLimits()

    def __post_init__(self) -> None:
        if self.bars <= 0:
            raise ValueError(f"bars must be > 0, got {self.bars}")
        if not 0.0 <= self.size <= 1.0:
            raise ValueError(f"size must be in 0..1, got {self.size}")
        if self.sweep_axis not in ("pan", "tilt"):
            raise ValueError(f"sweep_axis must be pan or tilt, got {self.sweep_axis!r}")


_DEFAULT = MovementParams()


def _circle(t: float) -> Point:
    a = 2.0 * math.pi * t
    return math.cos(a), math.sin(a)


def _figure8(t: float) -> Point:
    a = 2.0 * math.pi * t
    return math.sin(a), math.sin(2.0 * a)


def _ballyhoo(t: float) -> Point:
    a = 2.0 * math.pi * t
    return math.sin(3.0 * a), math.sin(2.0 * a + math.pi / 4.0)


_SHAPES: Mapping[str, Callable[[float], Point]] = MappingProxyType(
    {"circle": _circle, "figure8": _figure8, "ballyhoo": _ballyhoo}
)


def fixture_offset(fixture_index: int, fixture_count: int) -> float:
    """Return the cycle offset for one fixture, spread evenly over ``fixture_count``."""
    _check_fixture(fixture_index, fixture_count)
    return fixture_index / fixture_count


def offsets_for(fixtures: Sequence[PatchedFixture]) -> dict[str, float]:
    """Return an even cycle offset per fixture id, ordered left to right by stage x."""
    ordered = sorted(fixtures, key=lambda f: (f.position.x, f.id))
    count = len(ordered)
    return {f.id: i / count for i, f in enumerate(ordered)}


def _scale(unit: Point, limits: MovementLimits, size: float) -> Point:
    pan_mid = (limits.pan_min + limits.pan_max) / 2.0
    tilt_mid = (limits.tilt_min + limits.tilt_max) / 2.0
    pan_half = (limits.pan_max - limits.pan_min) / 2.0 * size
    tilt_half = (limits.tilt_max - limits.tilt_min) / 2.0 * size
    pan = min(limits.pan_max, max(limits.pan_min, pan_mid + pan_half * unit[0]))
    tilt = min(limits.tilt_max, max(limits.tilt_min, tilt_mid + tilt_half * unit[1]))
    return _clamp01(pan), _clamp01(tilt)


def path_point(shape: str, cycle_phase: float, params: MovementParams | None = None) -> Point:
    """Return normalised (pan, tilt) for ``shape`` at ``cycle_phase`` (1.0 is one full cycle)."""
    p = _DEFAULT if params is None else params
    t = cycle_phase - math.floor(cycle_phase)
    if shape == "sweep":
        s = math.sin(2.0 * math.pi * t)
        unit = (s, 0.0) if p.sweep_axis == "pan" else (0.0, s)
    elif shape in _SHAPES:
        unit = _SHAPES[shape](t)
    else:
        raise ValueError(f"unknown movement shape {shape!r}")
    return _scale(unit, p.limits, p.size)


def movement(
    shape: str,
    bar_phase: float,
    fixture_index: int,
    fixture_count: int,
    params: MovementParams | None = None,
) -> EffectOutput:
    """Return one fixture's pan/tilt override for ``shape`` at ``bar_phase``."""
    p = _DEFAULT if params is None else params
    phase = bar_phase / p.bars + fixture_offset(fixture_index, fixture_count)
    pan, tilt = path_point(shape, phase, p)
    return EffectOutput(intensity=1.0, pan=pan, tilt=tilt)


def slew_limit(previous: Point, target: Point, max_step: float) -> Point:
    """Move from ``previous`` toward ``target`` by at most ``max_step`` (Euclidean)."""
    if max_step <= 0:
        raise ValueError(f"max_step must be > 0, got {max_step}")
    dp = target[0] - previous[0]
    dt = target[1] - previous[1]
    dist = math.hypot(dp, dt)
    if dist <= max_step:
        return target
    k = max_step / dist
    return previous[0] + dp * k, previous[1] + dt * k


SHAPES: tuple[str, ...] = ("circle", "figure8", "sweep", "ballyhoo")

__all__ = [
    "SHAPES",
    "MovementLimits",
    "MovementParams",
    "Point",
    "fixture_offset",
    "movement",
    "offsets_for",
    "path_point",
    "slew_limit",
]
