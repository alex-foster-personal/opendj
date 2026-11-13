"""Render abstract fixture state to DMX channel values through a profile mode (LIGHT-44).

Pure functions. Rules: 16-bit channels split coarse/fine; RGBW fixtures without an
explicit white take W = min(R, G, B) and subtract it from R, G and B; a mode with no
intensity channel scales its colour channels by intensity; strobe Hz maps linearly
into the profile's strobe range (``None`` writes the open value); ``strobe_hz`` above
the flash cap raises ``ValueError``; pan and tilt map degrees across the full range.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.lighting.fixtures.profile import Channel, Mode
from apps.lighting.safety import MAX_FLASHES_PER_SECOND

RGB = tuple[int, int, int]

PAN_RANGE_DEG = 540.0
TILT_RANGE_DEG = 270.0
#: Strobe rate written at the top of a profile's strobe range.
STROBE_RANGE_MAX_HZ = 25.0

_STROBE_CAPS = ("strobe", "shutter")


@dataclass(frozen=True)
class FixtureState:
    """Abstract fixture state: what a look wants, independent of channels."""

    intensity: float = 1.0
    rgb: RGB = (0, 0, 0)
    white: float | None = None
    strobe_hz: float | None = None
    pan_deg: float = 0.0
    tilt_deg: float = 0.0


def _clamp_byte(value: float) -> int:
    return max(0, min(255, round(value)))


def _split16(fraction: float) -> tuple[int, int]:
    value = max(0, min(65535, round(max(0.0, min(1.0, fraction)) * 65535)))
    return value >> 8, value & 0xFF


def _strobe_value(channel: Channel, hz: float | None) -> int:
    open_range = next((r for r in channel.ranges if r.meaning in ("open", "off")), None)
    strobe_range = next((r for r in channel.ranges if r.meaning not in ("open", "off", "closed")), None)
    if hz is None or hz <= 0 or strobe_range is None:
        if open_range is not None:
            return open_range.end if open_range.meaning == "open" else open_range.start
        return 255 if channel.capability == "shutter" else 0
    fraction = min(1.0, hz / STROBE_RANGE_MAX_HZ)
    return _clamp_byte(strobe_range.start + fraction * (strobe_range.end - strobe_range.start))


def render_dmx(state: FixtureState, mode: Mode) -> bytes:
    """Render ``state`` to one byte per channel of ``mode``."""
    if state.strobe_hz is not None and state.strobe_hz > MAX_FLASHES_PER_SECOND:
        raise ValueError(
            f"strobe_hz {state.strobe_hz} exceeds the {MAX_FLASHES_PER_SECOND:g} per second flash cap;"
            " clamp it with the flash limiter first"
        )
    caps = set(mode.capabilities)
    intensity = max(0.0, min(1.0, state.intensity))
    red, green, blue = state.rgb
    if "white" in caps:
        if state.white is None:
            white = min(red, green, blue)
            red, green, blue = red - white, green - white, blue - white
        else:
            white = max(0.0, min(1.0, state.white)) * 255
    else:
        white = 0
    colour_scale = 1.0 if "intensity" in caps else intensity
    colours = {"red": red, "green": green, "blue": blue, "white": white}
    out = bytearray(len(mode.channels))
    for offset, channel in enumerate(mode.channels):
        cap = channel.capability
        if cap in colours:
            out[offset] = _clamp_byte(colours[cap] * colour_scale)
        elif cap == "intensity":
            out[offset] = _clamp_byte(intensity * 255)
        elif cap in _STROBE_CAPS:
            out[offset] = _strobe_value(channel, state.strobe_hz)
        elif cap in ("pan", "tilt"):
            span = PAN_RANGE_DEG if cap == "pan" else TILT_RANGE_DEG
            degrees = state.pan_deg if cap == "pan" else state.tilt_deg
            coarse, fine = _split16(degrees / span)
            out[offset] = coarse
            fine_offset = next((i for i, c in enumerate(mode.channels) if c.fine_of == offset), None)
            if fine_offset is not None:
                out[fine_offset] = fine
    return bytes(out)
