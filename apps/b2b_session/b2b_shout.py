"""LINK-29..LINK-31: B2B shout bubbles, pure half (no FastAPI, no I/O).

Two DJs on two laptops message each other without talking: a preset shout ("You
take the next one") appears as a speech bubble out of the other laptop's LINK
button, answered with thumbs up, "1 sec" or thumbs down.

Transport is PULL ONLY, so the LAN door (``peer_listener``) stays read-only: each
engine appends its own shouts and replies to an in-memory OUTBOX, and the other
engine polls that outbox with ``GET /api/v1/b2b/shouts/outbox?since=<seq>``. A
reply to a shout is an outbox event of its own, read back by the sender the same
way. ``epoch`` is a per-boot id, so a restarted peer (seq back at 1) is noticed and
its cache dropped rather than silently skipped past.

``SHOUT_PRESETS`` is THE list of presets; the page and the CLI both read it from
the feed answer. Edit it here.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal

#: (id, text, attaches a track). Edit this list to change the menu.
SHOUT_PRESETS: tuple[tuple[str, str, bool], ...] = (
    ("take_next", "I'll take the next one?", False),
    ("you_next", "You take the next one", False),
    ("sick", "That was siiick", False),
    ("one_more", "One more then swap", False),
    ("mix_out", "Mix out now?", False),
    ("wait", "Wait, not yet", False),
    ("next_track", "I think this next...", True),
    # the maintainer, Sat 10 Oct 2026, 21:45 BST: more things two DJs need to say.
    ("drop_next_one", "Drop on the next 1?", False),
    ("bring_down", "Bring it down?", False),
    ("big_one_next", "Big one next", False),
    ("need_minute", "Need a minute", False),
    ("watch_levels", "Watch the levels", False),
    ("loop_this", "Loop this?", False),
    ("one_for_one", "One-for-one?", False),
)
#: LINK-104 (the maintainer, Mon 12 Oct 2026: "'That was siiick' doesn't require yes/no"): presets that
#: say something rather than ask. Their bubble has no YES / 1 SEC / NO and closes itself
#: after a 3 s countdown. Every other preset, and free text, still asks for an answer.
STATEMENT_PRESETS: frozenset[str] = frozenset({"sick", "big_one_next", "need_minute", "watch_levels"})
#: TEMPORARY (R&D capture tool, the maintainer, Sat 10 Oct 2026: "add a text entry for now for
#: capturing new things needed and we'll remove it later so we're not having to censor").
#: A free-text shout, answered like any other; no filter. Remove with LINK-94.
FREE_TEXT_ID = "free_text"
FREE_TEXT_MAX_CHARS = 80
#: LINK-86 (mock v5 V4-8) EQ hints: (id, text, deck on the RECEIVER's laptop, band). A
#: suggestion only: the receiver's bubble shows their own reading of that knob and they
#: turn it themselves. Nothing ever changes the other laptop's EQ remotely.
EQ_HINT_PRESETS: tuple[tuple[str, str, int, str], ...] = (
    ("eq_kill_low_d2", "Kill your low on deck 2?", 2, "low"),
    ("eq_kill_low_d1", "Kill your low on deck 1?", 1, "low"),
)
REPLIES: tuple[str, ...] = ("up", "wait", "down", "dismiss")
Reply = Literal["up", "wait", "down", "dismiss"]
#: Events kept in the outbox (shouts + replies); the feed shows the last HISTORY_LEN shouts.
OUTBOX_MAX = 200
HISTORY_LEN = 10
#: LINKSHOUT-01: outgoing shouts are a token bucket: this many at once, then one per SHOUT_REFILL_S.
#: A DJ sends a few in a burst at most; an agent loop cannot flood the partner's screen. Replies
#: answer shouts that already arrived, so they are not limited.
SHOUT_BURST: Final[int] = 4
SHOUT_REFILL_S: Final[float] = 3.0


class ShoutRateLimited(RuntimeError):
    """LINKSHOUT-01: too many shouts; ``retry_after_s`` says when the next one is accepted."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(
            f"Too many shouts: the next one is accepted in {retry_after_s:.1f} s"
            f" ({SHOUT_BURST} at once, then one per {SHOUT_REFILL_S:g} s)"
        )
        self.retry_after_s = retry_after_s


def free_text_out() -> dict[str, Any]:
    """What the menu needs to offer the TEMPORARY free-text shout."""
    return {"id": FREE_TEXT_ID, "max_chars": FREE_TEXT_MAX_CHARS, "temporary": True}


def clean_free_text(text: str) -> str:
    """The shout text, stripped; ValueError when empty or over FREE_TEXT_MAX_CHARS."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("free-text shout is empty")
    if len(cleaned) > FREE_TEXT_MAX_CHARS:
        raise ValueError(f"free-text shout is {len(cleaned)} characters; the limit is {FREE_TEXT_MAX_CHARS}")
    return cleaned


def needs_reply(preset_id: str) -> bool:
    """LINK-104: False for a statement preset (no answer buttons), True for everything else."""
    return preset_id not in STATEMENT_PRESETS


def presets_out() -> list[dict[str, Any]]:
    plain = [{"id": pid, "text": text, "attaches_track": attaches, "eq_hint": None, "needs_reply": needs_reply(pid)}
             for pid, text, attaches in SHOUT_PRESETS]
    hints = [{"id": pid, "text": text, "attaches_track": False, "eq_hint": {"deck": deck, "band": band}, "needs_reply": True}
             for pid, text, deck, band in EQ_HINT_PRESETS]
    return plain + hints


def preset_by_id(preset_id: str) -> tuple[str, str, bool]:
    for preset in SHOUT_PRESETS:
        if preset[0] == preset_id:
            return preset
    for pid, text, _deck, _band in EQ_HINT_PRESETS:
        if pid == preset_id:
            return pid, text, False
    known = [p[0] for p in SHOUT_PRESETS] + [p[0] for p in EQ_HINT_PRESETS]
    raise KeyError(f"unknown shout preset {preset_id!r}; known: {', '.join(known)}")


def eq_hint_for(preset_id: str) -> dict[str, Any] | None:
    """``{"deck", "band"}`` for an EQ hint preset, None for a plain one."""
    return next(({"deck": deck, "band": band} for pid, _t, deck, band in EQ_HINT_PRESETS if pid == preset_id), None)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ShoutOutbox:
    """This engine's own shouts and replies, in send order, plus local dismissals."""

    machine: str
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    events: list[dict[str, Any]] = field(default_factory=list)
    #: Outgoing shout ids whose reply bubble this side has closed (never sent to the peer).
    acked: set[str] = field(default_factory=set)
    #: The clock the shout bucket refills by (injected in tests).
    clock: Callable[[], float] = time.monotonic
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _tokens: float = float(SHOUT_BURST)
    _refilled_at: float | None = None

    def _take_shout_token(self) -> None:
        """LINKSHOUT-01: spend one token or refuse with the wait; a refused shout is never queued."""
        with self._lock:
            now = self.clock()
            if self._refilled_at is not None:
                self._tokens = min(float(SHOUT_BURST), self._tokens + (now - self._refilled_at) / SHOUT_REFILL_S)
            self._refilled_at = now
            if self._tokens < 1.0:
                raise ShoutRateLimited((1.0 - self._tokens) * SHOUT_REFILL_S)
            self._tokens -= 1.0

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            stamped = {**event, "seq": self._seq, "at_ms": _now_ms(), "from": self.machine}
            self.events.append(stamped)
            del self.events[:-OUTBOX_MAX]
            return stamped

    def shout(self, preset_id: str, track: dict[str, Any] | None, text: str | None = None) -> dict[str, Any]:
        self._validate_shout(preset_id, track, text)
        self._take_shout_token()
        return self._append_shout(preset_id, track, text)

    def _validate_shout(self, preset_id: str, track: dict[str, Any] | None, text: str | None) -> None:
        """A malformed shout is refused before it spends a token."""
        if preset_id == FREE_TEXT_ID:
            if track is not None:
                raise ValueError("a free-text shout does not carry a track")
            if text is None:
                raise ValueError("a free-text shout needs text")
            clean_free_text(text)
            return
        if text is not None:
            raise ValueError(f"preset {preset_id!r} has fixed text; send free text with preset_id {FREE_TEXT_ID!r}")
        pid, _text, attaches = preset_by_id(preset_id)
        if attaches and track is None:
            raise ValueError(f"preset {pid!r} needs a track (select or cue one first)")
        if not attaches and track is not None:
            raise ValueError(f"preset {pid!r} does not carry a track")

    def _append_shout(self, preset_id: str, track: dict[str, Any] | None, text: str | None) -> dict[str, Any]:
        """Queue a shout ``_validate_shout`` already accepted."""
        if preset_id == FREE_TEXT_ID:
            assert text is not None
            return self._append({"kind": "shout", "id": uuid.uuid4().hex[:12], "preset_id": FREE_TEXT_ID,
                                 "text": clean_free_text(text), "track": None, "eq_hint": None})
        pid, preset_text, _attaches = preset_by_id(preset_id)
        return self._append({"kind": "shout", "id": uuid.uuid4().hex[:12], "preset_id": pid, "text": preset_text,
                             "track": track, "eq_hint": eq_hint_for(pid)})

    def reply(self, shout_id: str, reply: str) -> dict[str, Any]:
        if reply not in REPLIES:
            raise ValueError(f"reply must be one of {', '.join(REPLIES)}, got {reply!r}")
        return self._append({"kind": "reply", "id": uuid.uuid4().hex[:12], "shout_id": shout_id, "reply": reply})

    def since(self, seq: int) -> dict[str, Any]:
        with self._lock:
            return {"machine": self.machine, "epoch": self.epoch, "seq": self._seq,
                    "events": [e for e in self.events if e["seq"] > seq]}

    def own_shout(self, shout_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((e for e in self.events if e["kind"] == "shout" and e["id"] == shout_id), None)

    def ack(self, shout_id: str) -> None:
        with self._lock:
            self.acked.add(shout_id)

    def acked_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self.acked)


@dataclass
class PeerCache:
    """What this engine has pulled from the peer's outbox so far."""

    epoch: str | None = None
    seq: int = 0
    machine: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    #: The page and the CLI can poll at once; absorb is a read-modify-write on seq + events.
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> tuple[str | None, list[dict[str, Any]]]:
        """``(machine, events copy)`` read under the lock."""
        with self._lock:
            return self.machine, list(self.events)

    def absorb(self, page: dict[str, Any]) -> None:
        """Fold one ``since`` page in. A new epoch (peer restarted) replaces the cache."""
        epoch, events = page.get("epoch"), page.get("events")
        if not isinstance(epoch, str) or not isinstance(events, list) or not isinstance(page.get("seq"), int):
            raise TypeError("peer outbox page has no epoch/seq/events")
        with self._lock:
            self._absorb_locked(epoch, events, page)

    def _absorb_locked(self, epoch: str, events: list[Any], page: dict[str, Any]) -> None:
        if epoch != self.epoch:
            self.epoch, self.seq, self.events = epoch, 0, []
        fresh = [e for e in events if isinstance(e, dict) and isinstance(e.get("seq"), int) and e["seq"] > self.seq]
        self.events.extend(fresh)
        del self.events[:-OUTBOX_MAX]
        self.seq = max([self.seq, *(e["seq"] for e in fresh)])
        self.machine = page.get("machine") if isinstance(page.get("machine"), str) else self.machine


def _latest_replies(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") == "reply" and isinstance(event.get("shout_id"), str):
            latest[event["shout_id"]] = event
    return latest


def _shouts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("kind") == "shout"][-HISTORY_LEN:]


def build_feed(outbox: ShoutOutbox, peer: PeerCache) -> dict[str, Any]:
    """Both directions, newest last, each shout with its answer.

    ``incoming[].open`` is True until THIS side replies or dismisses; ``outgoing[].open``
    is True while the bubble should show: unanswered, or answered and not yet closed here.
    ``needs_reply`` is decided HERE from the preset id (LINK-104), so an older peer's events
    still get it.
    """
    mine = outbox.since(0)["events"]
    acked = outbox.acked_ids()
    peer_machine, peer_events = peer.snapshot()
    my_replies, peer_replies = _latest_replies(mine), _latest_replies(peer_events)
    incoming = []
    for shout in _shouts(peer_events):
        answer = my_replies.get(shout["id"])
        incoming.append({**shout, "reply": None if answer is None else answer["reply"], "open": answer is None,
                         "needs_reply": needs_reply(str(shout.get("preset_id")))})
    outgoing = []
    for shout in _shouts(mine):
        answer = peer_replies.get(shout["id"])
        outgoing.append({**shout, "reply": None if answer is None else answer["reply"],
                         "reply_at_ms": None if answer is None else answer["at_ms"],
                         "open": shout["id"] not in acked, "needs_reply": needs_reply(str(shout.get("preset_id")))})
    return {"machine": outbox.machine, "peer_machine": peer_machine, "presets": presets_out(), "free_text": free_text_out(),
            "incoming": incoming, "outgoing": outgoing}
