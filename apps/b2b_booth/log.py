"""Shared booth ordered log and the partner clock (B2BBOOTH-03..05), pure. Contract v3, "Session
identity and ordering" and "Clocks". Moved from the page (``booth-log.ts``, wave 1) in S1b.

- ``Sequencer`` lives on the INVITER engine only: it assigns ``seq`` (monotonic per epoch, from
  1) to every op it commits, its own and the joiner's alike, and keeps the log so it can answer
  ``resend {from}`` and hand the joiner a ``snapshot``.
- ``Applier`` lives on BOTH engines: it takes committed ops and returns them strictly in seq
  order. A gap is buffered and answered with ONE ``resend {from}``; a duplicate is dropped and
  counted; another epoch is dropped and counted (``stale_epoch``), never applied.
- ``PeerClock``: the partner engine's Link host clock minus this one's, from door round trips
  (least round trip of the last samples wins), so a peer's ``at_link_us`` is relayed to this
  laptop's page on this laptop's clock.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: The inviter keeps at most this many committed ops per epoch (a long set is a few thousand).
MAX_LOG_OPS = 50_000
#: Round trips the partner clock estimate keeps; the least round trip among them wins.
CLOCK_SAMPLES = 32


@dataclass
class LogCounters:
    stale_epoch: int = 0
    gaps: int = 0
    resends: int = 0
    duplicates: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"stale_epoch": self.stale_epoch, "gaps": self.gaps, "resends": self.resends,
                "duplicates": self.duplicates}


class Sequencer:
    def __init__(self, epoch: str) -> None:
        self.epoch = epoch
        self.log: list[dict[str, Any]] = []

    @property
    def seq(self) -> int:
        return len(self.log)

    def commit(self, by: str, op: dict[str, Any], pid: int | None) -> dict[str, Any]:
        if len(self.log) >= MAX_LOG_OPS:
            raise ValueError(f"booth: the log holds at most {MAX_LOG_OPS} ops per epoch")
        committed = {"seq": len(self.log) + 1, "by": by, "op": op, "pid": pid}
        self.log.append(committed)
        return committed

    def since(self, start: int) -> list[dict[str, Any]]:
        if start < 1:
            raise ValueError(f"booth: resend from must be >= 1, got {start}")
        return self.log[start - 1:]


@dataclass
class ApplyResult:
    apply: list[dict[str, Any]] = field(default_factory=list)
    #: Ask the inviter to resend from this seq (a gap opened), else None.
    resend_from: int | None = None


class Applier:
    """Committed ops in, the ones to apply now out, each exactly once and in seq order."""

    def __init__(self, epoch: str | None) -> None:
        self.epoch = epoch
        self.applied_seq = 0
        self.counters = LogCounters()
        self._buffered: dict[int, dict[str, Any]] = {}
        self._asked_from: int | None = None

    def reset(self, epoch: str | None) -> None:
        """A new epoch: everything applied so far belongs to the old one."""
        self.epoch = epoch
        self.applied_seq = 0
        self._buffered.clear()
        self._asked_from = None

    def admits(self, epoch: str | None) -> bool:
        if epoch is not None and epoch == self.epoch:
            return True
        self.counters.stale_epoch += 1
        return False

    def offer(self, ops: list[dict[str, Any]]) -> ApplyResult:
        for committed in ops:
            if committed["seq"] <= self.applied_seq or committed["seq"] in self._buffered:
                self.counters.duplicates += 1
                continue
            self._buffered[committed["seq"]] = committed
        return self._drain()

    def _drain(self) -> ApplyResult:
        out = ApplyResult()
        nxt = self._buffered.pop(self.applied_seq + 1, None)
        while nxt is not None:
            out.apply.append(nxt)
            self.applied_seq = nxt["seq"]
            nxt = self._buffered.pop(self.applied_seq + 1, None)
        if not self._buffered:
            self._asked_from = None
            return out
        start = self.applied_seq + 1
        if self._asked_from != start:
            self._asked_from = start
            self.counters.gaps += 1
            self.counters.resends += 1
            out.resend_from = start
        return out

    def gap_from(self) -> int | None:
        """The first missing seq while a gap is open (for a timed re-ask), else None."""
        return self.applied_seq + 1 if self._buffered else None


class PeerClock:
    """Partner Link time minus this laptop's, from round trips: ``t0`` sent, ``peer_us`` the partner's
    clock when it answered, ``t1`` the answer back. The least round trip of the last samples wins
    (its midpoint is the tightest bound); until the first sample the offset is unknown (None)."""

    def __init__(self, samples: int = CLOCK_SAMPLES) -> None:
        self._samples: deque[tuple[int, float]] = deque(maxlen=samples)

    def add(self, t0_us: int, peer_us: int, t1_us: int) -> None:
        rtt = t1_us - t0_us
        if rtt < 0:
            raise ValueError(f"booth clock: a round trip cannot be negative ({rtt} us)")
        self._samples.append((rtt, peer_us - (t0_us + t1_us) / 2))

    @property
    def offset_us(self) -> float | None:
        return None if not self._samples else min(self._samples)[1]

    @property
    def rtt_us(self) -> int | None:
        return None if not self._samples else min(self._samples)[0]

    def to_local(self, peer_us: int) -> int:
        """A partner timestamp on this laptop's clock; raises before the first sample (never guesses)."""
        offset = self.offset_us
        if offset is None:
            raise ValueError("booth clock: no round trip with the partner yet, its times cannot be converted")
        return round(peer_us - offset)
