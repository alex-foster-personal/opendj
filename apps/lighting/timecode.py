"""MIDI timecode and Art-Net ArtTimeCode generator carrying per-deck track time (LIGHT-31).

Pure: no ports are opened. Convention: hours = deck number (1..4); minutes, seconds and frames
are the followed deck's track position. Rates 24, 25 and 30 fps are supported.

MTC bytes follow the MIDI 1.0 MIDI Time Code definition: quarter-frame 0xF1 with eight pieces
per two frames (piece 7 carries the rate bits), and the Full Frame SysEx
``F0 7F 7F 01 01 hh mm ss ff F7`` with the rate in bits 5..6 of ``hh``. ArtTimeCode follows the
Art-Net 4 specification (OpCode 0x9700).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.lighting.artnet import ARTNET_ID, PROTOCOL_VERSION

QUARTER_FRAME = 0xF1
OPCODE_ARTTIMECODE = 0x9700
NS_PER_SECOND = 1_000_000_000
PIECES = 8

# MTC rate code and ArtTimeCode Type per supported frame rate.
RATE_CODES = {24: 0, 25: 1, 30: 3}
DEFAULT_FPS = 25


@dataclass(frozen=True, order=True)
class TimecodeMessage:
    """One MIDI timecode message and the host time it is due."""

    at_host_ns: int
    data: bytes


@dataclass(frozen=True)
class Timecode:
    """Hours (deck), minutes, seconds and frames at a frame rate."""

    hours: int
    minutes: int
    seconds: int
    frames: int
    fps: int = DEFAULT_FPS


def _rate_code(fps: float) -> int:
    if fps not in RATE_CODES:
        supported = ", ".join(str(r) for r in RATE_CODES)
        raise ValueError(f"unsupported timecode rate {fps}; supported rates: {supported} fps")
    return RATE_CODES[int(fps)]


def timecode_for(deck: int, position_s: float, fps: float = DEFAULT_FPS) -> Timecode:
    """Build the per-deck timecode for a track position in seconds."""
    _rate_code(fps)
    if not 1 <= deck <= 4:
        raise ValueError(f"deck must be 1..4, got {deck}")
    total_frames = int(round(max(position_s, 0.0) * fps, 6))
    frames = total_frames % int(fps)
    total_s = total_frames // int(fps)
    minutes = (total_s // 60) % 60
    return Timecode(deck, minutes, total_s % 60, frames, int(fps))


def full_frame(tc: Timecode) -> bytes:
    """Encode the MTC Full Frame SysEx for a timecode."""
    hh = (_rate_code(tc.fps) << 5) | (tc.hours & 0x1F)
    return bytes([0xF0, 0x7F, 0x7F, 0x01, 0x01, hh, tc.minutes, tc.seconds, tc.frames, 0xF7])


def quarter_frame(tc: Timecode, piece: int) -> bytes:
    """Encode one MTC quarter-frame message (piece 0..7) for a timecode."""
    values = (tc.frames, tc.seconds, tc.minutes, tc.hours & 0x1F)
    value = values[piece // 2]
    nibble = value & 0x0F if piece % 2 == 0 else value >> 4
    if piece == 7:
        nibble = (_rate_code(tc.fps) << 1) | (nibble & 0x01)
    return bytes([QUARTER_FRAME, (piece << 4) | (nibble & 0x0F)])


def encode_arttimecode(tc: Timecode, stream_id: int = 0) -> bytes:
    """Encode an Art-Net 4 ArtTimeCode packet for a timecode."""
    return (
        ARTNET_ID
        + OPCODE_ARTTIMECODE.to_bytes(2, "little")
        + PROTOCOL_VERSION.to_bytes(2, "big")
        + bytes([0, stream_id & 0xFF, tc.frames, tc.seconds, tc.minutes, tc.hours, _rate_code(tc.fps)])
    )


@dataclass
class MtcScheduler:
    """Schedules MTC for one followed deck; a seek or load queues a Full Frame first."""

    deck: int
    fps: int = DEFAULT_FPS
    _origin_host_ns: int = 0
    _origin_position_s: float = 0.0
    _next_piece_ns: int = 0
    _piece: int = 0
    _pending_full: bool = False
    _block: Timecode = field(default_factory=lambda: Timecode(1, 0, 0, 0))

    def __post_init__(self) -> None:
        _rate_code(self.fps)

    def seek(self, position_s: float, at_host_ns: int) -> None:
        """Jump (seek or load) to a track position; the next output starts with a Full Frame."""
        self._origin_host_ns = at_host_ns
        self._origin_position_s = position_s
        self._next_piece_ns = at_host_ns
        self._piece = 0
        self._pending_full = True

    def advance(self, until_host_ns: int) -> list[TimecodeMessage]:
        """Return messages due before ``until_host_ns``."""
        out: list[TimecodeMessage] = []
        if self._pending_full:
            tc = timecode_for(self.deck, self._origin_position_s, self.fps)
            out.append(TimecodeMessage(self._origin_host_ns, full_frame(tc)))
            self._pending_full = False
        step_ns = NS_PER_SECOND // (self.fps * 4)
        while self._next_piece_ns < until_host_ns:
            if self._piece == 0:
                elapsed_s = (self._next_piece_ns - self._origin_host_ns) / NS_PER_SECOND
                self._block = timecode_for(self.deck, self._origin_position_s + elapsed_s, self.fps)
            out.append(TimecodeMessage(self._next_piece_ns, quarter_frame(self._block, self._piece)))
            self._piece = (self._piece + 1) % PIECES
            self._next_piece_ns += step_ns
        return out
