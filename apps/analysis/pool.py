"""The analysis work unit and the process pool that runs it.

Split out of :mod:`apps.analysis.run` so the CLI driver holds argument parsing,
queue building and reporting, while the part that has to be picklable, spawned
and crash-diagnosed lives on its own. The pool speaks in ``(stable_id, path)``
pairs rather than the CLI's ``TrackRef``, which keeps the dependency one-way.
"""
from __future__ import annotations

import multiprocessing
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from .backends import get_backend
from .backends.base import (
    AnalyzerBackend,
    BackendNotAvailable,
    TrackTooLong,
    TrackUnreadable,
    TrackVanished,
)
from .record import AnalysisRecord
from .worker_diagnostics import (
    init_worker,
    owner_identity,
    pool_death_message,
    worker_exit_signals,
)

#: ``(stable_id, record, error)`` for one track.
Row = tuple[str, AnalysisRecord | None, str | None]


def analyze_one(
    backend: str | type[AnalyzerBackend], stable_id: str, path_str: str
) -> Row:
    """Analyze one track, or say why this FILE could not be.

    Only failures a backend has declared to be about the input are turned
    into a per-track error here. Everything else propagates: a broad catch at
    this level relabels a machine-wide fault - an unreadable analyzer config,
    a backend that failed to initialize, a dead worker - as "this file
    failed", which the CLI then reports as EXIT_TRACK_FAILURES, which is the
    one status a chunking caller is allowed to continue past. The caller then
    meets the identical fault once per chunk across the whole library.

    ``get_backend`` is outside the guard for the same reason: an unresolvable
    backend name is a configuration fact, not a property of this track.
    ``run`` resolves the name once and hands workers the CLASS: a ``spawn``
    worker imports it by reference instead of re-reading a registry that only
    the parent process has populated. A name is still accepted for callers
    that analyze a single track in-process.

    Either way the backend module is imported in the child only when the work
    item is unpickled, which is AFTER
    :func:`~apps.analysis.worker_diagnostics.init_worker` has run, so the
    thread pins are already in place before numpy and numba load.
    """
    if isinstance(backend, str):
        backend = get_backend(backend)
    try:
        rec = backend.analyze(Path(path_str), stable_id)
    except (
        BackendNotAvailable, TrackTooLong, TrackUnreadable, TrackVanished
    ) as exc:
        return stable_id, None, f"{type(exc).__name__}: {exc}"
    return stable_id, rec, None


class AnalysisProcessPoolExecutor(ProcessPoolExecutor):
    """Refresh the manager's sentinel set after each spawned worker is registered.

    CPython 3.11 wakes the manager before dynamically spawning on submission.
    It can consume that wakeup before the new process enters ``_processes`` and
    then wait on only older workers, missing a new worker's fatal exit. Notify
    after the stdlib spawn has registered the child; keep its spawning, queue,
    failure handling and teardown unchanged on every supported platform.
    """

    def _spawn_process(self) -> None:
        super()._spawn_process()
        self._executor_manager_thread_wakeup.wakeup()


def spawn_pool(workers: int) -> ProcessPoolExecutor:
    """The analysis worker pool: spawned workers, thread pins, parent watch.

    Both pool owners (:func:`run_pool` and the queue runner) build it here,
    so the initializer wiring the quit tests exercise is the one they run.
    """
    return AnalysisProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=init_worker,
        # The pool owner's pid and start time, read HERE: a worker reading
        # them itself could name a reparented or recycled pid if the owner died first.
        initargs=owner_identity(),
    )


def run_pool(
    targets: Iterable[tuple[str, str]],
    *,
    backend: str | type[AnalyzerBackend],
    workers: int,
) -> list[Row]:
    """Analyze ``(stable_id, path)`` targets across SPAWNED worker processes.

    The context is ``spawn``, never the Linux default ``fork``. Forking a
    parent that has already imported the librosa/numba/OpenBLAS stack copies
    only the calling thread, so the child inherits once-initialized native
    function tables and thread-local state whose owning threads do not exist in
    it. Calling into that state jumps through a NULL function pointer, which is
    exactly the ``segfault at 0 ip 0000000000000000`` worker death seen on CI
    (issue #1316), and no Python traceback can exist for it. A spawned child
    imports the stack itself, from a clean interpreter.

    A consequence worth knowing: a spawned child re-imports the parent's
    ``__main__``, so an embedder calling this from a script must guard its
    entry point with ``if __name__ == "__main__":``. Every caller in this
    repository shells out to ``python -m apps.analysis.run``, which is safe.
    """
    pool = spawn_pool(workers)
    # The stdlib exposes no public handle on the worker processes, and
    # ``shutdown`` drops the private one, so snapshot the objects while they
    # are still reachable. Keyed by pid: a worker that dies is replaced, and
    # both the dead and the replacement matter to the diagnostic.
    seen_workers: dict[int, object] = {}

    def _snapshot_workers() -> None:
        seen_workers.update(
            {p.pid: p for p in (getattr(pool, "_processes", None) or {}).values()}
        )

    rows: list[Row] = []
    try:
        try:
            futures = []
            for stable_id, path_str in targets:
                # Submission is inside the same guard as collection: a worker
                # can die while the queue is still being submitted, and that
                # path raises BrokenProcessPool out of ``submit`` itself.
                futures.append(
                    pool.submit(analyze_one, backend, stable_id, path_str)
                )
                _snapshot_workers()
            for fut in as_completed(futures):
                rows.append(fut.result())
                _snapshot_workers()
        finally:
            # Snapshot BEFORE shutting down, read exit codes AFTER: while the
            # executor is still tearing workers down every ``Process.exitcode``
            # is still None, and a real native crash would be reported only as
            # the generic "died before its exit signal could be read" fallback.
            _snapshot_workers()
            pool.shutdown(wait=True)
    except BrokenProcessPool as exc:
        raise RuntimeError(
            pool_death_message(worker_exit_signals(seen_workers.values()))
        ) from exc
    return rows
