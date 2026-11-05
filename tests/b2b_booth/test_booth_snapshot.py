"""B2BBOOTH-99: a (re)subscribing page's snapshot is enough for a mirror to rebuild every deck.

- [if] a page that subscribes after loads, plays, controls and positions does not get every committed
  op of the epoch in seq order [then] fail
- [if] the snapshot's decks do not carry the loaded track, the playing state, the last committed
  position and time, and this laptop's track resolution [then] fail
- [if] a deck with no track shows a resolution, or a resolution change is not pushed as a ``decks``
  event [then] fail
- [if] the snapshot breaks any line above [then] fail, [else stop].
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from apps.b2b_booth.booth import Booth
from tests.b2b_booth.test_booth_engine import _clock, _door, _until

pytestmark = [pytest.mark.requirement("B2BBOOTH-99")]

TRACK = {"sha256": "ab" * 32, "bytes": 1000, "title": "Pilot", "artist": "Rig", "bpm": 124.0, "key": "8A",
         "duration_s": 300.0, "owner_track_id": "rb-123"}


@pytest.fixture
def booths() -> Iterator[tuple[Booth, Booth]]:
    inviter, joiner = Booth(_clock), Booth(_clock)
    inviter.attach_transport(_door(joiner), "test")
    joiner.attach_transport(_door(inviter), "test")
    inviter.open("inviter")
    joiner.open("joiner")
    _until(lambda: inviter.view(enabled=True)["state"] == joiner.view(enabled=True)["state"] == "live", what="both live")
    try:
        yield inviter, joiner
    finally:
        inviter.shutdown()
        joiner.shutdown()


class Events:
    def __init__(self, booth: Booth) -> None:
        self.items: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        booth.subscribe(self._on)

    def _on(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.items.append(event)

    def first(self) -> dict[str, Any]:
        with self.lock:
            return self.items[0]

    def of(self, kind: str) -> list[dict[str, Any]]:
        with self.lock:
            return [e for e in self.items if e["type"] == kind]


def test_a_late_subscriber_gets_the_whole_epoch_and_every_decks_state(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    resolution = {"state": "receiving", "stable_id": None, "reason": "receiving 40%", "percent": 40}
    stray = {"state": "ready", "stable_id": "left-over", "reason": None}  # the transfer still knows deck 2
    joiner.resolve_decks_with(lambda: {1: dict(resolution), 2: dict(stray), 3: None, 4: None})
    at = _clock()
    inviter.intent({"kind": "op", "op": {"op": "load", "deck": 1, "track": TRACK}})
    inviter.intent({"kind": "op", "op": {"op": "play", "deck": 1, "at_link_us": at, "pos_frames": 4800.0}})
    joiner.intent({"kind": "control", "unit": "strip:3", "ctl": "eq.low", "value": 0.25})
    inviter.position({"deck": 1, "pos_frames": 9600.0, "at_link_us": at + 100_000, "rate": 1.0})
    _until(lambda: joiner.view(enabled=True)["seq"] == 2 and joiner.view(enabled=True)["decks"][0]["playing"], what="ops at the joiner")
    _until(lambda: any(p["deck"] == 1 for p in joiner.core.positions.values()) if joiner.core else False, what="position at the joiner")
    time.sleep(0.6)  # one deck-resolution poll
    snap = Events(joiner).first()
    assert snap["type"] == "snapshot" and snap["epoch"] == inviter.view(enabled=True)["epoch"]
    assert [(c["seq"], c["op"]["op"]) for c in snap["ops"]] == [(1, "load"), (2, "play")]
    deck1 = snap["decks"][0]
    assert deck1["deck"] == 1 and deck1["track"] == TRACK and deck1["playing"] is True and deck1["pos_frames"] == 4800.0
    assert abs(deck1["at_link_us"] - at) < 50_000, "the committed time on the joiner's own clock"
    assert deck1["resolution"] == resolution, "if the snapshot does not say how this laptop resolves the track then broken"
    assert [d["resolution"] for d in snap["decks"][1:]] == [None, None, None], "no track, no resolution (even a stray one)"
    assert any(c["unit"] == "strip:3" and c["value"] == 0.25 for c in snap["controls"])
    assert any(p["deck"] == 1 and p["pos_frames"] == 9600.0 for p in snap["positions"])
    assert snap["settings"]["audio_laptop"] == "inviter" and snap["role"] == "control"
    assert set(snap["units"]) == {"deck:1", "deck:2", "deck:3", "deck:4", "strip:1", "strip:2", "strip:3", "strip:4", "xfader", "master"}


def test_a_resolution_change_is_pushed_as_a_decks_event(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    current: dict[int, dict[str, Any] | None] = {1: {"state": "queued", "stable_id": None, "reason": "resolving"}}
    joiner.resolve_decks_with(lambda: dict(current))
    inviter.intent({"kind": "op", "op": {"op": "load", "deck": 1, "track": TRACK}})
    events = Events(joiner)
    _until(lambda: events.of("decks") and events.of("decks")[-1]["decks"][0]["resolution"] == current[1], what="first decks event")
    current[1] = {"state": "ready", "stable_id": "usb-peer-rig-a-rb-123", "reason": None}
    _until(lambda: events.of("decks")[-1]["decks"][0]["resolution"]["stable_id"] == "usb-peer-rig-a-rb-123", what="resolved")
    count = len(events.of("decks"))
    time.sleep(0.6)
    assert len(events.of("decks")) == count, "if an unchanged resolution is pushed again every poll then broken"


def test_the_off_snapshot_names_no_decks() -> None:
    lone = Booth(_clock)
    assert Events(lone).first() == {"type": "snapshot", "state": "off", "epoch": None, "side": None, "ops": [], "decks": []}
