"""SMART-03 -- per-key debouncer."""
from __future__ import annotations

import pytest

from apps.smartlists.debounce import Debouncer

pytestmark = pytest.mark.requirement("SMART-03")


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def fake_clock():
    return _FakeClock()


def test_arm_then_wait_ready(fake_clock) -> None:
    deb = Debouncer(window_seconds=5.0, clock=fake_clock)
    deb.arm("x")
    assert deb.ready() == []
    fake_clock.advance(4.999)
    assert deb.ready() == []
    fake_clock.advance(0.002)
    assert deb.ready() == ["x"]
    assert deb.ready() == []


def test_re_arm_resets_timer(fake_clock) -> None:
    deb = Debouncer(window_seconds=5.0, clock=fake_clock)
    deb.arm("x")
    fake_clock.advance(4.9)
    deb.arm("x")
    fake_clock.advance(4.9)
    assert deb.ready() == []
    fake_clock.advance(0.2)
    assert deb.ready() == ["x"]


def test_many_keys(fake_clock) -> None:
    deb = Debouncer(window_seconds=1.0, clock=fake_clock)
    for k in ["a", "b", "c"]:
        deb.arm(k)
    fake_clock.advance(2.0)
    assert sorted(deb.ready()) == ["a", "b", "c"]


def test_cancel(fake_clock) -> None:
    deb = Debouncer(window_seconds=1.0, clock=fake_clock)
    deb.arm("x")
    deb.cancel("x")
    fake_clock.advance(10)
    assert deb.ready() == []


def test_pending_list(fake_clock) -> None:
    deb = Debouncer(window_seconds=5.0, clock=fake_clock)
    deb.arm("x")
    deb.arm("y")
    assert sorted(deb.pending()) == ["x", "y"]
