"""The thread-leak guard in tests/support/thread_leaks.py names real leaks and only those.

- [if] an unclosed real sentry_sdk client's monitor is not reported by name [then] broken, [else stop].
- [if] a closed client's draining monitor is still reported [then] broken, [else stop].
- [if] a non-daemon thread started by the test is not reported where the rule applies [then] broken, [else stop].
- [if] a thread alive before the test started is reported [then] broken, [else stop].

-Claude
"""
from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from tests.support.sentry_client import close_sentry_client
from tests.support.thread_leaks import SENTRY_MONITOR_THREAD, leaked_threads

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
    """[if] an unclosed client's monitor is unreported, or a closed one's still reported [then] broken, [else stop]."""
    baseline = set(threading.enumerate())
    try:
        _start_real_sentry_monitor()
        leaks = leaked_threads(baseline, include_non_daemon=False, grace_s=FAST_GRACE_S)
        assert any(leak.startswith(SENTRY_MONITOR_THREAD) for leak in leaks), leaks
    finally:
        close_sentry_client()
    assert leaked_threads(baseline, include_non_daemon=False, grace_s=FAST_GRACE_S) == []


def test_non_daemon_thread_is_named_only_where_the_rule_applies() -> None:
    """[if] a leaked non-daemon thread is unreported under the rule, or reported without it [then] broken, [else stop]."""
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
