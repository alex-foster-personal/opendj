"""Colour temperature helpers: kelvin to RGB and warm/cool white mixing.

Pure stdlib. RGB here is a float triple in 0..1 (looks.RGB is 0..255 ints).
The curve mimics the Tanner Helland approximation.
"""

from __future__ import annotations

import math

RGBf = tuple[float, float, float]

MIN_KELVIN = 1000.0
MAX_KELVIN = 40000.0


def _check_kelvin(name: str, kelvin: float) -> float:
    value = float(kelvin)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < MIN_KELVIN or value > MAX_KELVIN:
        raise ValueError(f"{name} must be within {MIN_KELVIN:g}..{MAX_KELVIN:g} K")
    return value


def _clamp_unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def kelvin_to_rgb(kelvin: float) -> RGBf:
    """Return an approximate 0..1 RGB triple for a colour temperature in kelvin."""
    t = _check_kelvin("kelvin", kelvin) / 100.0
    if t <= 66.0:
        red = 255.0
        green = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        red = 329.698727446 * ((t - 60.0) ** -0.1332047592)
        green = 288.1221695283 * ((t - 60.0) ** -0.0755148492)
    if t >= 66.0:
        blue = 255.0
    elif t <= 19.0:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(t - 10.0) - 305.0447927307
    return (
        _clamp_unit(red / 255.0),
        _clamp_unit(green / 255.0),
        _clamp_unit(blue / 255.0),
    )


def mix_white(target_kelvin: float, warm_kelvin: float, cool_kelvin: float) -> tuple[float, float]:
    """Return (warm, cool) channel levels in 0..1 that approximate target_kelvin."""
    target = _check_kelvin("target_kelvin", target_kelvin)
    warm = _check_kelvin("warm_kelvin", warm_kelvin)
    cool = _check_kelvin("cool_kelvin", cool_kelvin)
    if warm >= cool:
        raise ValueError("warm_kelvin must be below cool_kelvin")
    if target <= warm:
        return (1.0, 0.0)
    if target >= cool:
        return (0.0, 1.0)
    # Interpolate in mireds, which is closer to perceived colour spacing.
    m_target, m_warm, m_cool = 1e6 / target, 1e6 / warm, 1e6 / cool
    cool_share = (m_warm - m_target) / (m_warm - m_cool)
    cool_share = _clamp_unit(cool_share)
    return (1.0 - cool_share, cool_share)
