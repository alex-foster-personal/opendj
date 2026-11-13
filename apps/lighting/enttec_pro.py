"""ENTTEC DMX USB Pro widget message framing (from ENTTEC's public widget API document).

Message: 0x7E, label, data length LSB, MSB, data, 0xE7.
Label 6 sends a DMX packet (start code plus 24..512 slots), label 3 requests the widget
parameters, label 4 sets break time, MAB time and output rate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

SOM = 0x7E
EOM = 0xE7
LABEL_GET_PARAMS = 3
LABEL_SET_PARAMS = 4
LABEL_SEND_DMX = 6
MIN_SLOTS = 24
MAX_SLOTS = 512
TIME_UNIT_US = 10.67


def frame(label: int, data: bytes = b"") -> bytes:
    if not 0 <= label <= 255:
        raise ValueError("label out of range")
    if len(data) > 600:
        raise ValueError("data too long")
    return bytes([SOM, label, len(data) & 0xFF, len(data) >> 8]) + bytes(data) + bytes([EOM])


def dmx_frame(slots: bytes, start_code: int = 0) -> bytes:
    if len(slots) > MAX_SLOTS:
        raise ValueError("more than 512 slots")
    padded = bytes(slots) + bytes(max(0, MIN_SLOTS - len(slots)))
    return frame(LABEL_SEND_DMX, bytes([start_code]) + padded)


def get_params_frame(user_size: int = 0) -> bytes:
    return frame(LABEL_GET_PARAMS, bytes([user_size & 0xFF, user_size >> 8]))


@dataclass(frozen=True)
class Params:
    firmware: int
    break_time: int  # units of 10.67 us, 9..127
    mab_time: int  # units of 10.67 us, 1..127
    rate: int  # packets per second, 0..40 (0 = as fast as possible)
    user_config: bytes = b""

    @property
    def break_us(self) -> float:
        return self.break_time * TIME_UNIT_US

    @property
    def mab_us(self) -> float:
        return self.mab_time * TIME_UNIT_US


def set_params_frame(break_time: int, mab_time: int, rate: int, user_config: bytes = b"") -> bytes:
    if not 9 <= break_time <= 127:
        raise ValueError("break time out of range 9..127")
    if not 1 <= mab_time <= 127:
        raise ValueError("MAB time out of range 1..127")
    if not 0 <= rate <= 40:
        raise ValueError("rate out of range 0..40")
    n = len(user_config)
    return frame(LABEL_SET_PARAMS, bytes([n & 0xFF, n >> 8, break_time, mab_time, rate]) + user_config)


def parse_message(buf: bytes) -> tuple[int, bytes, bytes] | None:
    """Return (label, data, rest) for the first complete message in buf, or None."""
    start = buf.find(bytes([SOM]))
    if start < 0 or len(buf) - start < 5:
        return None
    label, lsb, msb = buf[start + 1], buf[start + 2], buf[start + 3]
    n = lsb | (msb << 8)
    end = start + 4 + n
    if len(buf) <= end:
        return None
    if buf[end] != EOM:
        raise ValueError("missing end of message")
    return label, bytes(buf[start + 4 : end]), bytes(buf[end + 1 :])


def parse_params_reply(message: bytes) -> Params:
    parsed = parse_message(message)
    if parsed is None:
        raise ValueError("incomplete message")
    label, data, _ = parsed
    if label != LABEL_GET_PARAMS or len(data) < 5:
        raise ValueError("not a parameters reply")
    return Params(data[0] | (data[1] << 8), data[2], data[3], data[4], data[5:])


class SerialSink(Protocol):
    def write(self, data: bytes) -> int: ...


class FdSink:
    """Writes to a file descriptor (a pty end or an opened serial device)."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view) :]
        return len(data)


class ProWriter:
    def __init__(self, sink: SerialSink) -> None:
        self.sink = sink

    def send_dmx(self, slots: bytes) -> None:
        self.sink.write(dmx_frame(slots))


def open_serial(path: str) -> FdSink:
    """Thin adapter: open a real serial device for writing. No logic."""
    return FdSink(os.open(path, os.O_RDWR | os.O_NOCTTY))
