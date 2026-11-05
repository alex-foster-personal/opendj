"""Shared booth grab lease, turn lock and settings (B2BBOOTH-06..11), pure. Contract v3, "Grab
lease and turn lock". Moved from the page (``booth-lease.ts``, wave 1) to the engine in S1b.

- ``BoothState.apply``: the deterministic reducer BOTH engines run over the committed log, in
  seq order only (an out-of-order apply raises). Same log in, same booth out, on either laptop.
- ``LeaseArbiter``: the INVITER engine's decisions, turning a proposal (and the passage of time)
  into the ops it commits: ``locked`` at once under the turn lock, granted at once when unheld or
  lapsed, otherwise an ask; ``yield`` grants it, ``refuse`` within 1 s ends it refused, and 1 s
  of holder silence grants it.
- ``LeaseClock``: lease expiry from renewals (every 250 ms by the holder's engine while its page
  is connected).

Choices where the contract is silent (kept from wave 1, listed in the PR body):
- LEASE_US = 750 ms: a holder that missed three renews has lapsed;
- "a lock is on" means the ON AIR DJ has ``lock_half_on_turn`` on, and while it is on the
  crossfader and master belong to ON AIR too;
- when a lock comes on (turn change or setting) the locking DJ gets their locked units back at
  once, on both laptops, from the log alone;
- a refuse arriving at exactly 1 s is too late: the fallback has already granted.

B2BBOOTH-120 (Tue 20 Oct 2026, "anyone grabs any deck by default"): a ``load`` on a deck the loader
does not hold is a grab with the load attached. Granted at once (unheld or lapsed), the load commits
right after the grant in the same decision; otherwise the ask waits for the holder exactly as a grab
does, and the load commits right after a ``yield`` or the 1 s silence grant, never after a refuse or
a ``locked`` ending. The turn lock refuses it ``locked`` at once. Every other deck op still needs the
deck held.

B2BBOOTH-123 (Tue 20 Oct 2026, double-click load-and-play): any deck op this DJ sends while their own
grab of that deck waits (a load's grab, or a plain GRAB) is HELD on the ask, not refused: it commits
once, right after the grant and the load, in the order sent, with a timed op's ``at_link_us`` moved to
the grant time plus the lead it was sent with; a ``refuse`` or a ``locked`` ending drops every held op
(counted in ``held_dropped``). A holder's ``release`` while such an ask waits grants it at once.

B2BBOOTH-124 (Tue 20 Oct 2026, the transfer gap): the audio laptop's page reports ``ready {deck, seq,
pos_frames}`` for each committed load once that deck is loaded there (seq 0: nothing loaded). Once the
audio side has reported at all, a deck op on a deck whose current load it has not reported ready is held
(``awaiting``), and the matching ``ready`` commits it at ready time plus its lead; a held ``play`` or
``cue`` starts from the reported position (the audio laptop's cue point). Another load drops what was held.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from apps.b2b_booth.protocol import (
    DECKS,
    LEASE_OPS,
    UNITS,
    Role,
    Side,
    half_of_unit,
    other_side,
    unit_of_deck_op,
    units_of_half,
)

#: The holder's engine renews this often while its page is connected.
RENEW_INTERVAL_US = 250_000
#: A lease lapses this long after the holder's last renew.
LEASE_US = 750_000
#: A grab waits this long for the holder's answer, then is granted (the fallback).
GRAB_FALLBACK_US = 1_000_000

RefusalReason = Literal[
    "ended", "not_holder", "not_your_setting", "already_held", "already_asked", "no_pending_grab", "too_late",
    "not_open", "stale_epoch", "clock_unknown", "bad_time", "locked", "stale_ready", "not_audio_laptop",
]


@dataclass
class Settings:
    audio_laptop: Side = "inviter"
    two_outputs: bool = False
    lock_half_on_turn: dict[str, bool] = field(default_factory=lambda: {"inviter": False, "joiner": False})

    def as_dict(self) -> dict[str, Any]:
        return {"audio_laptop": self.audio_laptop, "two_outputs": self.two_outputs,
                "lock_half_on_turn": dict(self.lock_half_on_turn)}


def _empty_deck(deck: int) -> dict[str, Any]:
    return {"deck": deck, "track": None, "playing": False, "pos_frames": None, "at_link_us": None, "loop": None}


@dataclass
class BoothState:
    """The applied booth: ON AIR, settings, every unit's holder and pending grab, the decks."""

    onair: Side = "inviter"
    settings: Settings = field(default_factory=Settings)
    holders: dict[str, Side | None] = field(default_factory=dict)
    pending: dict[str, Side | None] = field(default_factory=dict)
    decks: dict[int, dict[str, Any]] = field(default_factory=dict)
    ended: bool = False
    #: B2BBOOTH-124: per deck, the seq of its committed load (0: none) and the side that reported it ready.
    load_seq: dict[int, int] = field(default_factory=dict)
    ready_by: dict[int, Side | None] = field(default_factory=dict)
    #: B2BBOOTH-124: the sides whose page has reported readiness this epoch (the gate applies to them only).
    ready_reporters: set[Side] = field(default_factory=set)
    seq: int = 0

    def __post_init__(self) -> None:
        for unit in UNITS:
            self.holders.setdefault(unit, half_of_unit(unit) or self.onair)
            self.pending.setdefault(unit, None)
        for deck in DECKS:
            self.decks.setdefault(deck, _empty_deck(deck))
            self.load_seq.setdefault(deck, 0)
            self.ready_by.setdefault(deck, None)

    def awaits_ready(self, deck: int) -> bool:
        """B2BBOOTH-124: the audio laptop reports readiness and has not reported this deck's current load."""
        audio = self.settings.audio_laptop
        return audio in self.ready_reporters and self.decks[deck]["track"] is not None and self.ready_by[deck] != audio

    def copy(self) -> BoothState:
        return copy.deepcopy(self)

    def lock_owner(self) -> Side | None:
        return self.onair if self.settings.lock_half_on_turn[self.onair] else None

    def locked_for(self, unit: str, grabber: Side) -> bool:
        owner = self.lock_owner()
        return owner is not None and owner != grabber and unit in (*units_of_half(owner), "xfader", "master")

    def role_of(self, side: Side) -> Role:
        if self.settings.two_outputs:
            return "audio"
        return "audio" if self.settings.audio_laptop == side else "control"

    def units_view(self, lease_until: dict[str, int | None]) -> dict[str, dict[str, Any]]:
        owner = self.lock_owner()
        out: dict[str, dict[str, Any]] = {}
        for unit in UNITS:
            locked = owner is not None and self.locked_for(unit, other_side(owner))
            out[unit] = {"holder": self.holders[unit], "pending": self.pending[unit], "locked": locked,
                         "locked_by": owner if locked else None, "lease_until_us": lease_until.get(unit)}
        return out

    def _enforce_lock(self) -> None:
        owner = self.lock_owner()
        if owner is None:
            return
        for unit in (*units_of_half(owner), "xfader", "master"):
            self.holders[unit], self.pending[unit] = owner, None

    def apply(self, committed: dict[str, Any]) -> None:
        """One committed op; ``committed['seq']`` must be the next one (the log applies in order only)."""
        if committed["seq"] != self.seq + 1:
            raise ValueError(f"booth: applying seq {committed['seq']} after {self.seq}; the log applies in order only")
        self.seq = committed["seq"]
        op, by = committed["op"], committed["by"]
        name = op["op"]
        if unit_of_deck_op(op) is not None:
            self._apply_deck(op)
            if name == "load":
                self.load_seq[op["deck"]], self.ready_by[op["deck"]] = committed["seq"], None
        elif name == "ready":  # the arbiter commits a ready only for the deck's current load
            self.ready_by[op["deck"]] = by
            self.ready_reporters.add(by)
        elif name in LEASE_OPS:
            self._apply_lease(by, op)
        elif name == "onair":
            self.onair = op["side"]
            self._enforce_lock()
        elif name == "setting":
            if "lock_half_on_turn" in op:
                self.settings.lock_half_on_turn[op["side"]] = op["lock_half_on_turn"]
            if "audio_laptop" in op:
                self.settings.audio_laptop = op["audio_laptop"]
            if "two_outputs" in op:
                self.settings.two_outputs = op["two_outputs"]
            self._enforce_lock()
        elif name == "end":
            self.ended = True
        else:
            raise ValueError(f"booth: unhandled op {name!r}")

    def _apply_lease(self, by: Side, op: dict[str, Any]) -> None:
        unit, name = op["unit"], op["op"]
        if name == "grab" and op["phase"] == "granted":
            self.holders[unit], self.pending[unit] = by, None
        elif name == "grab":
            self.pending[unit] = by
        elif name in ("refuse", "locked"):
            if self.pending[unit] == by:
                self.pending[unit] = None
        elif name == "release":
            if self.holders[unit] == by:
                self.holders[unit], self.pending[unit] = None, None
        elif name == "yield":
            pass  # the arbiter commits the grant instead; a committed yield changes nothing

    def _apply_deck(self, op: dict[str, Any]) -> None:
        deck = self.decks[op["deck"]]
        name = op["op"]
        if name == "load":
            deck.update(track=op["track"], playing=False, pos_frames=0, at_link_us=None, loop=None)
        elif name == "loop_off":
            deck["loop"] = None
        else:
            deck.update(pos_frames=op["pos_frames"], at_link_us=op["at_link_us"])
            if name == "play":
                deck["playing"] = True
            elif name in ("pause", "cue"):
                deck["playing"] = False
            elif name == "loop":
                deck["loop"] = dict(op["loop"])


# ---------------------------------------------------------------- lease clock


class LeaseClock:
    """Lease expiry per unit, from renewals. Beside the log, not in it: it is time, not order."""

    def __init__(self) -> None:
        self.until: dict[str, int] = {}

    def seed(self, state: BoothState, now_us: int) -> None:
        """Every held unit starts freshly leased (booth open, or a joiner that just took a snapshot)."""
        for unit in UNITS:
            if state.holders[unit] is not None:
                self.until[unit] = now_us + LEASE_US

    def renew(self, state: BoothState, side: Side, units: list[str], at_us: int) -> None:
        """A renewal from ``side``: only units it actually holds are extended."""
        for unit in units:
            if state.holders[unit] == side:
                self.until[unit] = max(self.until.get(unit, 0), at_us + LEASE_US)

    def granted(self, unit: str, now_us: int) -> None:
        self.until[unit] = now_us + LEASE_US

    def lease_until(self, unit: str) -> int | None:
        return self.until.get(unit)

    def lapsed(self, unit: str, now_us: int) -> bool:
        until = self.until.get(unit)
        return until is None or now_us > until


# ---------------------------------------------------------------- arbiter (inviter engine only)


@dataclass(frozen=True)
class ToCommit:
    by: Side
    op: dict[str, Any]
    pid: int | None


@dataclass(frozen=True)
class Decision:
    commit: list[ToCommit]
    refused: RefusalReason | None = None


@dataclass
class Ask:
    unit: str
    grabber: Side
    asked_us: int
    pid: int | None
    #: B2BBOOTH-120/123: what rides the grant, in order: the load it was asked for, then any deck op the
    #: grabber sent while it waited, each with the lead (us) its at_link_us had over its arrival, or None.
    then: list[tuple[ToCommit, int | None]] = field(default_factory=list)


class LeaseArbiter:
    """The inviter engine's lease and log-admission decisions. It reads the CURRENT applied state and
    never mutates it: what it returns goes through the sequencer and back through ``apply``."""

    def __init__(self, state: BoothState, now_us: int) -> None:
        self.clock = LeaseClock()
        self.clock.seed(state, now_us)
        self.asks: dict[str, Ask] = {}
        #: B2BBOOTH-123: held ops dropped because their grab ended refused or locked (or, 124, a newer load).
        self.held_dropped = 0
        #: B2BBOOTH-124: per deck, ops held until the audio laptop reports the deck's load ready (with their lead).
        self.awaiting: dict[int, list[tuple[ToCommit, int | None]]] = {}

    def _grant(self, state: BoothState, unit: str, grabber: Side, reason: str, pid: int | None, now_us: int) -> Decision:
        ask = self.asks.pop(unit, None)
        self.clock.granted(unit, now_us)
        grant = ToCommit(grabber, {"op": "grab", "unit": unit, "phase": "granted", "reason": reason}, pid)
        held = [] if ask is None else ask.then  # one ask per unit, so its grabber is the grantee
        commits = [grant]
        loaded = False
        for item, lead in held:
            if item.op["op"] == "load":
                self._drop_awaiting(item.op["deck"])
                loaded = True
                commits.append(item)
            elif loaded and state.settings.audio_laptop in state.ready_reporters:  # a fresh load is never ready yet
                self.awaiting.setdefault(item.op["deck"], []).append((item, lead))
            else:
                commits.append(_released(item, lead, now_us))
        return Decision(commits)

    def _drop(self, ask: Ask | None) -> None:
        if ask is not None:
            self.held_dropped += len(ask.then)

    def _load(self, state: BoothState, by: Side, op: dict[str, Any], pid: int | None, now_us: int) -> Decision:
        """B2BBOOTH-120: a load on a deck ``by`` does not hold grabs it first, the load riding the grant."""
        unit = f"deck:{op['deck']}"
        load = ToCommit(by, op, pid)
        if state.holders[unit] == by:
            self._drop_awaiting(op["deck"])
            return Decision([load])
        if state.locked_for(unit, by):
            return Decision([], "locked")
        if state.holders[unit] is None or self.clock.lapsed(unit, now_us):
            granted = self._grant(state, unit, by, "unheld" if state.holders[unit] is None else "lapsed", None, now_us)
            self._drop_awaiting(op["deck"])
            return Decision([*granted.commit, load])
        if unit in self.asks:
            return Decision([], "already_asked")
        self.asks[unit] = Ask(unit, by, now_us, None, then=[(load, None)])
        return Decision([ToCommit(by, {"op": "grab", "unit": unit, "phase": "ask"}, None)])

    def propose(self, state: BoothState, by: Side, op: dict[str, Any], pid: int | None, now_us: int) -> Decision:  # noqa: PLR0911 - one return per rule
        if state.ended:
            return Decision([], "ended")
        name = op["op"]
        if name == "grab":
            return self._grab(state, op["unit"], by, pid, now_us)
        if name in ("yield", "refuse"):
            return self._answer(state, by, name, op["unit"], pid, now_us)
        if name == "release":
            return self._release(state, by, op, pid, now_us)
        if name == "locked":
            return Decision([], "not_holder")  # only the arbiter decides `locked`; a proposed one is a confused peer
        if name == "setting" and "lock_half_on_turn" in op and op["side"] != by:
            return Decision([], "not_your_setting")
        if name in ("setting", "onair", "end"):
            return Decision([ToCommit(by, op, pid)])
        if name == "load":
            return self._load(state, by, op, pid, now_us)
        if name == "ready":
            return self._ready(state, by, op, pid, now_us)
        return self._deck_op(state, by, op, pid, now_us)

    def _release(self, state: BoothState, by: Side, op: dict[str, Any], pid: int | None, now_us: int) -> Decision:
        if state.holders[op["unit"]] != by:
            return Decision([], "not_holder")
        waiting = self.asks.get(op["unit"])
        if waiting is not None and waiting.then:  # B2BBOOTH-123: hand the deck to the grab that carries ops
            return Decision([ToCommit(by, op, pid), *self._grant(state, op["unit"], waiting.grabber, "unheld", waiting.pid, now_us).commit])
        self.asks.pop(op["unit"], None)
        return Decision([ToCommit(by, op, pid)])

    def _deck_op(self, state: BoothState, by: Side, op: dict[str, Any], pid: int | None, now_us: int) -> Decision:
        """A deck op from ``by``: committed when ``by`` holds the deck; HELD while ``by``'s grab of it waits
        (B2BBOOTH-123; with two sides, anyone but the holder is the ask's grabber); else ``not_holder``."""
        unit = unit_of_deck_op(op)
        if unit is None:
            return Decision([], "not_holder")
        lead = op["at_link_us"] - now_us if "at_link_us" in op else None
        if state.holders[unit] == by:
            if state.awaits_ready(op["deck"]):  # B2BBOOTH-124: held until the audio laptop has the deck loaded
                self.awaiting.setdefault(op["deck"], []).append((ToCommit(by, op, pid), lead))
                return Decision([])
            return Decision([ToCommit(by, op, pid)])
        ask = self.asks.get(unit)
        if ask is not None:
            ask.then.append((ToCommit(by, op, pid), lead))
            return Decision([])
        return Decision([], "not_holder")

    def _ready(self, state: BoothState, by: Side, op: dict[str, Any], pid: int | None, now_us: int) -> Decision:
        """B2BBOOTH-124: the audio laptop has the deck's current load: commit the report, then what waited on it,
        a held play or cue from the reported position."""
        if by != state.settings.audio_laptop:
            return Decision([], "not_audio_laptop")
        if op["seq"] != state.load_seq[op["deck"]]:
            return Decision([], "stale_ready")
        held = self.awaiting.pop(op["deck"], [])
        released = [_released(item, lead, now_us, op["pos_frames"]) for item, lead in held]
        return Decision([ToCommit(by, op, pid), *released])

    def _drop_awaiting(self, deck: int) -> None:
        self.held_dropped += len(self.awaiting.pop(deck, []))

    def _answer(self, state: BoothState, by: Side, name: str, unit: str, pid: int | None, now_us: int) -> Decision:
        """The holder's ``yield`` (grant now) or ``refuse`` (within the fallback only) of a waiting grab."""
        ask = self.asks.get(unit)
        if ask is None or state.holders[unit] != by:
            return Decision([], "no_pending_grab")
        if name == "yield":
            return self._grant(state, unit, ask.grabber, "yield", ask.pid, now_us)
        if now_us - ask.asked_us >= GRAB_FALLBACK_US:
            return Decision([], "too_late")
        self._drop(self.asks.pop(unit))
        return Decision([ToCommit(ask.grabber, {"op": "refuse", "unit": unit}, pid)])

    def _grab(self, state: BoothState, unit: str, grabber: Side, pid: int | None, now_us: int) -> Decision:
        if state.locked_for(unit, grabber):
            return Decision([ToCommit(grabber, {"op": "locked", "unit": unit}, pid)])
        holder = state.holders[unit]
        if holder == grabber:
            return Decision([], "already_held")
        if holder is None:
            return self._grant(state, unit, grabber, "unheld", pid, now_us)
        if self.clock.lapsed(unit, now_us):
            return self._grant(state, unit, grabber, "lapsed", pid, now_us)
        if unit in self.asks:
            return Decision([], "already_asked")
        self.asks[unit] = Ask(unit, grabber, now_us, pid)
        return Decision([ToCommit(grabber, {"op": "grab", "unit": unit, "phase": "ask"}, pid)])

    def tick(self, state: BoothState, now_us: int) -> list[ToCommit]:
        """Time passes: an ask a lock now covers ends locked; one whose holder stayed silent for the full
        fallback is granted."""
        out: list[ToCommit] = []
        for ask in list(self.asks.values()):
            if state.ended:
                break
            if state.locked_for(ask.unit, ask.grabber):
                self._drop(self.asks.pop(ask.unit))
                out.append(ToCommit(ask.grabber, {"op": "locked", "unit": ask.unit}, ask.pid))
            elif now_us - ask.asked_us >= GRAB_FALLBACK_US:
                out.extend(self._grant(state, ask.unit, ask.grabber, "silence", ask.pid, now_us).commit)
        return out


def _released(item: ToCommit, lead_us: int | None, now_us: int, cue_frames: float | None = None) -> ToCommit:
    """A held op as it commits: a timed op keeps its lead from now (B2BBOOTH-123); released by the audio
    laptop's ready, a play or cue starts from that deck's reported position (B2BBOOTH-124)."""
    op = dict(item.op)
    if lead_us is not None:
        op["at_link_us"] = now_us + max(0, lead_us)
    if cue_frames is not None and op["op"] in ("play", "cue"):
        op["pos_frames"] = cue_frames
    return replace(item, op=op)
