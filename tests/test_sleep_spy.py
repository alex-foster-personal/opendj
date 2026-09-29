"""The thread-scoped sleep spy records its own thread and nothing else.

[if] another thread calls time.sleep while the spy is installed [then] the call
is neither recorded nor made instant, else stop.
[if] the installing thread calls time.sleep [then] the duration is recorded and
the call returns at once, else stop.

-Claude
"""
from __future__ import annotations

import threading
import time

import pytest

from tests.sleep_spy import record_own_thread_sleeps

FOREIGN_SLEEP_S: float = 0.05


def test_foreign_thread_sleep_is_real_and_unrecorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record_own_thread_sleeps(monkeypatch)
    elapsed: list[float] = []

    def _foreign() -> None:
        t0 = time.monotonic()
        time.sleep(FOREIGN_SLEEP_S)
        elapsed.append(time.monotonic() - t0)

    thread = threading.Thread(target=_foreign, name="foreign-sleeper")
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "the foreign thread never finished its sleep"
    assert calls == [], "a foreign thread's sleep landed in the owner's list"
    assert len(elapsed) == 1, "the foreign thread never reached its sleep"
    assert elapsed[0] >= FOREIGN_SLEEP_S, "the foreign thread's sleep was made instant"


def test_own_thread_sleep_is_recorded_and_instant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record_own_thread_sleeps(monkeypatch)
    t0 = time.monotonic()
    time.sleep(3600)
    assert time.monotonic() - t0 < 60, "the owner's sleep was not made instant"
    assert calls == [3600]
