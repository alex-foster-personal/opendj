"""Colour conversion helpers for fixtures: RGB to RGBW, CMY and HSV (issue #14511, spec 05 section 3).

Pure: no I/O. Channels are unit floats in 0.0..1.0; hue is degrees in [0, 360).
Errors raise ``LookError`` (a ``ValueError``) so callers of ``apps.lighting.looks`` handle one type.
"""

from __future__ import annotations

import colorsys
import math

from apps.lighting.looks import LookError

__all__ = [
    "cmy_to_rgb",
    "hsv_to_rgb",
    "rgb_to_cmy",
    "rgb_to_hsv",
    "rgb_to_rgbw",
]

Unit3 = tuple[float, float, float]


def _unit(name: str, value: float) -> float:
    v = float(value)
    if not math.isfinite(v) or not 0.0 <= v <= 1.0:
        raise LookError(f"{name} {value!r} must be finite and within 0.0..1.0")
    return v


def _unit3(names: str, values: Unit3) -> Unit3:
    if len(values) != 3:
        raise LookError(f"expected 3 channels ({names}), got {len(values)}")
    a, b, c = (_unit(n, v) for n, v in zip(names, values, strict=True))
    return a, b, c


def rgb_to_rgbw(rgb: Unit3) -> tuple[float, float, float, float]:
    """Split RGB into RGBW by moving the common (min) component to the white channel."""
    r, g, b = _unit3("rgb", rgb)
    w = min(r, g, b)
    return r - w, g - w, b - w, w


def rgb_to_cmy(rgb: Unit3) -> Unit3:
    """Convert RGB to subtractive CMY (c = 1 - r)."""
    r, g, b = _unit3("rgb", rgb)
    return 1.0 - r, 1.0 - g, 1.0 - b


def cmy_to_rgb(cmy: Unit3) -> Unit3:
    """Convert subtractive CMY back to RGB."""
    c, m, y = _unit3("cmy", cmy)
    return 1.0 - c, 1.0 - m, 1.0 - y


def rgb_to_hsv(rgb: Unit3) -> Unit3:
    """Convert RGB to (hue degrees in [0, 360), saturation, value)."""
    h, s, v = colorsys.rgb_to_hsv(*_unit3("rgb", rgb))
    return (h * 360.0) % 360.0, s, v


def hsv_to_rgb(hsv: Unit3) -> Unit3:
    """Convert (hue degrees, saturation, value) to RGB; hue wraps modulo 360."""
    if len(hsv) != 3:
        raise LookError(f"expected 3 channels (hsv), got {len(hsv)}")
    hue = float(hsv[0])
    if not math.isfinite(hue):
        raise LookError(f"hue {hsv[0]!r} must be finite")
    s = _unit("s", hsv[1])
    v = _unit("v", hsv[2])
    return colorsys.hsv_to_rgb((hue % 360.0) / 360.0, s, v)
