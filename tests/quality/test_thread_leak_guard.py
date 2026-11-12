"""The thread-leak guard in tests/support/thread_leaks.py names real leaks and only those.

- [if] an unclosed real sentry_sdk client's monitor goes unnamed [then] broken, [else stop].
- [if] a closed client's draining monitor is still reported [then] broken, [else stop].
- [if] a test's own non-daemon thread goes unnamed under the rule [then] broken, [else stop].
- [if] a thread alive before the test started is reported [then] broken, [else stop].
- [if] a busy daemon survivor goes unnamed at module end [then] broken, [else stop].
- [if] a blocked daemon survivor is reported at module end [then] broken, [else stop].
- [if] busy_leaked_threads waits the window when nobody survived [then] broken, [else stop].

-Claude
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from tests.support.sentry_client import close_sentry_client
from tests.support.thread_leaks import SENTRY_MONITOR_THREAD, busy_leaked_threads, leaked_threads

pytestmark = pytest.mark.no_leaked_threads

FAKE_DSN: str = "https://public@o0.ingest.de.sentry.io/43"
FAST_GRACE_S: float = 0.1


@pytest.fixture(scope="module")
def thread_alive_before_every_test() -> Iterator[threading.Thread]:
    """A real non-daemon thread started before each test's baseline and alive through it."""
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="pre-existing-worker", daemon=False)
    thread.start()
    yield thread
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "the pre-existing worker ignored its release"


def _start_real_sentry_monitor() -> None:
    import sentry_sdk
    from sentry_sdk.transport import Transport

    class DiscardingTransport(Transport):
        def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
            return None

    sentry_sdk.init(dsn=FAKE_DSN, transport=DiscardingTransport(), traces_sample_rate=1.0)
    with sentry_sdk.start_transaction(op="test", name="start the monitor"):
        pass


def test_unclosed_sentry_client_leaks_a_named_monitor_and_closing_drains_it() -> None:
    """[if] an open monitor goes unnamed, or a closed one is named [then] broken, [else stop]."""
    pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")
    baseline = set(threading.enumerate())
    try:
        _start_real_sentry_monitor()
        leaks = leaked_threads(baseline, include_non_daemon=False, grace_s=FAST_GRACE_S)
        assert any(leak.startswith(SENTRY_MONITOR_THREAD) for leak in leaks), leaks
    finally:
        close_sentry_client()
    assert leaked_threads(baseline, include_non_daemon=False, grace_s=FAST_GRACE_S) == []


def test_non_daemon_thread_is_named_only_where_the_rule_applies() -> None:
    """[if] a non-daemon leak is missed on-rule, or named off-rule [then] broken, [else stop]."""
    baseline = set(threading.enumerate())
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="leaky-worker", daemon=False)
    worker.start()
    try:
        strict = leaked_threads(baseline, include_non_daemon=True, grace_s=FAST_GRACE_S)
        assert [leak.split(" ")[0] for leak in strict] == ["leaky-worker"], strict
        assert leaked_threads(baseline, include_non_daemon=False, grace_s=FAST_GRACE_S) == []
    finally:
        release.set()
        worker.join(timeout=5)


def test_thread_alive_before_the_test_is_not_a_leak(
    thread_alive_before_every_test: threading.Thread,
) -> None:
    """[if] a thread alive before the test started is reported [then] broken, [else stop].

    Control for the autouse guard's own baseline too: the module fixture's
    non-daemon worker outlives this test by design, so a guard that loses its
    before-set fails this clean test at teardown.
    """
    assert thread_alive_before_every_test.is_alive()
    baseline = set(threading.enumerate())
    assert leaked_threads(baseline, include_non_daemon=True, grace_s=FAST_GRACE_S) == []


def _spin(stop: threading.Event) -> None:
    while not stop.is_set():
        pass


def test_busy_daemon_survivor_is_reported_at_module_end() -> None:
    """[if] a busy daemon survivor goes unnamed at module end [then] broken, [else stop]."""
    baseline = set(threading.enumerate())
    stop = threading.Event()
    worker = threading.Thread(target=_spin, args=(stop,), name="busy-worker", daemon=True)
    worker.start()
    try:
        busy = busy_leaked_threads(baseline, window_s=0.2)
        assert any("busy-worker" in line for line in busy), busy
        assert any("test_busy_daemon_survivor" in line or "_spin" in line for line in busy), busy
    finally:
        stop.set()
        worker.join(timeout=5)


def test_blocked_daemon_survivor_is_not_reported_at_module_end() -> None:
    """[if] a blocked daemon survivor is reported at module end [then] broken, [else stop]."""
    baseline = set(threading.enumerate())
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="blocked-worker", daemon=True)
    worker.start()
    try:
        assert busy_leaked_threads(baseline, window_s=0.2) == []
    finally:
        release.set()
        worker.join(timeout=5)


def test_busy_thread_alive_before_baseline_is_not_reported() -> None:
    """[if] a busy thread alive before the module baseline is reported [then] broken, [else stop]."""
    stop = threading.Event()
    worker = threading.Thread(target=_spin, args=(stop,), name="pre-busy", daemon=True)
    worker.start()
    baseline = set(threading.enumerate())
    try:
        assert busy_leaked_threads(baseline, window_s=0.2) == []
    finally:
        stop.set()
        worker.join(timeout=5)


def test_busy_leaked_threads_skips_the_window_when_nobody_survived() -> None:
    """[if] busy_leaked_threads waits the window when nobody survived [then] broken, [else stop]."""
    baseline = set(threading.enumerate())
    started = time.monotonic()
    assert busy_leaked_threads(baseline, window_s=5.0) == []
    assert time.monotonic() - started < 1.0


def test_busy_leaked_threads_fallback_reports_busy_unmapped_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the macOS fallback misses a busy thread [then] broken, [else stop]."""
    monkeypatch.setattr("tests.support.thread_leaks._per_thread_cpu_s", dict)
    baseline = set(threading.enumerate())
    stop = threading.Event()
    worker = threading.Thread(target=_spin, args=(stop,), name="fallback-busy", daemon=True)
    worker.start()
    try:
        busy = busy_leaked_threads(baseline, window_s=0.2)
        assert any("fallback-busy" in line for line in busy), busy
        assert any("per-thread CPU unavailable" in line for line in busy), busy
    finally:
        stop.set()
        worker.join(timeout=5)


def test_busy_leaked_threads_fallback_ignores_blocked_unmapped_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] the macOS fallback reports a blocked thread [then] broken, [else stop]."""
    monkeypatch.setattr("tests.support.thread_leaks._per_thread_cpu_s", dict)
    baseline = set(threading.enumerate())
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="fallback-blocked", daemon=True)
    worker.start()
    try:
        assert busy_leaked_threads(baseline, window_s=0.2) == []
    finally:
        release.set()
        worker.join(timeout=5)
