"""Dimmer response curves and gamma-correct luminance (issue #14676, spec 06 line 107).

Curves map a normalised level 0..1 to a normalised output 0..1. DMX helpers round to
the nearest 8-bit or 16-bit step. Relative luminance reuses ``safety.relative_luminance``
(sRGB transfer, WCAG coefficients) rather than duplicating the colour math.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from apps.lighting.safety import FixtureLight, relative_luminance

GAMMA = 2.2
DMX8_MAX = 255
DMX16_MAX = 65535


def _check_level(level: float) -> float:
    if isinstance(level, bool) or not isinstance(level, int | float):
        raise ValueError(f"level must be a number, got {level!r}")  # noqa: TRY004 - issue requires ValueError
    x = float(level)
    if not math.isfinite(x) or not 0.0 <= x <= 1.0:
        raise ValueError(f"level must be finite and in 0..1, got {level!r}")
    return x


def linear(level: float) -> float:
    """Identity dimmer curve."""
    return _check_level(level)


def square_law(level: float) -> float:
    """Square-law dimmer curve (level squared)."""
    x = _check_level(level)
    return x * x


def s_curve(level: float) -> float:
    """Smoothstep S-curve dimmer response."""
    x = _check_level(level)
    return x * x * (3.0 - 2.0 * x)


def gamma22(level: float) -> float:
    """Gamma 2.2 dimmer curve."""
    return _check_level(level) ** GAMMA


CURVES: dict[str, Callable[[float], float]] = {
    "linear": linear,
    "square": square_law,
    "s_curve": s_curve,
    "gamma22": gamma22,
}


def apply_curve(name: str, level: float) -> float:
    """Apply the named curve to a 0..1 level."""
    try:
        curve = CURVES[name]
    except KeyError:
        raise ValueError(f"unknown dimmer curve {name!r}") from None
    return curve(level)


def to_dmx8(level: float) -> int:
    """Round a 0..1 level to the nearest 8-bit DMX step."""
    return min(DMX8_MAX, round(_check_level(level) * DMX8_MAX))


def to_dmx16(level: float) -> int:
    """Round a 0..1 level to the nearest 16-bit DMX step."""
    return min(DMX16_MAX, round(_check_level(level) * DMX16_MAX))


def rgb_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG relative luminance of an 8-bit sRGB triple (white 1, black 0)."""
    if len(rgb) != 3:
        raise ValueError(f"rgb must have 3 channels, got {rgb!r}")
    for c in rgb:
        if isinstance(c, float) and not math.isfinite(c):
            raise ValueError(f"rgb channel must be finite, got {c!r}")
    return relative_luminance(FixtureLight(rgb=tuple(rgb), intensity=1.0))
