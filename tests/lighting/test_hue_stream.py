"""LIGHT-9910: HueStream v2 packet layout, channel limits, and brightness limiter under 5 Hz.

[if] 2 channels are not a 66-byte packet, 21 channels encode, or 10 Hz brightness passes 5/s [then] fail, [else stop].
"""

from __future__ import annotations

import struct

import pytest

from apps.lighting.hue_stream import (
    CHANNEL_SIZE,
    COLOUR_SPACE_RGB,
    COLOUR_SPACE_XY,
    CONFIG_ID_SIZE,
    HEADER_SIZE,
    BrightnessLimiter,
    HueChannel,
    HueFrame,
    HueStreamBuilder,
    brightness_of,
    encode_packet,
)

pytestmark = [pytest.mark.requirement("LIGHT-9910")]

CONFIG = "12345678-9abc-def0-1234-56789abcdef0"


def _ch(channel_id: int, r: int, g: int, b: int) -> HueChannel:
    return HueChannel(channel_id, (r, g, b))


def _frame(**overrides: object) -> HueFrame:
    fields: dict[str, object] = {
        "config_id": CONFIG,
        "channels": (_ch(1, 0x1111, 0x2222, 0x3333), _ch(2, 0xAAAA, 0xBBBB, 0xCCCC)),
    }
    fields.update(overrides)
    return HueFrame(**fields)  # type: ignore[arg-type]


def _brightness_in_packet(packet: bytes, channel_id: int = 1) -> int:
    offset = HEADER_SIZE + CONFIG_ID_SIZE
    while offset + CHANNEL_SIZE <= len(packet):
        cid, r, g, b = struct.unpack_from("!BHHH", packet, offset)
        if cid == channel_id:
            return max(r, g, b)
        offset += CHANNEL_SIZE
    raise AssertionError(f"channel {channel_id} not in packet")


def test_two_channels_make_a_66_byte_packet_starting_with_huestream() -> None:
    """[if] two channels are not 16+36+14 bytes starting with HueStream [then] fail, [else stop]."""
    packet = encode_packet(_frame())
    assert len(packet) == HEADER_SIZE + CONFIG_ID_SIZE + 14
    assert packet.startswith(b"HueStream")
    assert packet[9:11] == b"\x02\x00"
    assert packet[12:14] == b"\x00\x00"
    assert packet[14] == 0
    assert packet[15] == 0
    assert packet[16:52] == CONFIG.encode()
    offset = 52
    for expected in ((1, 0x1111, 0x2222, 0x3333), (2, 0xAAAA, 0xBBBB, 0xCCCC)):
        assert struct.unpack("!BHHH", packet[offset : offset + 7]) == expected
        offset += 7


def test_sequence_and_xy_colour_space_sit_in_the_header() -> None:
    """[if] sequence 42 or XY colour space are not in the header [then] fail, [else stop]."""
    packet = encode_packet(_frame(sequence=42, colour_space=COLOUR_SPACE_XY))
    assert packet[11] == 42
    assert packet[14] == 1


def test_more_than_twenty_channels_raise_value_error() -> None:
    """[if] more than 20 channels encode without ValueError [then] fail, [else stop]."""
    many = tuple(_ch(i, 0, 0, 0) for i in range(21))
    with pytest.raises(ValueError):
        HueFrame(config_id=CONFIG, channels=many)
    builder = HueStreamBuilder(CONFIG)
    with pytest.raises(ValueError):
        builder.build(many, now=0.0)
    twenty = tuple(_ch(i, 1, 1, 1) for i in range(20))
    packet = encode_packet(HueFrame(config_id=CONFIG, channels=twenty))
    assert len(packet) == HEADER_SIZE + CONFIG_ID_SIZE + CHANNEL_SIZE * 20


@pytest.mark.parametrize(
    "make_frame",
    [
        lambda: HueFrame(config_id=CONFIG, channels=()),
        lambda: HueFrame(config_id=CONFIG, channels=(_ch(1, 0, 0, 0), _ch(1, 1, 1, 1))),
        lambda: HueFrame(config_id=CONFIG, channels=(HueChannel(256, (0, 0, 0)),)),
        lambda: HueFrame(config_id=CONFIG, channels=(HueChannel(0, (65536, 0, 0)),)),
        lambda: HueFrame(config_id="not-a-uuid", channels=(_ch(1, 0, 0, 0),)),
        lambda: HueFrame(config_id="12345678901234567890123456789012", channels=(_ch(1, 0, 0, 0),)),
        lambda: HueFrame(config_id=CONFIG, channels=(_ch(1, 0, 0, 0),), sequence=256),
        lambda: HueFrame(config_id=CONFIG, channels=(_ch(1, 0, 0, 0),), colour_space=2),
    ],
)
def test_invalid_fields_raise_value_error(make_frame: object) -> None:
    """[if] invalid frame fields construct without ValueError [then] fail, [else stop]."""
    with pytest.raises(ValueError):
        make_frame()  # type: ignore[operator]


def _count_brightness_changes(brightnesses: list[int]) -> list[tuple[float, int]]:
    """Count brightness transitions in each 1 s window [s, s+1) for s in 0..20."""
    times = [k / 10.0 for k in range(len(brightnesses))]
    windows: list[tuple[float, int]] = []
    for s in range(21):
        start, end = float(s), float(s + 1)
        changes = 0
        prev: int | None = None
        for t, b in zip(times, brightnesses, strict=True):
            if not (start <= t < end):
                continue
            if prev is not None and b != prev:
                changes += 1
            prev = b
        windows.append((start, changes))
    return windows


def test_brightness_alternating_at_10_hz_passes_at_most_5_changes_per_second() -> None:
    """[if] 10 Hz brightness alternation passes more than 5 changes in any 1 s window [then] fail, [else stop]."""
    builder = HueStreamBuilder(CONFIG)
    bright = _ch(1, 0xFFFF, 0xFFFF, 0xFFFF)
    dark = _ch(1, 0, 0, 0)
    decoded: list[int] = []
    for k in range(31):
        ch = bright if k % 2 == 0 else dark
        packet = builder.build((ch,), now=k / 10.0)
        decoded.append(_brightness_in_packet(packet))
    assert sum(1 for i in range(1, len(decoded)) if decoded[i] != decoded[i - 1]) > 0
    for _start, changes in _count_brightness_changes(decoded):
        assert changes <= 5

    limiter = BrightnessLimiter()
    passed: list[int] = []
    for k in range(31):
        target = 0xFFFF if k % 2 == 0 else 0
        if limiter.allow(1, target, k / 10.0):
            passed.append(target)
    assert len(passed) > 0
    for _start, changes in _count_brightness_changes(passed):
        assert changes <= 5


def test_colour_change_at_same_brightness_is_not_limited() -> None:
    """[if] same max RGB at 0.1 s apart is blocked [then] fail, [else stop]."""
    limiter = BrightnessLimiter()
    assert limiter.allow(1, 0xFFFF, 0.0)
    assert limiter.allow(1, 0xFFFF, 0.1)


def test_limiter_rejects_an_interval_that_allows_5_hz() -> None:
    """[if] BrightnessLimiter(0.1) constructs [then] fail, [else stop]."""
    with pytest.raises(ValueError):
        BrightnessLimiter(0.1)


def test_sequence_wraps_after_255() -> None:
    """[if] the 257th packet sequence is not 0 [then] fail, [else stop]."""
    builder = HueStreamBuilder(CONFIG)
    ch = _ch(1, 1, 1, 1)
    for _ in range(256):
        builder.build((ch,), now=0.0)
    packet = builder.build((ch,), now=0.0)
    assert packet[11] == 0


def test_brightness_of_rgb_and_xy() -> None:
    """[if] brightness_of disagrees with RGB max or XY third value [then] fail, [else stop]."""
    assert brightness_of((10, 20, 30), COLOUR_SPACE_RGB) == 30
    assert brightness_of((100, 200, 50), COLOUR_SPACE_XY) == 50
