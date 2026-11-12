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

A third rule applies at module boundaries via :func:`busy_leaked_threads`: a
thread a module started that is still alive after the module AND used at least
``BUSY_CPU_FRACTION`` of a core over ``BUSY_WINDOW_S`` is a leak everywhere;
blocked threads (``Event.wait``, ``serve_forever``, ``accept``) are not
(issue #9628, abandoned PerformancePage pollers).

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
SENTRY_MONITOR_CLASS: tuple[str, str] = ("sentry_sdk.monitor", "Monitor")
BUSY_WINDOW_S: float = 0.2
BUSY_CPU_FRACTION: float = 0.1


# ---------------------------------------------------------------- helpers
def _thread_owner_module(thread: threading.Thread) -> str:
    target = getattr(thread, "_target", None)
    return getattr(target, "__module__", None) or type(thread).__module__


def _is_sentry_owned(thread: threading.Thread) -> bool:
    return thread.name == SENTRY_MONITOR_THREAD or _thread_owner_module(thread).startswith("sentry_sdk")


def _is_sentry_monitor_object(obj: object) -> bool:
    cls = type(obj)
    return (cls.__module__, cls.__qualname__) == SENTRY_MONITOR_CLASS


def _is_draining_sentry_monitor(thread: threading.Thread) -> bool:
    """True when ``thread`` is a sentry.monitor whose Monitor was killed.

    Reads the Monitor from the thread target's closure; a monitor thread whose
    Monitor cannot be found is UNKNOWN and raises rather than guessing. Matches
    the class by qualified name instead of importing it: the guard tears down
    while a test's own ``builtins.__import__`` patch may still be installed.
    """
    if thread.name != SENTRY_MONITOR_THREAD:
        return False
    closure = getattr(getattr(thread, "_target", None), "__closure__", None) or ()
    monitors = [cell.cell_contents for cell in closure if _is_sentry_monitor_object(cell.cell_contents)]
    if len(monitors) == 1:
        return monitors[0]._running is False
    if not thread.is_alive():
        return True
    raise RuntimeError(
        f"UNKNOWN: found {len(monitors)} Monitor objects behind live thread {thread.name!r}; "
        "sentry_sdk's monitor internals changed, re-derive tests/support/thread_leaks.py"
    )


def _describe(thread: threading.Thread) -> str:
    return f"{thread.name} (daemon={thread.daemon}, owner={_thread_owner_module(thread)}, ident={thread.ident})"


def _target_qualname(thread: threading.Thread) -> str:
    target = getattr(thread, "_target", None)
    qualname = getattr(target, "__qualname__", None)
    if qualname:
        return qualname
    return type(thread).__qualname__


def _per_thread_cpu_s() -> dict[int, float]:
    import psutil

    proc = psutil.Process()
    return {t.id: t.user_time + t.system_time for t in proc.threads()}


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


def busy_leaked_threads(
    baseline: set[threading.Thread],
    *,
    window_s: float = BUSY_WINDOW_S,
    min_cpu_fraction: float = BUSY_CPU_FRACTION,
) -> list[str]:
    """Describe survivors still burning CPU after an idle window (module guard)."""
    survivors = [t for t in threading.enumerate() if t not in baseline and t.is_alive()]
    if not survivors:
        return []

    threshold = window_s * min_cpu_fraction
    before_cpu = _per_thread_cpu_s()
    process_before = time.process_time()
    caller_before = getattr(time, "thread_time", lambda: 0.0)()

    threading.Event().wait(window_s)

    after_cpu = _per_thread_cpu_s()
    process_after = time.process_time()
    caller_after = getattr(time, "thread_time", lambda: 0.0)()
    reports: list[str] = []

    still_alive = [t for t in survivors if t.is_alive()]
    unmapped: list[threading.Thread] = []
    for thread in still_alive:
        native_id = thread.native_id
        if native_id is None:
            unmapped.append(thread)
            continue
        if native_id not in before_cpu or native_id not in after_cpu:
            unmapped.append(thread)
            continue
        delta = after_cpu[native_id] - before_cpu[native_id]
        if delta >= threshold:
            reports.append(
                f"{thread.name} (daemon={thread.daemon}, target={_target_qualname(thread)}, "
                f"owner={_thread_owner_module(thread)}, cpu={delta * 1000:.0f}ms in {window_s * 1000:.0f}ms)"
            )

    if unmapped:
        foreign_delta = (process_after - process_before) - (caller_after - caller_before)
        if foreign_delta >= threshold:
            note = f"per-thread CPU unavailable on this platform; group used {foreign_delta * 1000:.0f}ms"
            reports.extend(
                f"{thread.name} (daemon={thread.daemon}, target={_target_qualname(thread)}, "
                f"owner={_thread_owner_module(thread)}, {note})"
                for thread in unmapped
                if thread.is_alive()
            )
    return reports
