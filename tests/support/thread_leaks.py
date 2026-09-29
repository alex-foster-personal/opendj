"""Find background threads a test started and left running.

sentry_sdk's ``sentry.monitor`` thread loops ``time.sleep(10)`` until its
client is closed. Teardowns that only called ``sentry_sdk.init(dsn=None)``
swapped the client without closing it, so every such monitor ran for the rest
of the pytest process and landed its sleeps in later tests' ``time.sleep``
spies (PR #4250). The root conftest runs :func:`leaked_threads` around every
test and fails the one that leaks.

Two rules, both keyed on threads that were NOT alive when the test started:

- a thread owned by ``sentry_sdk`` is a leak everywhere;
- a non-daemon thread of any owner is a leak only in modules marked
  ``no_leaked_threads`` (the suite-wide cost of that rule is unmeasured).

A closed client's monitor is killed but finishes its current ``sleep(10)``
before it exits. That thread is draining, not leaking: it never calls
``time.sleep`` again, so it is exempt once its Monitor reads not running.

-Claude
"""
from __future__ import annotations

import threading
import time

LEAK_GRACE_S: float = 2.0
SENTRY_MONITOR_THREAD: str = "sentry.monitor"


# ---------------------------------------------------------------- helpers
def _thread_owner_module(thread: threading.Thread) -> str:
    target = getattr(thread, "_target", None)
    return getattr(target, "__module__", None) or type(thread).__module__


def _is_sentry_owned(thread: threading.Thread) -> bool:
    return thread.name == SENTRY_MONITOR_THREAD or _thread_owner_module(thread).startswith(
        "sentry_sdk"
    )


def _is_draining_sentry_monitor(thread: threading.Thread) -> bool:
    """True when ``thread`` is a sentry.monitor whose Monitor was killed.

    Reads the Monitor from the thread target's closure; a monitor thread whose
    Monitor cannot be found is UNKNOWN and raises rather than guessing.
    """
    if thread.name != SENTRY_MONITOR_THREAD:
        return False
    from sentry_sdk.monitor import Monitor

    closure = getattr(getattr(thread, "_target", None), "__closure__", None) or ()
    monitors = [cell.cell_contents for cell in closure if isinstance(cell.cell_contents, Monitor)]
    if len(monitors) == 1:
        return monitors[0]._running is False
    if not thread.is_alive():
        return True
    raise RuntimeError(
        f"UNKNOWN: found {len(monitors)} Monitor objects behind live thread {thread.name!r}; "
        "sentry_sdk's monitor internals changed, re-derive tests/support/thread_leaks.py"
    )


def _describe(thread: threading.Thread) -> str:
    return (
        f"{thread.name} (daemon={thread.daemon}, owner={_thread_owner_module(thread)}, "
        f"ident={thread.ident})"
    )


# ---------------------------------------------------------------- public
def leaked_threads(
    baseline: set[threading.Thread],
    *,
    include_non_daemon: bool,
    grace_s: float = LEAK_GRACE_S,
) -> list[str]:
    """Describe each thread started since ``baseline`` that is still running.

    Candidates get one shared ``grace_s`` deadline to finish (a bounded join,
    not a sleep), so a thread that is already shutting down is not reported.
    """
    candidates = [
        thread
        for thread in threading.enumerate()
        if thread not in baseline
        and (_is_sentry_owned(thread) or (include_non_daemon and not thread.daemon))
        and not _is_draining_sentry_monitor(thread)
    ]
    deadline = time.monotonic() + grace_s
    for thread in candidates:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return [_describe(thread) for thread in candidates if thread.is_alive()]
