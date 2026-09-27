"""Thread pinning and native-crash diagnostics for analysis pool workers.

Stdlib only, on purpose. :func:`init_worker` runs as the pool's ``initializer``,
and with a ``spawn`` context the child imports this module to resolve that
callable BEFORE it imports anything that pulls in the analysis backend. That
ordering is the whole point: every native math library reads its thread-count
variable once, when it is imported, so a pin applied after the import chain has
run is a no-op that still looks like a fix.
"""
from __future__ import annotations

import faulthandler
import os
import signal
from collections.abc import Iterable

#: Native math libraries the librosa backend pulls in transitively, each of
#: which sizes a thread pool from its own environment variable at import time.
#: The analysis pool already fans out across processes, so a per-worker thread
#: pool buys nothing and oversubscribes the host; on a loaded CI box that
#: oversubscription is what made the fork-time crash of issue #1316 likely.
THREAD_PIN_VARS: tuple[str, ...] = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMBA_NUM_THREADS",
)

#: The fatal signals :mod:`faulthandler` installs a handler for. A worker
#: killed by anything outside this set leaves no Python-level trace, so the
#: operator must not be sent looking for one. Looked up by name because the set
#: differs by platform: Windows has no ``SIGBUS``, and faulthandler does not
#: trace it there either, so omitting it is the accurate answer, not a gap.
TRACED_SIGNALS: frozenset[int] = frozenset(
    int(getattr(signal, name))
    for name in ("SIGSEGV", "SIGFPE", "SIGABRT", "SIGBUS", "SIGILL")
    if hasattr(signal, name)
)

#: What :class:`~concurrent.futures.ProcessPoolExecutor` sends to the workers
#: that were still alive when another worker broke the pool. Their exit codes
#: are indistinguishable from a real cause by inspection alone, so they are
#: reported as cleanup whenever a genuine crash signal is also present.
CLEANUP_SIGNAL: int = int(signal.SIGTERM)


def init_worker() -> None:
    """Pool-worker entry point: pin the native thread pools, then arm the tracer.

    Runs in the child before the backend import chain, which is what makes the
    pin effective. See :data:`THREAD_PIN_VARS`.
    """
    for name in THREAD_PIN_VARS:
        os.environ[name] = "1"
    faulthandler.enable(all_threads=True)


def worker_exit_signals(processes: Iterable[object]) -> list[int]:
    """The terminating signal number of every worker that exited on a signal.

    ``multiprocessing`` reports a signalled death as a negative ``exitcode``.
    ``None`` means the child has not been reaped yet, which is why callers must
    shut the pool down before reading this.
    """
    numbers: list[int] = []
    for process in processes:
        exitcode = getattr(process, "exitcode", None)
        if exitcode is not None and exitcode < 0:
            numbers.append(-exitcode)
    return numbers


def _signal_label(number: int) -> str:
    """Name a terminating signal, or render its number when it has no name.

    ``signal.Signals(n)`` raises ``ValueError`` for signal numbers with no enum
    member, notably the Linux real-time signals, and that ValueError would
    replace the crash diagnosis with an unrelated traceback.
    """
    try:
        return f"{signal.Signals(number).name} (signal {number})"
    except ValueError:
        return f"unnamed signal {number}"


def pool_death_message(exit_signals: Iterable[int]) -> str:
    """Describe a signalled worker exit instead of leaking BrokenProcessPool.

    Separates the initiating crash from executor teardown: once any worker has
    died from a signal other than :data:`CLEANUP_SIGNAL`, the SIGTERMs are the
    executor killing the survivors and are reported as cleanup rather than as
    independent causes. A run where SIGTERM is the ONLY signal seen keeps it as
    the cause, because then nothing killed the pool first.
    """
    signals = list(exit_signals)
    if not signals:
        return "analysis worker died before its exit signal could be read"

    crash_signals = sorted({n for n in signals if n != CLEANUP_SIGNAL})
    if crash_signals:
        causes = crash_signals
        cleanup_count = sum(1 for n in signals if n == CLEANUP_SIGNAL)
    else:
        causes = sorted(set(signals))
        cleanup_count = 0

    rendered = ", ".join(_signal_label(n) for n in causes)
    message = f"analysis worker died from {rendered}"
    if cleanup_count:
        plural = "" if cleanup_count == 1 else "s"
        message += (
            f"; {cleanup_count} surviving worker{plural} then terminated by "
            f"{_signal_label(CLEANUP_SIGNAL)} during executor cleanup"
        )
    if any(n in TRACED_SIGNALS for n in causes):
        message += "; the faulthandler trace from the crashed worker is printed above"
    else:
        message += (
            "; faulthandler was armed in the worker but does not trace "
            f"{rendered}, so no fatal trace was printed"
        )
    return message
