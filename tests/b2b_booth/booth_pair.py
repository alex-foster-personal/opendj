"""Two pure booth cores joined by an instant wire through the strict envelope validator (JSON round
trip, exactly what a door POST carries). The lanes' timing and loss are tested on their own and on
the netns rig; this harness tests the decisions."""

from __future__ import annotations

import json
from typing import Any

from apps.b2b_booth.core import BoothCore, Out
from apps.b2b_booth.lanes import envelope
from apps.b2b_booth.protocol import Lane, validate_envelope

E1, E2 = "0123456789abcdef", "fedcba9876543210"
T0 = 10_000_000


def wire(lane: Lane, messages: list[dict[str, Any]], sent_us: int = 0) -> dict[str, Any]:
    """One envelope per epoch run, through JSON and ``validate_envelope``."""
    raw = json.dumps(envelope(messages[0].get("epoch"), sent_us, messages))
    return validate_envelope(json.loads(raw), lane)


def committed_ops(out: Out) -> list[dict[str, Any]]:
    return [e["committed"] for e in out.events if e["type"] == "op"]


class Pair:
    """``offset_us``: the joiner's Link clock minus the inviter's (both cores learn it as if from a
    zero-delay round trip), so clock conversion is exercised, not assumed away."""

    def __init__(self, now: int = T0, offset_us: int = 0) -> None:
        self.offset_us = offset_us
        self.inviter = BoothCore("inviter", E1, now)
        self.joiner = BoothCore("joiner", None, now + offset_us)
        self.inviter.peer_clock.add(now, now + offset_us, now)
        self.joiner.peer_clock.add(now + offset_us, now, now + offset_us)
        self.events: dict[str, list[dict[str, Any]]] = {"inviter": [], "joiner": []}
        self.flush("joiner", Out(ops=[self.joiner.hello()]), now)

    def core(self, side: str) -> BoothCore:
        return self.inviter if side == "inviter" else self.joiner

    def other(self, side: str) -> str:
        return "joiner" if side == "inviter" else "inviter"

    def local(self, side: str, now: int) -> int:
        """``now`` (inviter clock) on ``side``'s clock."""
        return now + (self.offset_us if side == "joiner" else 0)

    def flush(self, side: str, out: Out, now: int, applied: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Deliver everything ``side`` produced, recursively. Returns every op the INVITER applied."""
        applied = [] if applied is None else applied
        self.events[side] += out.events
        if side == "inviter":
            applied += committed_ops(out)
        to = self.other(side)
        lanes: list[tuple[Lane, list[dict[str, Any]]]] = [("ops", out.ops), ("ctl", [m for _, m in out.ctl])]
        for lane, messages in lanes:
            for message in messages:
                reply = self.core(to).receive(wire(lane, [message]), self.local(to, now))
                replies, answers = reply.replies, reply.answers
                reply.replies, reply.answers = [], []
                self.flush(to, reply, now, applied)
                if replies:
                    back = self.core(side).receive(wire("ops", replies), self.local(side, now), reply=True)
                    self.flush(side, back, now, applied)
                if answers:
                    self.flush(side, self.core(side).answer(answers), now, applied)
        return applied

    def act(self, side: str, op: dict[str, Any], now: int) -> list[dict[str, Any]]:
        return self.flush(side, self.core(side).intent_op(op, self.local(side, now)), now)

    def control(self, side: str, unit: str, ctl: str, value: float, now: int) -> Out:
        out = self.core(side).intent_control(unit, ctl, value, self.local(side, now))
        self.flush(side, out, now)
        return out

    def tick(self, now: int) -> list[dict[str, Any]]:
        return self.flush("inviter", self.inviter.tick(now), now)

    def renew(self, side: str, now: int) -> None:
        self.flush(side, self.core(side).renew(self.local(side, now)), now)


def last_op(applied: list[dict[str, Any]]) -> dict[str, Any] | None:
    return applied[-1]["op"] if applied else None


def same_booth(a: BoothCore, b: BoothCore) -> bool:
    return (a.state.holders, a.state.pending, a.state.onair, a.state.settings, a.state.decks, a.state.seq) == (
        b.state.holders, b.state.pending, b.state.onair, b.state.settings, b.state.decks, b.state.seq)
