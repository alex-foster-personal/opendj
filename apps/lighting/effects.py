"""LIGHT-10570: pure beat-synced effect generators (pulse, chase, colour_step, sweep, strobe).

``beat_phase`` and ``bar_phase`` are unwrapped positions since an anchor: the integer part counts
beats or bars since the anchor and the fractional part is the phase within the current beat or bar
(for example ``0.99`` means 99 % through beat 0). ``math.floor`` and Python ``%`` handle negative
values without special cases.

``EffectOutput.intensity`` is a multiplier in 0..1 applied to the look's intensity (never HTP).
Colour and pan/tilt on the output, when set, replace the look's values (LTP). A compositor
(#9893) will call ``compose()`` to merge look and effect per fixture.

Strobe rate is in Hz but inputs are beats; ``bpm`` converts beat phase to seconds. The strobe
expression never reads ``fixture_index``, so every fixture flashes at the same instants by
construction. The hard 3 Hz flash limiter lives in ``safety.py`` (#9892) and runs after the
compositor, not here.

Standard library only; no clock reads.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

RGB = tuple[float, float, float]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _validate_rgb_channels(colour: RGB, *, field: str = "colour") -> None:
    for i, ch in enumerate(colour):
        if not 0.0 <= ch <= 1.0:
            raise ValueError(f"{field}[{i}] must be in 0..1, got {ch}")


def _check_fixture(fixture_index: int, fixture_count: int) -> None:
    if fixture_count < 1:
        raise ValueError(f"fixture_count must be >= 1, got {fixture_count}")
    if not 0 <= fixture_index < fixture_count:
        raise ValueError(f"fixture_index must be in 0..{fixture_count - 1}, got {fixture_index}")


@dataclass(frozen=True)
class EffectOutput:
    """One fixture's effect contribution: intensity multiplier plus optional LTP overrides."""

    intensity: float = 1.0
    colour: RGB | None = None
    pan: float | None = None
    tilt: float | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.intensity <= 1.0:
            raise ValueError(f"intensity must be in 0..1, got {self.intensity}")
        if self.colour is not None:
            _validate_rgb_channels(self.colour)


@dataclass(frozen=True)
class LookValues:
    """Stand-in for one fixture's look state until looks.py (#9890) lands."""

    intensity: float
    colour: RGB | None = None
    pan: float | None = None
    tilt: float | None = None


def compose(look: LookValues, out: EffectOutput) -> LookValues:
    """Merge look and effect: multiply intensity; LTP colour and position from effect when set."""
    return LookValues(
        intensity=look.intensity * out.intensity,
        colour=out.colour if out.colour is not None else look.colour,
        pan=out.pan if out.pan is not None else look.pan,
        tilt=out.tilt if out.tilt is not None else look.tilt,
    )


@dataclass(frozen=True)
class PulseParams:
    decay: float = 0.15
    length: float = 0.5

    def __post_init__(self) -> None:
        if self.decay <= 0:
            raise ValueError(f"decay must be > 0, got {self.decay}")
        if not 0 < self.length <= 1:
            raise ValueError(f"length must be in (0, 1], got {self.length}")


@dataclass(frozen=True)
class ChaseParams:
    direction: Literal["forward", "reverse"] = "forward"
    width: int = 1

    def __post_init__(self) -> None:
        if self.direction not in ("forward", "reverse"):
            raise ValueError(f"direction must be forward or reverse, got {self.direction!r}")
        if self.width < 1:
            raise ValueError(f"width must be >= 1, got {self.width}")


@dataclass(frozen=True)
class ColourStepParams:
    palette: tuple[RGB, ...] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    every_bars: int = 1

    def __post_init__(self) -> None:
        if not self.palette:
            raise ValueError("palette must not be empty")
        for i, colour in enumerate(self.palette):
            _validate_rgb_channels(colour, field=f"palette[{i}]")
        if self.every_bars < 1:
            raise ValueError(f"every_bars must be >= 1, got {self.every_bars}")


@dataclass(frozen=True)
class SweepParams:
    axis: Literal["pan", "tilt"] = "pan"
    bars: float = 4.0
    centre_deg: float = 0.0
    amplitude_deg: float = 45.0
    offset: float = 0.0

    def __post_init__(self) -> None:
        if self.axis not in ("pan", "tilt"):
            raise ValueError(f"axis must be pan or tilt, got {self.axis!r}")
        if self.bars <= 0:
            raise ValueError(f"bars must be > 0, got {self.bars}")
        if self.amplitude_deg < 0:
            raise ValueError(f"amplitude_deg must be >= 0, got {self.amplitude_deg}")


@dataclass(frozen=True)
class StrobeParams:
    rate_hz: float = 3.0
    bpm: float = 120.0
    duty: float = 0.5

    def __post_init__(self) -> None:
        if self.rate_hz <= 0:
            raise ValueError(f"rate_hz must be > 0, got {self.rate_hz}")
        if self.bpm <= 0:
            raise ValueError(f"bpm must be > 0, got {self.bpm}")
        if not 0 < self.duty < 1:
            raise ValueError(f"duty must be in (0, 1), got {self.duty}")


_PULSE_DEFAULT = PulseParams()
_CHASE_DEFAULT = ChaseParams()
_COLOUR_STEP_DEFAULT = ColourStepParams()
_SWEEP_DEFAULT = SweepParams()
_STROBE_DEFAULT = StrobeParams()


def pulse(
    beat_phase: float,
    bar_phase: float,
    fixture_index: int,
    fixture_count: int,
    params: PulseParams | None = None,
) -> EffectOutput:
    del bar_phase
    _check_fixture(fixture_index, fixture_count)
    p = _PULSE_DEFAULT if params is None else params
    within = beat_phase - math.floor(beat_phase)
    if within >= p.length:
        intensity = 0.0
    else:
        tail = math.exp(-p.length / p.decay)
        intensity = (math.exp(-within / p.decay) - tail) / (1.0 - tail)
        intensity = _clamp01(intensity)
    return EffectOutput(intensity=intensity)


def chase(
    beat_phase: float,
    bar_phase: float,
    fixture_index: int,
    fixture_count: int,
    params: ChaseParams | None = None,
) -> EffectOutput:
    del bar_phase
    _check_fixture(fixture_index, fixture_count)
    p = _CHASE_DEFAULT if params is None else params
    beat = math.floor(beat_phase)
    n = fixture_count
    beat_mod = beat % n
    if p.direction == "forward":
        head = beat_mod
        width = min(p.width, n)
        lit = {(head + k) % n for k in range(width)}
    else:
        head = (n - 1) - beat_mod
        width = min(p.width, n)
        lit = {(head - k) % n for k in range(width)}
    intensity = 1.0 if fixture_index in lit else 0.0
    return EffectOutput(intensity=intensity)


def colour_step(
    beat_phase: float,
    bar_phase: float,
    fixture_index: int,
    fixture_count: int,
    params: ColourStepParams | None = None,
) -> EffectOutput:
    del beat_phase
    _check_fixture(fixture_index, fixture_count)
    p = _COLOUR_STEP_DEFAULT if params is None else params
    step = math.floor(bar_phase / p.every_bars)
    colour = p.palette[step % len(p.palette)]
    return EffectOutput(intensity=1.0, colour=colour)


def sweep(
    beat_phase: float,
    bar_phase: float,
    fixture_index: int,
    fixture_count: int,
    params: SweepParams | None = None,
) -> EffectOutput:
    del beat_phase
    _check_fixture(fixture_index, fixture_count)
    p = _SWEEP_DEFAULT if params is None else params
    angle = p.centre_deg + p.amplitude_deg * math.sin(2.0 * math.pi * (bar_phase / p.bars + fixture_index * p.offset))
    if p.axis == "pan":
        return EffectOutput(intensity=1.0, pan=angle)
    return EffectOutput(intensity=1.0, tilt=angle)


def strobe(
    beat_phase: float,
    bar_phase: float,
    fixture_index: int,
    fixture_count: int,
    params: StrobeParams | None = None,
) -> EffectOutput:
    del bar_phase
    _check_fixture(fixture_index, fixture_count)
    p = _STROBE_DEFAULT if params is None else params
    seconds = beat_phase * 60.0 / p.bpm
    phase = seconds * p.rate_hz
    frac = phase - math.floor(phase)
    on = frac < p.duty
    return EffectOutput(intensity=1.0 if on else 0.0)


class Effect(Protocol):
    def __call__(
        self,
        beat_phase: float,
        bar_phase: float,
        fixture_index: int,
        fixture_count: int,
        params: Any = None,
    ) -> EffectOutput: ...


EFFECTS: Mapping[str, Effect] = MappingProxyType(
    {
        "pulse": pulse,
        "chase": chase,
        "colour_step": colour_step,
        "sweep": sweep,
        "strobe": strobe,
    }
)

__all__ = [
    "EFFECTS",
    "MOVEMENT_AMPLITUDE",
    "MOVEMENT_SWEEP_BARS",
    "RGB",
    "ChaseParams",
    "ColourStepParams",
    "Effect",
    "EffectOutput",
    "LookValues",
    "PulseParams",
    "StrobeParams",
    "SweepParams",
    "chase",
    "colour_step",
    "compose",
    "movement_sweep",
    "pulse",
    "strobe",
    "sweep",
]


# ----------------------------------------------------------------------------- movement variant (#10107)

MOVEMENT_AMPLITUDE: Mapping[str, float] = MappingProxyType({"small": 0.20, "wide": 0.60})
MOVEMENT_SWEEP_BARS: Mapping[str, float] = MappingProxyType({"small": 2.0, "wide": 1.0})


def movement_sweep(
    movement: str, *, range_deg: float, axis: Literal["pan", "tilt"] = "pan", centre_deg: float = 0.0
) -> SweepParams:
    """Sweep params for a ``lighting.movement`` value: amplitude as a share of ``range_deg`` and period."""
    if movement not in MOVEMENT_AMPLITUDE:
        raise ValueError(f"movement must be one of {', '.join(MOVEMENT_AMPLITUDE)}, got {movement!r}")
    return SweepParams(
        axis=axis,
        bars=MOVEMENT_SWEEP_BARS[movement],
        centre_deg=centre_deg,
        amplitude_deg=MOVEMENT_AMPLITUDE[movement] * range_deg,
    )
