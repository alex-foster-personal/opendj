"""B2BBOOTH-40: the engine booth's commit watcher, the hook the track transfer hangs off.

- [if] a committed op on EITHER laptop does not wake that laptop's watcher with the op readable in
  ``log_since`` [then] fail
- [if] a booth close (``off``) does not wake the watcher with ``log_since`` reading no epoch [then] fail
- [if] a watcher counts as a connected page (leases would renew with no DJ looking) [then] fail
- [if] a watcher is woken by traffic that commits nothing (controls) [then] fail, [else stop].
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from apps.b2b_booth.booth import Booth
from tests.b2b_booth.test_booth_engine import Page, _clock, _door, _open, _until

pytestmark = [pytest.mark.requirement("B2BBOOTH-40")]


class Watch:
    def __init__(self, booth: Booth) -> None:
        self.booth, self.calls, self.lock = booth, 0, threading.Lock()
        self.seen: list[dict[str, object]] = []
        booth.watch_commits(self._on)

    def _on(self) -> None:
        with self.lock:
            self.calls += 1
            self.seen.append(self.booth.log_since(0))

    def ops(self) -> list[dict[str, object]]:
        with self.lock:
            return list(self.seen[-1]["ops"]) if self.seen else []  # type: ignore[arg-type]


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


def test_a_committed_op_wakes_both_engines_watchers_with_the_op_in_the_log(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    wa, wb = Watch(inviter), Watch(joiner)
    _open(inviter, joiner)
    joiner.intent({"kind": "op", "op": {"op": "loop_off", "deck": 3}})
    for watch in (wa, wb):
        _until(lambda w=watch: any(c["op"] == {"op": "loop_off", "deck": 3} for c in w.ops()), what="the op in the watcher's log")  # type: ignore[index]


def test_a_close_wakes_the_watcher_with_no_epoch(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    wa = Watch(inviter)
    _open(inviter, joiner)
    inviter.close()
    assert wa.seen[-1]["epoch"] is None, "the last wake after close reads a closed booth"


def test_a_watcher_is_not_a_page_and_controls_do_not_wake_it(booths: tuple[Booth, Booth]) -> None:
    inviter, joiner = booths
    wa, wb = Watch(inviter), Watch(joiner)
    _open(inviter, joiner)
    assert inviter.page_connected is False, "a commit watcher must never count as a connected page"
    page = Page(joiner)  # positive control: the controls DO reach the joiner's stream as events
    time.sleep(0.3)
    before = (wa.calls, wb.calls)
    for i in range(10):
        inviter.intent({"kind": "control", "unit": "strip:1", "ctl": "fader", "value": i / 10})
    _until(lambda: bool(page.of("control")) and page.of("control")[-1]["control"]["value"] == 0.9,
           what="the newest fader value on the joiner's page (controls coalesce newest-wins)")
    assert (wa.calls, wb.calls) == before, "continuous controls commit nothing: they must not wake the transfer"
