"""B2BBOOTH-91: the two engine-to-engine lanes (pure threads, the transport injected).

- [if] the OPS lane loses, reorders or duplicates a message across a failed POST [then] fail
- [if] the CTL lane ever has two values of one key in flight, or sends an older value after a newer one was put [then] fail
- [if] a CTL value that failed to send is lost although nothing newer replaced it [then] fail
- [if] a newer CTL value is overwritten by the retry of an older one [then] fail
- [if] a lane breaks any line above [then] fail, [else stop].
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from apps.b2b_booth.lanes import CtlLane, OpsLane

pytestmark = [pytest.mark.requirement("B2BBOOTH-91")]
E1 = "0123456789abcdef"


class Wire:
    """An in-process transport that records every envelope and can hold or fail a POST."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail_next = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.lock = threading.Lock()

    def post(self, lane: str, envelope: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            assert self.gate.wait(5), "a held POST was never released"
            with self.lock:
                if self.fail_next:
                    self.fail_next -= 1
                    raise ConnectionError("injected failure")
                self.sent.append(envelope)
            return {"v": 1, "now_us": 0, "messages": [], "answers": []}
        finally:
            with self.lock:
                self.in_flight -= 1


def _lane(cls: type, wire: Wire) -> Any:
    errors: list[str] = []
    lane = cls(wire.post, lambda: 0, lambda *a: None, lambda name, msg: errors.append(msg))
    lane.errors = errors
    lane.start()
    return lane


def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def _ops(n: int, start: int = 1) -> list[dict[str, Any]]:
    return [{"kind": "resend", "epoch": E1, "from": i} for i in range(start, start + n)]


def test_ops_lane_keeps_order_across_failures_and_never_duplicates() -> None:
    wire = Wire()
    wire.fail_next = 3
    lane = _lane(OpsLane, wire)
    try:
        lane.put(_ops(50))
        lane.put(_ops(50, 51))
        _until(lambda: sum(len(e["messages"]) for e in wire.sent) == 100)
        sent = [m["from"] for e in wire.sent for m in e["messages"]]
        assert sent == list(range(1, 101)), "in order, each exactly once"
        assert lane.failures == 3 and len(lane.errors) == 3, "positive control: the failures really happened"
        assert all(e["epoch"] == E1 and "epoch" not in e["messages"][0] for e in wire.sent)
        assert wire.max_in_flight == 1
    finally:
        lane.stop()


def test_a_failed_batch_goes_back_ahead_of_messages_put_while_it_was_in_flight() -> None:
    wire = Wire()
    lane = _lane(OpsLane, wire)
    try:
        wire.gate.clear()
        wire.fail_next = 1
        lane.put(_ops(5))
        _until(lambda: wire.in_flight == 1)
        lane.put(_ops(5, 6))  # arrives while 1..5 are in flight (and about to fail)
        wire.gate.set()
        _until(lambda: sum(len(e["messages"]) for e in wire.sent) == 10)
        assert [m["from"] for e in wire.sent for m in e["messages"]] == list(range(1, 11))
    finally:
        lane.stop()


def test_ops_lane_splits_an_envelope_at_an_epoch_change() -> None:
    wire = Wire()
    lane = _lane(OpsLane, wire)
    try:
        wire.gate.clear()
        lane.put([{"kind": "resend", "epoch": None, "from": 1}])
        _until(lambda: wire.in_flight == 1)
        lane.put(_ops(2))
        wire.gate.set()
        _until(lambda: len(wire.sent) == 2)
        assert [e["epoch"] for e in wire.sent] == [None, E1]
    finally:
        lane.stop()


def _ctl(key: str, value: float) -> tuple[str, dict[str, Any]]:
    return key, {"kind": "ctl", "epoch": E1, "key": key, "value": value}


def test_ctl_lane_one_in_flight_newest_replaces_unsent() -> None:
    wire = Wire()
    lane = _lane(CtlLane, wire)
    try:
        wire.gate.clear()
        lane.put([_ctl("fader", 0.1)])
        _until(lambda: wire.in_flight == 1)
        for v in (0.2, 0.3, 0.4):
            lane.put([_ctl("fader", v)])
        lane.put([_ctl("eq", 0.9)])
        time.sleep(0.05)
        assert wire.max_in_flight == 1, "the next batch waits for the one in flight"
        wire.gate.set()
        _until(lambda: len(wire.sent) == 2)
        assert [[(m["key"], m["value"]) for m in e["messages"]] for e in wire.sent] == [
            [("fader", 0.1)], [("fader", 0.4), ("eq", 0.9)]]
        assert lane.replaced == 2, "0.2 and 0.3 were replaced unsent"
    finally:
        lane.stop()


def test_ctl_lane_failed_value_is_put_back_unless_something_newer_replaced_it() -> None:
    wire = Wire()
    lane = _lane(CtlLane, wire)
    try:
        wire.gate.clear()
        wire.fail_next = 1
        lane.put([_ctl("fader", 0.1), _ctl("eq", 0.5)])
        _until(lambda: wire.in_flight == 1)
        lane.put([_ctl("fader", 0.7)])  # newer than the failing one
        wire.gate.set()
        _until(lambda: len(wire.sent) == 1)
        values = {m["key"]: m["value"] for m in wire.sent[0]["messages"]}
        assert values == {"fader": 0.7, "eq": 0.5}, "eq came back after the failure; fader kept the newer 0.7"
    finally:
        lane.stop()


def test_a_slower_transport_stretches_the_lane_timeouts_and_the_lan_keeps_its_own() -> None:
    wire = Wire()
    lan = OpsLane(wire.post, lambda: 0, lambda *a: None, lambda *a: None)
    relay = CtlLane(wire.post, lambda: 0, lambda *a: None, lambda *a: None, 4.0)
    assert (lan.timeout_s, relay.timeout_s) == (3.0, 6.0)
    assert OpsLane.timeout_s == 3.0, "negative control: the class default is untouched by an instance's scale"
