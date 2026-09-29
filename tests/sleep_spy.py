"""Thread-scoped ``time.sleep`` spies for tests.

``monkeypatch.setattr(time, "sleep", spy)`` swaps the attribute on the ONE
process-wide ``time`` module, so while it is installed EVERY thread in the
pytest process calls the spy, not just the test's own. A daemon thread left
running by an earlier test then writes into the test's list: sentry_sdk's
``sentry.monitor`` thread loops ``time.sleep(10)``, and a spy that returns at
once turns it into a busy loop that flooded
``test_dry_runner_still_skips_fresh_bundle`` with 436 entries of ``10`` on
PR #4250 (Tue 29 Sep 2026).

These helpers act only on calls made by the thread that installed them and
hand every other thread the real sleep, unchanged.

CONTAINMENT ONLY, NOT A PATTERN TO SPREAD. AGENTS.md "No mocks and locked real
fixtures" bars monkeypatching in tests. This module exists to narrow two
global ``time.sleep`` patches that predate it
(tests/lyrics/test_sources_http_retry.py, which also fakes urlopen, and
tests/shared/test_machine_pressure.py) until they move to a real path. Do not
use it in a new test: drive the real timed path instead, as the dry-runner
tests in tests/webui/test_queue_user.py now do.

-Claude
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest


def spy_on_own_thread_sleep(
    monkeypatch: pytest.MonkeyPatch, on_own_thread: Callable[[float], None]
) -> None:
    """Route ``time.sleep`` calls from the CALLING thread to ``on_own_thread``.

    Calls from any other thread still sleep for real, so a background loop
    keeps its own cadence instead of spinning.
    """
    owner = threading.get_ident()
    real_sleep = time.sleep

    def _sleep(seconds: float) -> None:
        if threading.get_ident() != owner:
            real_sleep(seconds)
            return
        on_own_thread(seconds)

    monkeypatch.setattr(time, "sleep", _sleep)


def record_own_thread_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make the calling thread's sleeps instant and return the list of their durations."""
    calls: list[float] = []
    spy_on_own_thread_sleep(monkeypatch, calls.append)
    return calls
