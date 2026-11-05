"""B2BBOOTH-16, B2BBOOTH-19, B2BBOOTH-93..95: two engine booths, their real lanes and timers, joined
through each other's ``door_post`` (the paired door minus the socket; the netns rig adds the socket).

- [if] the joiner does not reach live with the inviter's epoch [then] fail
- [if] an intent on either laptop does not reach BOTH page streams once, in seq order [then] fail
- [if] a page that resubscribes (a reload) gets another epoch or a different booth [then] fail
- [if] a control from one laptop's page is not pushed to the other laptop's page [then] fail
- [if] leases renew while no page is connected, or stop renewing while one is [then] fail
- [if] DELETE does not end the booth on BOTH laptops [then] fail
- [if] two engine booths disagree on any line above [then] fail, [else stop].
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from apps.b2b_booth.booth import Booth
from apps.b2b_booth.protocol import BoothError

pytestmark = [pytest.mark.requirement(r) for r in ("B2BBOOTH-16", "B2BBOOTH-19", "B2BBOOTH-93", "B2BBOOTH-94", "B2BBOOTH-95")]


def _clock() -> int:
    return time.monotonic_ns() // 1000


def _door(target: Booth) -> Any:
    def post(lane: str, envelope: dict[str, Any], timeout: float) -> dict[str, Any]:
        status, body = target.door_post(lane, json.dumps(envelope).encode())
        if status != 200:
            raise ConnectionError(f"door answered {status}: {body}")
        return body

    return post


class Page:
    """A page's stream: every event in order, thread-safe."""

    def __init__(self, booth: Booth) -> None:
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.off = booth.subscribe(self._on)

    def _on(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.events.append(event)

    def of(self, kind: str) -> list[dict[str, Any]]:
        with self.lock:
            return [e for e in self.events if e["type"] == kind]

    def ops(self) -> list[dict[str, Any]]:
        with self.lock:
            out: list[dict[str, Any]] = []
            for e in self.events:
                if e["type"] == "snapshot":
                    out = list(e.get("ops", []))
                elif e["type"] == "op":
                    out.append(e["committed"])
            return out


def _until(predicate: Any, timeout: float = 5.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


@pytest.fixture
def booths() -> Iterator[tuple[Booth, Booth]]:
    inviter, joiner = Booth(_clock), Booth(_clock)
    inviter.attach_transport(_door(joiner), "test")
    joiner.attach_transport(_door(inviter), "test")
    try:
        yield inviter, joiner
    finally:
        inviter.shutdown()
        joiner.shutdown()


def _open(inviter: Booth, joiner: Booth) -> None:
    inviter.open("inviter")
    joiner.open("joiner")
    _until(lambda: inviter.view(enabled=True)["state"] == joiner.view(enabled=True)["state"] == "live", what="both live")


def test_open_without_a_partner_is_refused_with_a_reason() -> None:
    lone = Booth(_clock)
    with pytest.raises(BoothError) as err:
        lone.open("inviter")
    assert err.value.code == "no_peer"


def test_both_go_live_on_the_inviters_epoch(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    _open(inviter, joiner)
    a, b = inviter.view(enabled=True), joiner.view(enabled=True)
    assert a["epoch"] == b["epoch"] and len(a["epoch"]) == 16
    assert (a["role"], b["role"]) == ("audio", "control")
    assert b["peer_clock"]["offset_us"] is not None and abs(b["peer_clock"]["offset_us"]) < 50_000


def test_intents_reach_both_pages_once_in_order(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    pa, pb = Page(inviter), Page(joiner)
    _open(inviter, joiner)
    for i in range(40):
        side, deck = (joiner, 3) if i % 2 else (inviter, 1)
        side.intent({"kind": "op", "op": {"op": "play", "deck": deck, "at_link_us": _clock(), "pos_frames": i}, "t_page_us": _clock()})
    _until(lambda: len(pa.ops()) == 40 and len(pb.ops()) == 40, what="40 ops on both pages")
    assert [c["seq"] for c in pa.ops()] == list(range(1, 41))
    assert [(c["seq"], c["by"]) for c in pb.ops()] == [(c["seq"], c["by"]) for c in pa.ops()]
    traces = [e["trace"] for e in pa.of("op") if e["committed"]["by"] == "joiner"]
    assert traces and all({"page_us", "engine_us", "inviter_us"} <= set(t) for t in traces), "every hop stamped"


def test_a_refused_intent_names_the_engines_reason(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    pb = Page(joiner)
    _open(inviter, joiner)
    with pytest.raises(BoothError) as err:
        joiner.intent({"kind": "op", "op": {"op": "play", "deck": 1, "at_link_us": 1, "pos_frames": 0}})
    assert err.value.code == "not_holder" and err.value.status == 409
    joiner.intent({"kind": "op", "op": {"op": "setting", "side": "inviter", "lock_half_on_turn": True}})
    _until(lambda: pb.of("refusal"), what="the inviter's refusal on the joiner's page")
    assert pb.of("refusal")[-1]["reason"] == "not_your_setting"


def test_a_reload_keeps_the_epoch_and_starts_from_the_same_booth(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    first = Page(joiner)
    _open(inviter, joiner)
    joiner.intent({"kind": "op", "op": {"op": "setting", "side": "joiner", "lock_half_on_turn": True}})
    inviter.intent({"kind": "control", "unit": "strip:1", "ctl": "eq.low", "value": 0.25})
    _until(lambda: len(first.ops()) == 1 and first.of("control"), what="setting and control")
    epoch = joiner.view(enabled=True)["epoch"]
    first.off()  # the page reloads
    second = Page(joiner)
    snapshot = second.events[0]
    assert snapshot["type"] == "snapshot" and snapshot["epoch"] == epoch and snapshot["state"] == "live"
    assert [c["op"]["op"] for c in snapshot["ops"]] == ["setting"]
    assert snapshot["settings"]["lock_half_on_turn"]["joiner"] is True
    assert [(c["ctl"], c["value"]) for c in snapshot["controls"]] == [("eq.low", 0.25)]
    assert joiner.view(enabled=True)["epoch"] == epoch, "negative control: nothing re-opened"


def test_controls_cross_to_the_other_page_with_every_hop_stamped(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    pa = Page(inviter)
    _open(inviter, joiner)
    for value in (0.1, 0.2, 0.3):
        joiner.intent({"kind": "control", "unit": "strip:3", "ctl": "fader", "value": value, "t_page_us": _clock()})
    _until(lambda: pa.of("control") and pa.of("control")[-1]["control"]["value"] == 0.3, what="the newest fader value")
    last = pa.of("control")[-1]
    assert {"page_us", "engine_us", "peer_us"} <= set(last["trace"])
    assert last["trace"]["page_us"] <= last["trace"]["engine_us"] <= last["trace"]["peer_us"]


def test_leases_renew_only_while_a_page_is_connected(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    _open(inviter, joiner)
    core = inviter.core
    assert core is not None
    time.sleep(1.0)
    stale = core.clock.lease_until("deck:3")
    assert stale is not None and stale < _clock(), "no joiner page: its leases lapse"
    page = Page(joiner)
    _until(lambda: (core.clock.lease_until("deck:3") or 0) > _clock(), what="renewed while the page is connected")
    page.off()
    time.sleep(1.0)
    assert (core.clock.lease_until("deck:3") or 0) < _clock(), "the page left: renewing stopped"


def test_delete_ends_the_booth_on_both_laptops(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    pb = Page(joiner)
    _open(inviter, joiner)
    epoch = inviter.view(enabled=True)["epoch"]
    inviter.close()
    _until(lambda: any(e["type"] == "state" and e["state"] == "off" for e in pb.events), what="end on the joiner")
    assert inviter.view(enabled=True)["state"] == "off"
    assert pb.ops()[-1]["op"] == {"op": "end"}
    inviter.open("inviter")
    assert inviter.view(enabled=True)["epoch"] != epoch, "a re-open mints a new epoch"


def test_a_partner_that_keeps_refusing_the_ops_lane_turns_the_booth_degraded_loudly() -> None:
    """A protocol disagreement (the partner answers 4xx to every ops POST) must not read as live while
    the ordered lane is stuck behind it; controls and renews still answer, so contact alone is fresh."""
    inviter, joiner = Booth(_clock), Booth(_clock)
    refuse_ops = threading.Event()

    def to_joiner(lane: str, envelope: dict[str, Any], timeout: float) -> dict[str, Any]:
        if lane == "ops" and refuse_ops.is_set():
            raise ConnectionError("door answered 422: bad_field")
        return _door(joiner)(lane, envelope, timeout)

    inviter.attach_transport(to_joiner, "test")
    joiner.attach_transport(_door(inviter), "test")
    try:
        _open(inviter, joiner)
        refuse_ops.set()
        inviter.intent({"kind": "op", "op": {"op": "loop_off", "deck": 1}})
        _until(lambda: inviter.view(enabled=True)["state"] == "degraded", what="degraded")
        view = inviter.view(enabled=True)
        assert "422" in (view["last_error"] or "") and view["lanes"]["ops"]["pending"] == 1
        refuse_ops.clear()
        _until(lambda: inviter.view(enabled=True)["state"] == "live", what="live again once the partner accepts")
        _until(lambda: joiner.view(enabled=True)["seq"] == 1, what="the held op delivered, not lost")
    finally:
        inviter.shutdown()
        joiner.shutdown()
