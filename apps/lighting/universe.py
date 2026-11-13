"""DMX universe buffer, HTP and LTP layer merge, and the frame send policy (LIGHT-20).

Pure: no sockets, no packet encoding, no clock reads. Callers pass time in seconds as ``now``.

Sources (public documents only):
- ANSI E1.11 (DMX512-A): 512 slots per universe, each 0..255; a full universe refreshes at about
  44 frames per second, so 44 Hz is the send ceiling while data changes.
- Art-Net 4 and ANSI E1.31 (sACN): a node keeps its output alive when data is resent at least about
  once a second, so an unchanged universe is resent every 800 to 1000 ms.
- ANSI E1.31: a sender transmits 3 identical packets on the first send of a universe.
- HTP (highest takes precedence) merges intensity; LTP (latest takes precedence) merges colour and
  position, per common console practice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

SLOT_COUNT = 512
SLOT_MIN_VALUE = 0
SLOT_MAX_VALUE = 255
UNIVERSE_MAX_NUMBER = 63999

MAX_FRAMES_PER_SECOND = 44.0
KEEPALIVE_MIN_S = 0.8
KEEPALIVE_MAX_S = 1.0
KEEPALIVE_DEFAULT_S = 0.9
FIRST_SEND_PACKETS = 3


# ----------------------------------------------------------------------------- validation


def _require_slot(slot: int) -> None:
    if not 1 <= slot <= SLOT_COUNT:
        raise ValueError(f"DMX slot {slot} is outside 1..{SLOT_COUNT}")


def _require_value(slot: int, value: int) -> None:
    if not SLOT_MIN_VALUE <= value <= SLOT_MAX_VALUE:
        raise ValueError(f"DMX slot {slot} value {value} is outside {SLOT_MIN_VALUE}..{SLOT_MAX_VALUE}")


def _require_frame(frame: bytes) -> None:
    if len(frame) != SLOT_COUNT:
        raise ValueError(f"a frame holds {SLOT_COUNT} slots, got {len(frame)}")


# ----------------------------------------------------------------------------- universe


class Universe:
    """One DMX universe: 512 slots numbered 1..512, each 0..255, all 0 at start."""

    def __init__(self, number: int) -> None:
        if not 0 <= number <= UNIVERSE_MAX_NUMBER:
            raise ValueError(f"universe number {number} is outside 0..{UNIVERSE_MAX_NUMBER}")
        self.number = number
        self._slots = bytearray(SLOT_COUNT)

    def set(self, slot: int, value: int) -> None:
        _require_slot(slot)
        _require_value(slot, value)
        self._slots[slot - 1] = value

    def get(self, slot: int) -> int:
        _require_slot(slot)
        return self._slots[slot - 1]

    def load(self, frame: bytes) -> None:
        _require_frame(frame)
        self._slots[:] = frame

    def frame(self) -> bytes:
        return bytes(self._slots)


# ----------------------------------------------------------------------------- merge


class MergeRule(Enum):
    HTP = "htp"
    LTP = "ltp"


@dataclass(frozen=True)
class Layer:
    """One source's slot values (1-based slot to 0..255) and when it last wrote them."""

    name: str
    values: Mapping[int, int]
    timestamp: float

    def __post_init__(self) -> None:
        for slot, value in self.values.items():
            _require_slot(slot)
            _require_value(slot, value)


def merge(layers: Sequence[Layer], rules: Mapping[int, MergeRule], *, default: MergeRule) -> bytes:
    """Merge layers into one 512-slot frame.

    Each slot uses ``rules[slot]``, else ``default``. HTP keeps the highest value. LTP keeps the
    value from the layer with the latest timestamp; on a tie the layer later in ``layers`` wins.
    A slot no layer writes is 0.
    """
    for slot in rules:
        _require_slot(slot)
    frame = bytearray(SLOT_COUNT)
    ltp_stamp: dict[int, float] = {}
    for layer in layers:
        for slot, value in layer.values.items():
            index = slot - 1
            rule = rules.get(slot, default)
            if rule is MergeRule.HTP:
                frame[index] = max(frame[index], value)
            elif rule is MergeRule.LTP:
                if slot not in ltp_stamp or layer.timestamp >= ltp_stamp[slot]:
                    ltp_stamp[slot] = layer.timestamp
                    frame[index] = value
            else:
                raise ValueError(f"unhandled merge rule {rule!r}")
    return bytes(frame)


# ----------------------------------------------------------------------------- send policy


@dataclass
class SendPolicy:
    """Decides how many packets to send for a frame at time ``now``.

    First send: ``FIRST_SEND_PACKETS``. Changed data: 1 packet, at most ``max_fps`` per second.
    Unchanged data: 1 keep-alive packet every ``keepalive_s`` seconds. Otherwise 0.
    """

    max_fps: float = MAX_FRAMES_PER_SECOND
    keepalive_s: float = KEEPALIVE_DEFAULT_S
    _last_sent_at: float | None = field(default=None, init=False)
    _last_frame: bytes | None = field(default=None, init=False)
    _last_now: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not 0 < self.max_fps <= MAX_FRAMES_PER_SECOND:
            raise ValueError(f"max_fps {self.max_fps} is outside (0, {MAX_FRAMES_PER_SECOND}]")
        if not KEEPALIVE_MIN_S <= self.keepalive_s <= KEEPALIVE_MAX_S:
            raise ValueError(f"keepalive_s {self.keepalive_s} is outside {KEEPALIVE_MIN_S}..{KEEPALIVE_MAX_S}")

    def packets_due(self, now: float, frame: bytes) -> int:
        """Return how many identical packets of ``frame`` to send now, and record the send."""
        _require_frame(frame)
        if self._last_now is not None and now < self._last_now:
            raise ValueError(f"time went backwards: {now} after {self._last_now}")
        self._last_now = now
        packets = self._decide(now, frame)
        if packets:
            self._last_sent_at = now
            self._last_frame = bytes(frame)
        return packets

    def _decide(self, now: float, frame: bytes) -> int:
        if self._last_sent_at is None:
            return FIRST_SEND_PACKETS
        elapsed = now - self._last_sent_at
        if frame != self._last_frame:
            return 1 if elapsed >= 1.0 / self.max_fps else 0
        return 1 if elapsed >= self.keepalive_s else 0
