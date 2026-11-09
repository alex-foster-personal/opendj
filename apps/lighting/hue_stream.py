"""Philips Hue Entertainment (HueStream v2) packet builder, clean-room from public descriptions.

Pure bytes in, bytes out: no sockets, no DTLS, no pairing. Layout follows public third-party
descriptions of HueStream v2 because the official Philips Hue Entertainment API docs need a
developer login. Spec item L80 in
``(private path)``: transport is
DTLS-PSK on UDP port 2100 at about 25 Hz delivered; brightness changes stay under Hue's reported
5 Hz photosensitivity guidance via the per-channel limiter here (4 per second).

``apps/lighting/safety.py`` (#9892), the global flash limiter, will sit on top of this limiter
once it lands. This gate is Hue-specific and does not replace it.
"""

from __future__ import annotations

import struct
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

# ----- constants (HueStream v2, public third-party layout)

PROTOCOL_NAME = b"HueStream"
VERSION_MAJOR = 0x02
VERSION_MINOR = 0x00
COLOUR_SPACE_RGB = 0x00
COLOUR_SPACE_XY = 0x01
HEADER_SIZE = 16
CONFIG_ID_SIZE = 36
CHANNEL_SIZE = 7
MAX_CHANNELS = 20
MAX_VALUE = 0xFFFF
MAX_BRIGHTNESS_CHANGES_PER_SECOND = 4
MIN_BRIGHTNESS_INTERVAL_S = 1.0 / MAX_BRIGHTNESS_CHANGES_PER_SECOND


@dataclass(frozen=True)
class HueChannel:
    """One entertainment channel: id plus three 16-bit values (RGB or XY+brightness)."""

    channel_id: int
    values: tuple[int, int, int]

    def __post_init__(self) -> None:
        if not 0 <= self.channel_id <= 255:
            raise ValueError(f"channel_id must be 0..255, got {self.channel_id}")
        if len(self.values) != 3:
            raise ValueError(f"values must hold 3 integers, got {len(self.values)}")
        for value in self.values:
            if not 0 <= value <= MAX_VALUE:
                raise ValueError(f"each value must be 0..{MAX_VALUE}, got {value}")


@dataclass(frozen=True)
class HueFrame:
    """Validated frame fields before encoding."""

    config_id: str
    channels: tuple[HueChannel, ...]
    sequence: int = 0
    colour_space: int = COLOUR_SPACE_RGB

    def __post_init__(self) -> None:
        try:
            normal = str(uuid.UUID(self.config_id))
        except ValueError as exc:
            raise ValueError(f"config_id must be a UUID: {self.config_id!r}") from exc
        if normal != self.config_id.lower():
            raise ValueError(f"config_id must be 36-char hyphenated lowercase UUID, got {self.config_id!r}")
        n = len(self.channels)
        if n == 0:
            raise ValueError("at least one channel required")
        if n > MAX_CHANNELS:
            raise ValueError(f"at most {MAX_CHANNELS} channels, got {n}")
        seen: set[int] = set()
        for ch in self.channels:
            if ch.channel_id in seen:
                raise ValueError(f"duplicate channel id {ch.channel_id}")
            seen.add(ch.channel_id)
        if not 0 <= self.sequence <= 255:
            raise ValueError(f"sequence must be 0..255, got {self.sequence}")
        if self.colour_space not in (COLOUR_SPACE_RGB, COLOUR_SPACE_XY):
            raise ValueError(f"colour_space must be {COLOUR_SPACE_RGB} or {COLOUR_SPACE_XY}, got {self.colour_space}")


def brightness_of(values: tuple[int, int, int], colour_space: int) -> int:
    """Brightness for limiter decisions: max RGB, or the third value in XY space."""
    if colour_space == COLOUR_SPACE_XY:
        return values[2]
    return max(values)


def encode_packet(frame: HueFrame) -> bytes:
    """HueStream v2 packet: 16-byte header, 36-byte config id ASCII, then 7 bytes per channel."""
    header = PROTOCOL_NAME + struct.pack(
        "!BBBHBB",
        VERSION_MAJOR,
        VERSION_MINOR,
        frame.sequence,
        0,
        frame.colour_space,
        0,
    )
    body = frame.config_id.encode("ascii")
    for ch in frame.channels:
        body += struct.pack("!BHHH", ch.channel_id, *ch.values)
    packet = header + body
    expected = HEADER_SIZE + CONFIG_ID_SIZE + CHANNEL_SIZE * len(frame.channels)
    assert len(packet) == expected, f"encoder built {len(packet)} bytes, expected {expected}"
    return packet


class BrightnessLimiter:
    """Per-channel gate: brightness changes need MIN_BRIGHTNESS_INTERVAL_S between passes.

    Same-brightness updates (colour only) always pass. Time is an argument (seconds, monotonic).
    """

    def __init__(self, min_interval_s: float = MIN_BRIGHTNESS_INTERVAL_S) -> None:
        if min_interval_s < 0.2:
            raise ValueError(f"min_interval_s must be at least 0.2 s (under 5 Hz), got {min_interval_s}")
        self._min_interval_s = min_interval_s
        self._state: dict[int, tuple[int, float]] = {}

    def allow(self, channel_id: int, brightness: int, now: float) -> bool:
        last = self._state.get(channel_id)
        if last is None:
            self._state[channel_id] = (brightness, now)
            return True
        last_brightness, last_change_time = last
        if brightness == last_brightness:
            return True
        if now - last_change_time >= self._min_interval_s:
            self._state[channel_id] = (brightness, now)
            return True
        return False


class HueStreamBuilder:
    """Builds HueStream packets with optional per-channel brightness limiting."""

    def __init__(
        self,
        config_id: str,
        colour_space: int = COLOUR_SPACE_RGB,
        limiter: BrightnessLimiter | None = None,
    ) -> None:
        try:
            normal = str(uuid.UUID(config_id))
        except ValueError as exc:
            raise ValueError(f"config_id must be a UUID: {config_id!r}") from exc
        if normal != config_id.lower():
            raise ValueError(f"config_id must be 36-char hyphenated lowercase UUID, got {config_id!r}")
        self._config_id = normal
        self._colour_space = colour_space
        self._limiter = limiter or BrightnessLimiter()
        self._sequence = 0
        self._last_sent: dict[int, HueChannel] = {}

    def build(self, channels: Sequence[HueChannel], now: float) -> bytes:
        if len(channels) > MAX_CHANNELS:
            raise ValueError(f"at most {MAX_CHANNELS} channels, got {len(channels)}")
        out: list[HueChannel] = []
        for ch in channels:
            bright = brightness_of(ch.values, self._colour_space)
            if self._limiter.allow(ch.channel_id, bright, now):
                out.append(ch)
                self._last_sent[ch.channel_id] = ch
            else:
                out.append(self._last_sent[ch.channel_id])
        packet = encode_packet(
            HueFrame(
                config_id=self._config_id,
                channels=tuple(out),
                sequence=self._sequence,
                colour_space=self._colour_space,
            )
        )
        self._sequence = (self._sequence + 1) & 0xFF
        return packet
