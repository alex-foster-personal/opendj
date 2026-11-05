"""One laptop's whole booth core, pure (B2BBOOTH-03..11, B2BBOOTH-90..93): contract v3 "Authority".

The applier (both engines), and on the INVITER engine the sequencer and the lease arbiter.
Intents from this laptop's page and messages from the partner's engine in; out come the messages
for the partner (``Out.ops`` on the ordered OPS lane, ``Out.ctl`` keyed for the newest-wins CTL
lane) and the events for this laptop's page (``Out.events``, pushed on the localhost stream).
No threads, no I/O, no clock of its own: every call takes ``now_us`` (this laptop's Link host
time). ``Booth`` (``booth.py``) wraps it with the lanes, the timers and the page stream; tests
drive two of these over a fake wire.

Page events (the store's ``BoothEvent``): ``op`` (every committed op, in seq order, once),
``control`` (the partner's newest value of a (unit, ctl)), ``position`` (the audio laptop's
sample), ``settings`` (with this laptop's role), ``units``, ``state``, ``refusal`` (a proposal
the inviter turned down, with the engine's reason). Partner times reach the page on THIS
laptop's clock (``PeerClock``); a value that cannot be converted yet carries ``clock: unknown``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.b2b_booth.lease import BoothState, LeaseArbiter, LeaseClock, RefusalReason, ToCommit
from apps.b2b_booth.log import Applier, PeerClock, Sequencer
from apps.b2b_booth.protocol import EPOCH_RE, UNITS, BoothError, Side, other_side, unit_of_deck_op

#: Proposal pids the inviter remembers a refusal for, so a retried POST gets the same answer.
REMEMBERED_REFUSALS = 256
_UNIT_OPS = frozenset({"grab", "refuse", "locked", "release", "onair", "setting"})


@dataclass
class Out:
    """What one call produced. ``ops`` keep their order; ``ctl`` is ``(key, message)`` newest-wins."""

    ops: list[dict[str, Any]] = field(default_factory=list)
    ctl: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    refused: RefusalReason | None = None
    #: Inviter: the seq a local op committed at. Joiner: the pid its proposal went out with.
    seq: int | None = None
    pid: int | None = None
    #: Proposal refusals for the partner (inviter, in the answer to the joiner's POST).
    answers: list[dict[str, Any]] = field(default_factory=list)
    #: Messages that answer the partner's POST itself (a snapshot, a resend's commits).
    replies: list[dict[str, Any]] = field(default_factory=list)
    #: B2BBOOTH-97: the partner announced its sealed datagram socket ``{epoch, port, sid}`` (newest wins).
    peer_udp: dict[str, Any] | None = None

    def extend(self, other: Out) -> Out:
        self.ops += other.ops
        self.ctl += other.ctl
        self.events += other.events
        self.answers += other.answers
        self.replies += other.replies
        self.peer_udp = other.peer_udp or self.peer_udp
        return self


@dataclass
class CoreCounters:
    refused: int = 0
    ctl_not_holder: int = 0
    ctl_stale: int = 0
    proposals_dup: int = 0
    clock_unknown: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


class BoothCore:
    def __init__(self, side: Side, epoch: str | None, now_us: int) -> None:
        if side == "inviter" and (epoch is None or not EPOCH_RE.fullmatch(epoch)):
            raise ValueError(f"booth: the inviter needs a 16-hex epoch, got {epoch!r}")
        self.side: Side = side
        self.state = BoothState()
        self.applier = Applier(epoch)
        self.sequencer = Sequencer(epoch) if side == "inviter" and epoch else None
        self.arbiter = LeaseArbiter(self.state, now_us) if side == "inviter" else None
        self.clock: LeaseClock = self.arbiter.clock if self.arbiter else LeaseClock()
        if self.arbiter is None:
            self.clock.seed(self.state, now_us)
        self.peer_clock = PeerClock()
        self.counters = CoreCounters()
        #: Every committed op applied this epoch, in seq order (the page snapshot replays it).
        self.history: list[dict[str, Any]] = []
        #: Newest control per ``unit|ctl`` (both directions) and position per deck, for a page snapshot.
        self.controls: dict[str, dict[str, Any]] = {}
        self.positions: dict[int, dict[str, Any]] = {}
        self._pid = 0
        #: B2BBOOTH-123 (joiner): units whose grab this laptop proposed and the log has not resolved yet;
        #: a deck op on one is proposed (the inviter holds it until the grab resolves), not pre-refused.
        self._asking: set[str] = set()
        self._cseq: dict[str, int] = {}
        self._last_peer_pid = 0
        self._refusals: dict[int, str] = {}
        self._traces: dict[int, dict[str, int]] = {}

    @property
    def epoch(self) -> str | None:
        return self.applier.epoch

    @property
    def role(self) -> str:
        return self.state.role_of(self.side)

    def holds(self, unit: str) -> bool:
        return self.state.holders[unit] == self.side

    # ------------------------------------------------------------ clock conversion

    def _local(self, peer_us: int) -> int | None:
        try:
            return self.peer_clock.to_local(peer_us)
        except ValueError:
            self.counters.clock_unknown += 1
            return None

    def _local_trace(self, trace: dict[str, int] | None) -> dict[str, int | None]:
        return {} if not trace else {k: self._local(v) for k, v in trace.items()}

    # ------------------------------------------------------------ applying

    def units_event(self) -> dict[str, Any]:
        return {"type": "units", "units": self.state.units_view(self.clock.until)}

    def settings_event(self) -> dict[str, Any]:
        return {"type": "settings", "settings": self.state.settings.as_dict(), "role": self.role}

    def _apply(self, ops: list[dict[str, Any]], now_us: int, out: Out, traces: dict[int, dict[str, Any]] | None = None) -> None:
        units_changed = settings_changed = False
        for committed in ops:
            before = dict(self.state.holders)
            self.state.apply(committed)
            # Every unit that changed hands (a grant, or a turn lock handing a half back) starts a full lease.
            for unit in UNITS:
                holder = self.state.holders[unit]
                if holder is not None and holder != before[unit]:
                    self.clock.granted(unit, now_us)
            self.history.append(committed)
            event: dict[str, Any] = {"type": "op", "committed": self._for_page(committed)}
            if traces and committed["seq"] in traces:
                event["trace"] = traces[committed["seq"]]
            out.events.append(event)
            name = committed["op"]["op"]
            if name in ("refuse", "locked", "release") or (name == "grab" and committed["op"].get("phase") == "granted"):
                self._asking.discard(committed["op"]["unit"])  # B2BBOOTH-123: that grab resolved
            units_changed |= name in _UNIT_OPS
            settings_changed |= name == "setting"
            if name == "end":
                out.events.append({"type": "state", "state": "off"})
        if settings_changed:
            out.events.append(self.settings_event())
        if units_changed:
            out.events.append(self.units_event())

    def _for_page(self, committed: dict[str, Any]) -> dict[str, Any]:
        """The log keeps the INVITER's clock; a joiner's page gets each committed time on its own clock."""
        op = committed["op"]
        if self.sequencer is not None or "at_link_us" not in op:
            return committed
        at = self._local(op["at_link_us"])
        if at is None:
            return {**committed, "clock": "unknown"}
        return {**committed, "op": {**op, "at_link_us": at}}

    def _commit(self, items: list[ToCommit], now_us: int, out: Out, trace: dict[str, int] | None = None) -> None:
        """Inviter: sequence what the arbiter decided, apply it here, send it to the joiner."""
        assert self.sequencer is not None, "only the inviter commits"
        if not items:
            return
        committed = [self.sequencer.commit(i.by, i.op, i.pid) for i in items]
        result = self.applier.offer(committed)
        stamped = {**(trace or {}), "inviter_us": now_us}
        traces = {c["seq"]: stamped for c in committed} if trace is not None else None
        self._apply(result.apply, now_us, out, traces)
        message: dict[str, Any] = {"kind": "commit", "epoch": self.epoch, "ops": committed}
        if trace is not None:
            message["traces"] = {str(c["seq"]): stamped for c in committed}
        out.ops.append(message)
        out.seq = committed[-1]["seq"]

    # ------------------------------------------------------------ intents from this laptop's page

    def intent_op(self, op: dict[str, Any], now_us: int, trace: dict[str, int] | None = None) -> Out:
        """A local discrete input. The inviter decides and commits at once; the joiner pre-checks what
        it can (instant feedback) and proposes; the inviter's answer is the truth."""
        out = Out()
        if self.arbiter is not None:
            decision = self.arbiter.propose(self.state, self.side, op, None, now_us)
            if decision.refused is not None:
                self.counters.refused += 1
                out.refused = decision.refused
            self._commit(decision.commit, now_us, out, trace)
            return out
        if self.epoch is None:
            out.refused = "not_open"
            return out
        if self.state.ended:
            out.refused = "ended"
            return out
        unit = unit_of_deck_op(op)
        # B2BBOOTH-120: a load on a deck this laptop does not hold is a grab with the load attached, so
        # only the inviter's arbiter can judge it (lease, lock, a waiting ask): it is proposed, not pre-refused.
        if (unit is not None and op["op"] != "load" and not self.holds(unit) and unit not in self._asking) or (
            op["op"] == "release" and not self.holds(op["unit"])
        ):
            self.counters.refused += 1
            out.refused = "not_holder"
            return out
        if (op["op"] == "load" and unit is not None and not self.holds(unit)) or op["op"] == "grab":
            self._asking.add(unit if unit is not None else op["unit"])
        self._pid += 1
        message: dict[str, Any] = {"kind": "propose", "epoch": self.epoch, "from": self.side, "pid": self._pid, "op": op}
        if trace is not None:
            message["trace"] = trace
        out.ops.append(message)
        out.pid = self._pid
        return out

    def intent_control(self, unit: str, ctl: str, value: float, now_us: int, trace: dict[str, int] | None = None) -> Out:
        """A local continuous input on a unit this laptop holds; refused ``not_holder`` otherwise."""
        out = Out()
        if self.epoch is None:
            out.refused = "not_open"
            return out
        if not self.holds(unit):
            self.counters.refused += 1
            out.refused = "not_holder"
            return out
        key = f"{unit}|{ctl}"
        cseq = self._cseq.get(key, 0) + 1
        self._cseq[key] = cseq
        control = {"side": self.side, "unit": unit, "ctl": ctl, "value": value, "cseq": cseq, "at_link_us": now_us}
        self.controls[key] = control
        message: dict[str, Any] = {"kind": "ctl", "epoch": self.epoch, **control}
        if trace is not None:
            message["trace"] = trace
        out.ctl.append((f"ctl:{key}", message))
        return out

    def intent_position(self, sample: dict[str, Any]) -> Out:
        """The audio laptop page's position sample (its clock is this engine's): relayed newest-wins per deck."""
        out = Out()
        if self.epoch is None:
            out.refused = "not_open"
            return out
        self.positions[sample["deck"]] = dict(sample)
        out.ctl.append((f"pos:{sample['deck']}", {"kind": "pos", "epoch": self.epoch, **sample}))
        return out

    # ------------------------------------------------------------ messages from the partner's engine

    def receive(self, envelope: dict[str, Any], now_us: int, *, reply: bool = False) -> Out:
        """One validated envelope from the partner. ``reply`` is True when it is the answer to this
        engine's own POST (a snapshot there may move a joiner to the inviter's new epoch)."""
        out = Out()
        epoch = envelope["epoch"]
        for message in envelope["messages"]:
            kind = message["kind"]
            if kind == "snapshot" and self.sequencer is None and (reply or self.epoch is None or epoch == self.epoch):
                self._adopt_snapshot(epoch, message["ops"], now_us, out)
                continue
            if kind == "resend" and self.sequencer is not None and (epoch is None or epoch != self.epoch):
                out.replies.append(self.snapshot_message())
                continue
            if not self.applier.admits(epoch):
                if kind == "propose":
                    out.answers.append({"pid": message["pid"], "refused": "stale_epoch"})
                continue
            self._receive_one(message, now_us, out)
        return out

    def _adopt_snapshot(self, epoch: str | None, ops: list[dict[str, Any]], now_us: int, out: Out) -> None:
        if epoch is None:
            raise BoothError("bad_epoch", "a snapshot must name its epoch")
        if epoch != self.epoch or self.applier.applied_seq > len(ops):
            # A new epoch: the page starts over from one snapshot event, not op by op.
            self._restart(epoch, now_us)
            self._apply(self.applier.offer(ops).apply, now_us, Out())
            out.events.append(self.page_snapshot())
            return
        self._apply(self.applier.offer(ops).apply, now_us, out)

    def _restart(self, epoch: str, now_us: int) -> None:
        """Joiner: the inviter's (new) epoch; everything applied so far belonged to the old one."""
        self.state = BoothState()
        self.applier.reset(epoch)
        self.clock = LeaseClock()
        self.clock.seed(self.state, now_us)
        self.history.clear()
        self.controls.clear()
        self.positions.clear()
        self._cseq.clear()
        self._pid = 0
        self._asking.clear()

    def _receive_one(self, message: dict[str, Any], now_us: int, out: Out) -> None:
        kind = message["kind"]
        if kind == "commit":
            self._receive_commit(message, now_us, out)
        elif kind == "snapshot":
            pass  # a pushed snapshot of another epoch, not asked for: stale, counted by admits() above
        elif kind == "resend":
            if self.sequencer is None:
                raise BoothError("not_inviter", "the joiner got a resend; only the inviter keeps the log", 409)
            ops = self.sequencer.since(message["from"])
            if ops:
                out.replies.append({"kind": "commit", "epoch": self.epoch, "ops": ops})
        elif kind == "propose":
            self._receive_proposal(message, now_us, out)
        elif kind == "renew":
            at = self._local(message["at_link_us"])
            self.clock.renew(self.state, message["side"], message["units"], now_us if at is None else at)
        elif kind == "ctl":
            self._receive_control(message, now_us, out)
        elif kind == "pos":
            self._receive_position(message, out)
        elif kind == "udp":
            out.peer_udp = {"epoch": self.epoch, "port": message["port"], "sid": message["sid"]}
        else:
            raise BoothError("bad_kind", f"unhandled kind {kind!r}")

    def _receive_commit(self, message: dict[str, Any], now_us: int, out: Out) -> None:
        if self.sequencer is not None:
            raise BoothError("not_joiner", "the inviter got a commit; only it commits", 409)
        traces = {int(k): {**self._local_trace(t), "peer_us": now_us} for k, t in message.get("traces", {}).items()}
        result = self.applier.offer(message["ops"])
        self._apply(result.apply, now_us, out, traces)
        if result.resend_from is not None:
            out.ops.append({"kind": "resend", "epoch": self.epoch, "from": result.resend_from})

    def _receive_position(self, message: dict[str, Any], out: Out) -> None:
        at = self._local(message["at_link_us"])
        position = {"deck": message["deck"], "pos_frames": message["pos_frames"], "rate": message["rate"],
                    "at_link_us": at if at is not None else message["at_link_us"]}
        if at is None:
            position["clock"] = "unknown"
        self.positions[message["deck"]] = position
        out.events.append({"type": "position", "position": position})

    def _receive_proposal(self, message: dict[str, Any], now_us: int, out: Out) -> None:
        if self.arbiter is None:
            raise BoothError("not_inviter", "the joiner got a propose; only the inviter sequences", 409)
        pid = message["pid"]
        if pid <= self._last_peer_pid:
            self.counters.proposals_dup += 1  # a retried POST: answer as the first time, commit nothing twice
            if pid in self._refusals:
                out.answers.append({"pid": pid, "refused": self._refusals[pid]})
            return
        self._last_peer_pid = pid
        op = message["op"]
        if "at_link_us" in op:
            at = self._local(op["at_link_us"])
            if at is None or at < 0:
                self._refuse_proposal(pid, "clock_unknown" if at is None else "bad_time", out)
                return
            op = {**op, "at_link_us": at}  # the log keeps the inviter's clock
        decision = self.arbiter.propose(self.state, message["from"], op, pid, now_us)
        if decision.refused is not None:
            self._refuse_proposal(pid, decision.refused, out)
        trace = None
        if "trace" in message:
            trace = {k: v for k, v in self._local_trace(message["trace"]).items() if v is not None}
        self._commit(decision.commit, now_us, out, trace)

    def _refuse_proposal(self, pid: int, reason: str, out: Out) -> None:
        self.counters.refused += 1
        self._refusals[pid] = reason
        while len(self._refusals) > REMEMBERED_REFUSALS:
            del self._refusals[min(self._refusals)]
        out.answers.append({"pid": pid, "refused": reason})

    def _receive_control(self, message: dict[str, Any], now_us: int, out: Out) -> None:
        if self.state.holders[message["unit"]] != message["side"]:
            self.counters.ctl_not_holder += 1
            return
        key = f"{message['unit']}|{message['ctl']}"
        kept = self.controls.get(key)
        if kept is not None and kept["side"] == message["side"] and kept["cseq"] >= message["cseq"]:
            self.counters.ctl_stale += 1
            return
        at = self._local(message["at_link_us"])
        control = {"side": message["side"], "unit": message["unit"], "ctl": message["ctl"], "value": message["value"],
                   "cseq": message["cseq"], "at_link_us": at if at is not None else message["at_link_us"]}
        if at is None:
            control["clock"] = "unknown"
        self.controls[key] = control
        event: dict[str, Any] = {"type": "control", "control": control}
        if "trace" in message:
            event["trace"] = {**self._local_trace(message["trace"]), "peer_us": now_us}
        out.events.append(event)

    def answer(self, answers: list[dict[str, Any]]) -> Out:
        """Joiner: the inviter's refusals of this engine's proposals, for its page."""
        out = Out()
        for item in answers:
            self.counters.refused += 1
            out.events.append({"type": "refusal", "pid": item["pid"], "reason": item["refused"]})
        return out

    # ------------------------------------------------------------ time

    def tick(self, now_us: int) -> Out:
        """Inviter: the grab fallback and asks a lock now covers."""
        out = Out()
        if self.arbiter is not None:
            self._commit(self.arbiter.tick(self.state, now_us), now_us, out)
        return out

    def renew(self, now_us: int) -> Out:
        """Every RENEW_INTERVAL_US while this laptop's page is connected: the units it holds."""
        out = Out()
        units = [u for u in UNITS if self.state.holders[u] == self.side]
        self.clock.renew(self.state, self.side, units, now_us)
        # Both sides send it: the inviter's arbiter needs the joiner's; the joiner shows the inviter's
        # lease, and either way it is the heartbeat and a clock sample.
        if self.epoch is not None:
            out.ctl.append(("renew", {"kind": "renew", "epoch": self.epoch, "side": self.side, "units": units,
                                      "at_link_us": now_us}))
        return out

    def hello(self) -> dict[str, Any]:
        """Joiner: ask the inviter for its current snapshot (the first message, and after a stale epoch).
        No epoch on purpose: the answer names the inviter's current one."""
        return {"kind": "resend", "epoch": None, "from": 1}

    def reask_gap(self) -> dict[str, Any] | None:
        start = self.applier.gap_from()
        return None if start is None or self.sequencer is not None else {"kind": "resend", "epoch": self.epoch, "from": start}

    def snapshot_message(self) -> dict[str, Any]:
        if self.sequencer is None:
            raise BoothError("not_inviter", "only the inviter has the log to snapshot", 409)
        return {"kind": "snapshot", "epoch": self.epoch, "ops": list(self.sequencer.log)}

    def decks_view(self) -> list[dict[str, Any]]:
        """B2BBOOTH-99: each deck as the committed log left it (track, playing, the last committed
        position and its time, loop), every time on THIS laptop's clock (``clock: unknown`` when a
        joiner cannot convert one yet). Track resolution is added by the engine (``Booth``)."""
        out = []
        for number in sorted(self.state.decks):
            deck = dict(self.state.decks[number])
            if self.sequencer is None and deck["at_link_us"] is not None:
                at = self._local(deck["at_link_us"])
                if at is None:
                    deck["clock"] = "unknown"
                else:
                    deck["at_link_us"] = at
            out.append(deck)
        return out

    def page_snapshot(self) -> dict[str, Any]:
        """What a (re)subscribing page starts from: every committed op of the epoch, the newest controls
        and positions, settings with role, units, and each deck's applied state (B2BBOOTH-99)."""
        return {
            "type": "snapshot",
            "epoch": self.epoch,
            "side": self.side,
            "seq": self.applier.applied_seq,
            "ops": [self._for_page(c) for c in self.history],
            "controls": list(self.controls.values()),
            "positions": list(self.positions.values()),
            "settings": self.state.settings.as_dict(),
            "role": self.role,
            "units": self.state.units_view(self.clock.until),
            "onair": self.state.onair,
            "decks": self.decks_view(),
        }

    def counter_values(self) -> dict[str, int]:
        return {**self.applier.counters.as_dict(), **self.counters.as_dict()}

    def other(self) -> Side:
        return other_side(self.side)
