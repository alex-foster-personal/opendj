"""B2BBOOTH-97: the booth's datagram lane policy, pure (no socket, no clock of its own).

Contract v3 "Engine-to-engine channel", the documented fallback, decided by measurement (Thu 15 Oct
2026, ADR-0139-b2b-booth-datagram-lane): on a LAN pairing, continuous controls, position samples and
renews travel engine to engine as sealed UDP datagrams (``apps.b2b_transport.datagram``, wired in by
the web UI layer: ``apps.b2b_booth`` imports no transport), newest wins per key, never retransmitted.
Discrete ops stay on the reliable door OPS lane.

- ``DatagramLink`` decides which lane carries the CTL traffic. The datagram lane is active only
  while its own probe proves BOTH directions work: a ``ping`` this engine sent came back as a
  ``pong`` within ``DGRAM_STALE_S``. Until the first pong (the probe at booth open), and whenever
  pongs stop (UDP blocked, a firewall, the partner gone), the door CTL lane carries everything.
- ``NewestFilter`` is the receiver's newest-wins rule: per key (``ctl:<unit>|<ctl>``, ``pos:<deck>``,
  ``renew``) a message from an OLDER datagram (lower authenticated counter) than one already applied
  is dropped and counted, so a reordered datagram never moves a control or a playhead backwards.
- A lost datagram is never retransmitted; instead the newest value of a control is REPEATED after
  ``REPEAT_AFTER_S`` unless a newer one replaced it (the end of a fader move survives one loss; the
  receiver's ``cseq`` check drops the copy that arrives second).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CtlLaneName = Literal["datagram", "door"]

#: Probe pings while the datagram path is unproven, then steady pings (each pong is a clock sample).
PROBE_PING_S = 0.1
PING_S = 0.25
#: No pong for this long and the CTL traffic moves back to the door lane.
DGRAM_STALE_S = 1.0
#: The door re-announces this engine's datagram socket this often (a partner that re-opened learns it).
ADVERT_S = 2.0
#: A control's newest value is sent again this long after it was first sent, unless replaced.
REPEAT_AFTER_S = (0.04, 0.2)


def message_key(message: dict[str, Any]) -> str:
    kind = message["kind"]
    if kind == "ctl":
        return f"ctl:{message['unit']}|{message['ctl']}"
    if kind == "pos":
        return f"pos:{message['deck']}"
    if kind == "renew":
        return f"renew:{message['side']}"
    raise ValueError(f"booth datagram: {kind!r} has no newest-wins key")


@dataclass
class DatagramLink:
    """Which lane carries CTL traffic, from the datagram path's own probe. ``now`` is monotonic seconds."""

    available: bool = False
    #: Why the datagram lane is not active (None while it is).
    reason: str | None = "no datagram channel (an internet pairing, or no pairing token)"
    active: CtlLaneName = "door"
    last_pong: float | None = None
    last_ping: float | None = None
    rtt_us: int | None = None
    pings: int = 0
    pongs: int = 0
    switches: int = 0

    def reset(self, available: bool, reason: str | None = None) -> None:
        self.available = available
        self.active, self.last_pong, self.last_ping, self.rtt_us = "door", None, None, None
        self.reason = reason if not available else "probing: no pong yet"

    def ping_due(self, now: float, peer_known: bool) -> bool:
        if not self.available or not peer_known:
            return False
        interval = PING_S if self.last_pong is not None else PROBE_PING_S
        if self.last_ping is not None and now - self.last_ping < interval:
            return False
        self.last_ping = now
        self.pings += 1
        return True

    def on_pong(self, now: float, rtt_us: int) -> None:
        self.last_pong = now
        self.rtt_us = rtt_us
        self.pongs += 1

    def update(self, now: float, peer_known: bool) -> CtlLaneName | None:
        """Recompute the active lane; returns the new lane when it changed, else None."""
        if not self.available:
            wanted: CtlLaneName = "door"
        elif not peer_known:
            wanted, self.reason = "door", "waiting for the partner's datagram socket (announced over the door)"
        elif self.last_pong is None:
            wanted, self.reason = "door", "probing: no pong yet"
        elif now - self.last_pong > DGRAM_STALE_S:
            wanted = "door"
            self.reason = f"no pong for {now - self.last_pong:.1f} s (UDP blocked or lost): the door carries the controls"
        else:
            wanted, self.reason = "datagram", None
        if wanted == self.active:
            return None
        self.active = wanted
        self.switches += 1
        return wanted

    def status(self, now: float) -> dict[str, Any]:
        return {"active": self.active, "reason": self.reason, "pings": self.pings, "pongs": self.pongs,
                "switches": self.switches, "rtt_ms": None if self.rtt_us is None else round(self.rtt_us / 1000, 2),
                "pong_age_s": None if self.last_pong is None else round(now - self.last_pong, 3)}


@dataclass
class NewestFilter:
    """Receiver side: per key, only messages from a datagram NEWER than the newest one applied."""

    newest: dict[str, int] = field(default_factory=dict)
    stale: int = 0

    def admit(self, key: str, counter: int) -> bool:
        kept = self.newest.get(key)
        if kept is not None and counter <= kept:
            self.stale += 1
            return False
        self.newest[key] = counter
        return True

    def reset(self) -> None:
        self.newest.clear()


@dataclass
class Repeats:
    """Sender side: the newest value of each control, due to be sent again at ``REPEAT_AFTER_S``."""

    due: dict[str, tuple[dict[str, Any], list[float]]] = field(default_factory=dict)
    sent: int = 0

    def schedule(self, key: str, message: dict[str, Any], now: float) -> None:
        if key.startswith("ctl:"):
            self.due[key] = (message, [now + delay for delay in REPEAT_AFTER_S])

    def take(self, now: float) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for key in list(self.due):
            message, times = self.due[key]
            if times and times[0] <= now:
                times.pop(0)
                out.append((key, message))
                self.sent += 1
            if not times:
                del self.due[key]
        return out

    def clear(self) -> None:
        self.due.clear()
