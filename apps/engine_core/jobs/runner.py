"""Worker execution: one subprocess per job, JSON-lines progress, proven kill.

Every worker is spawned with ``start_new_session=True`` so it leads its own
process group (pgid == pid) and a cancel can take the whole tree, not just
the parent. The identity of that group is persisted AT SPAWN, before any
output is read, so a crash one millisecond later still leaves a provable
target for the next boot's reap.

Worker stdout contract: one JSON object per line,
``{"progress": 0.42, "message": "..."}``. A malformed line means the worker
is off-contract, so it is killed and the job fails with the raw tail. There
is no lenient parse and no partial credit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.engine_core.jobs.reap import (
    WorkerIdentity,
    group_has_live_member,
    reap,
)
from apps.engine_core.jobs.store import JobConflict, JobNotFound, JobStore

log = logging.getLogger(__name__)

# Argv builders and reconcile hooks are registered by the kinds themselves.
# The chassis ships ZERO kinds: an enqueue for an unregistered kind is a
# loud 400, not a job that sits queued forever.
WorkerArgv = Callable[[dict[str, Any]], Sequence[str]]
ReconcileHook = Callable[[dict[str, Any]], str]
# (job row, the whole parsed progress line) -> None. See
# register_progress_observer.
ProgressObserver = Callable[[dict[str, Any], dict[str, Any]], None]

RECONCILE_RESULTS: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "unknown"}
)

_WORKERS: dict[str, WorkerArgv] = {}
_RECONCILERS: dict[str, ReconcileHook] = {}
_OBSERVERS: dict[str, ProgressObserver] = {}

_TAIL_LINES: int = 20

# At most one progress write per job per this interval, and never on the event
# loop (#3964). The contract is a line per unit of work -- the folder import
# writes one per FILE -- and a StreamReader with buffered data hands back line
# after line without yielding, so writing each one synchronously held the loop
# for a whole backlog of sqlite commits: a 10,000-file import on a busy disk
# left /api/v1/health unanswered for about two minutes. 4 Hz is plenty for a
# progress bar, and every line still reaches its kind's observer.
PROGRESS_WRITE_INTERVAL_S: float = 0.25

# sqlite3.Error is NOT an OSError, so without it here a store write failing
# mid-run escaped the guard entirely and left the row 'running' forever with
# no worker behind it.
_ERRORS = (OSError, ValueError, RuntimeError, LookupError, sqlite3.Error)

# The supervisor is the ONLY thing draining the queue, so a transient store
# error must not end queue supervision for the life of the process. It backs
# off instead of dying, and resets the moment a drain succeeds.
_DRAIN_BACKOFF_S: float = 1.0
_DRAIN_BACKOFF_MAX_S: float = 30.0

# How long shutdown waits for a claimed job to come out the far side of its
# fork. A fork+exec is milliseconds; anything near this budget means something
# is wrong, so expiry is logged by job id rather than passed over.
_SPAWN_SETTLE_S: float = 5.0
_SPAWN_SETTLE_POLL_S: float = 0.01

# How long shutdown waits for a fork it gave up on to unwind once cancelled.
# Cancelling create_subprocess_exec mid-fork has asyncio SIGKILL the child it
# already started and wait for it, which is milliseconds; this only bounds it.
_FORK_ABORT_WAIT_S: float = 1.0

# The row of a job whose fork shutdown cancelled. asyncio killed the worker
# process it had started, if any, but no worker_pgid was ever recorded.
FORK_ABORTED_ERROR: str = (
    "engine shutdown cancelled this job while its worker was still forking; "
    "the worker process, if one had started, was killed with it before any "
    "worker_pgid was recorded"
)

# A 'running' row this engine holds no worker for. Naming the two ways in is
# the point: "holds no worker" describes the engine's bookkeeping, which is
# the thing a reader already knows, and says nothing about the process that
# may still be running because of it.
ORPHANED_WORKER_ERROR: str = (
    "cancel found the row running with no worker held by this engine. The "
    "spawn window between the fork and the worker_pgid write is one way in, "
    "and a row claimed by a runner this process has since replaced is the "
    "other; either way a worker may still be running with nothing left that "
    "can identify it, so the outcome cannot be established from here"
)


class UnknownJobKind(LookupError):
    """No worker is registered for that kind."""


class WorkerProtocolError(ValueError):
    """The worker wrote something that is not the progress contract."""


# ----- registries --------------------------------------------------------
def register_worker(kind: str, builder: WorkerArgv) -> None:
    _WORKERS[kind] = builder


def unregister_worker(kind: str) -> None:
    _WORKERS.pop(kind, None)
    _RECONCILERS.pop(kind, None)
    _OBSERVERS.pop(kind, None)


def register_progress_observer(kind: str, hook: ProgressObserver) -> None:
    """Let a kind react, in the engine process, to its own progress lines.

    A worker is a SUBPROCESS. ``events.publish`` reaches the WS hub through a
    process-local slot, so a worker cannot emit a domain event however much it
    would like to -- the only channel it has is its stdout progress line.

    This is the other end of that channel. A batch kind whose items land one
    at a time (stems separating ten tracks) needs to say "track 7 is on disk
    NOW", not just "70%", and ``jobs.updated`` alone cannot carry that: the
    job row has no per-item slot. The observer sees the WHOLE parsed line, so
    a kind can put its own keys alongside ``progress`` and turn them into the
    domain event its readers already subscribe to.

    Deliberately NOT a general hook on the job row: it fires per progress
    line, in the runner's event loop, so it must stay cheap and must not
    block. An observer that raises is logged and swallowed -- a kind's
    bookkeeping is not permitted to fail the job whose work already
    succeeded.
    """
    _OBSERVERS[kind] = hook


def observe_progress(job: dict[str, Any], line: dict[str, Any]) -> None:
    hook = _OBSERVERS.get(job["kind"])
    if hook is None:
        return
    try:
        hook(job, line)
    except Exception:  # a side channel must never fail the job
        log.exception(
            "progress observer for kind %r raised on job %s",
            job["kind"],
            job["id"],
        )


def known_kinds() -> tuple[str, ...]:
    return tuple(sorted(_WORKERS))


def worker_argv(kind: str, payload: dict[str, Any]) -> list[str]:
    builder = _WORKERS.get(kind)
    if builder is None:
        raise UnknownJobKind(
            f"no worker registered for kind {kind!r}; known kinds: "
            f"{list(known_kinds())}"
        )
    return [str(part) for part in builder(payload)]


def register_reconcile(kind: str, hook: ReconcileHook) -> None:
    """Teach the engine how to discover what a lost worker actually did."""
    _RECONCILERS[kind] = hook


def reconcile(job: dict[str, Any]) -> str:
    """Resolve an 'unknown' row's real outcome, or stay honest about it.

    The stub answer is 'unknown'. No kind implements a hook yet, so every
    'unknown' row stays unknown and re-enqueue refuses -- which is the point:
    re-running a job whose side effects may already have landed is worse than
    making a human look.
    """
    hook = _RECONCILERS.get(job["kind"])
    if hook is None:
        return "unknown"
    result = hook(job)
    if result not in RECONCILE_RESULTS:
        raise ValueError(
            f"reconcile hook for {job['kind']!r} returned {result!r}; "
            f"expected one of {sorted(RECONCILE_RESULTS)}"
        )
    return result


def resolve_and_reenqueue(store: JobStore, job_id: str) -> dict[str, Any]:
    """Re-enqueue, refusing while the previous outcome is still unknown.

    The reconcile hook runs OUTSIDE the write transaction (it can do arbitrary
    IO and holding sqlite's write lock across it would stall every reader),
    so the verdict it returns is applied as a compare-and-swap against the
    exact row it was asked about. A caller whose row moved underneath it lost
    the race and is refused with JobConflict.
    """
    job = store.get(job_id)
    if job["status"] != "unknown":
        return store.reenqueue(job_id, expect_attempt=job["attempt"])
    resolved = reconcile(job)
    if resolved == "unknown":
        raise JobConflict(
            f"job {job_id} ended 'unknown' and no reconcile hook for kind "
            f"{job['kind']!r} could establish what the worker actually "
            "did. Re-enqueueing could repeat a side effect that already "
            "landed. Register a reconcile hook or resolve it by hand."
        )
    return store.resolve_and_requeue(
        job_id,
        expect_status="unknown",
        expect_attempt=job["attempt"],
        resolution=resolved,
        resolution_error=f"reconciled by the {job['kind']} hook as {resolved}",
    )


# ----- execution ---------------------------------------------------------
@dataclass
class _Worker:
    proc: asyncio.subprocess.Process
    identity: WorkerIdentity
    cancelled: bool = False
    stderr_tail: deque[str] = field(
        default_factory=lambda: deque(maxlen=_TAIL_LINES)
    )


class JobRunner:
    """Asyncio supervisor: claim queued rows, run them, report every write."""

    def __init__(
        self,
        store: JobStore,
        *,
        poll_s: float = 0.2,
        max_concurrent: int = 1,
        spawn_settle_s: float = _SPAWN_SETTLE_S,
    ) -> None:
        self.store = store
        self.poll_s = poll_s
        self.max_concurrent = max_concurrent
        self.spawn_settle_s = spawn_settle_s
        self._running: dict[str, _Worker] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._supervisor: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._supervisor is not None:
            raise RuntimeError("job runner already started")
        self._supervisor = asyncio.create_task(self._supervise())
        self._supervisor.add_done_callback(_log_supervisor_exit)

    async def stop(self) -> None:
        """Cancel every in-flight job before the engine lets go of the lock.

        Leaving workers running after shutdown would orphan process groups
        whose owner_boot_id is about to become foreign -- exactly the mess
        boot recovery exists to clean up. Cheaper to not create it.

        "Every in-flight job" means every job the supervisor CLAIMED, which is
        why the spawn window is settled first. Iterating only the workers this
        runner already holds skipped anything still forking, and the gather at
        the end then waited for that worker to finish by itself.
        """
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        still_forking = await self._settle_spawn_window()
        # Nothing may yield between the settle and the abort: a fork that
        # registered in that gap would be neither aborted nor walked below.
        await self._abort_forks(still_forking)
        for job_id in list(self._running):
            await self._cancel_for_shutdown(job_id)
        # An aborted fork's task has already ended, or is reported by
        # _abort_forks as stuck; either way the gather leaves it out, because
        # waiting on it unbounded outlived the shell's grace.
        waiting = [
            task
            for job_id, task in self._tasks.items()
            if job_id not in still_forking
        ]
        if waiting:
            await asyncio.gather(*waiting, return_exceptions=True)

    async def _settle_spawn_window(self) -> set[str]:
        """Wait for every claimed job to finish forking, so none is skipped.

        Returns the jobs still forking when the wait gave up (empty when every
        job registered), so :meth:`stop` can cancel their forks.

        ``_tasks`` is the claim-time truth and ``_running`` the
        registration-time one; between them sits the fork. A shutdown that
        walked ``_running`` alone walked straight past a job still inside that
        window -- nothing cancelled it, and the final gather then blocked
        until the worker finished on its own, with its process group outliving
        the engine that was supposed to own it.

        Waiting is what closes it, and the wait is short by construction:
        ``_run`` registers with nothing allowed to yield between the fork
        returning and the write, so a task leaves the window the instant exec
        returns. A job that somehow does not is reported by name -- the
        shutdown continues, because the alternative is abandoning the workers
        it CAN still cancel.
        """
        deadline = time.monotonic() + self.spawn_settle_s
        while True:
            forking = [
                job_id for job_id in self._tasks if job_id not in self._running
            ]
            if not forking:
                return set()
            if time.monotonic() >= deadline:
                log.error(
                    "shutdown waited %.0fs and jobs %s are still between the "
                    "fork and their registration; cancelling their forks",
                    self.spawn_settle_s,
                    forking,
                )
                return set(forking)
            await asyncio.sleep(_SPAWN_SETTLE_POLL_S)

    async def _abort_forks(self, job_ids: set[str]) -> None:
        """Cancel the jobs still forking after the settle window, boundedly.

        Abandoning them instead left the one process nothing can name: a
        forked worker whose worker_pgid is not written yet, leading its own
        session, so neither the shell's SIGKILL nor boot recovery reaches it.
        Cancelling the task while create_subprocess_exec is still awaiting its
        transport makes asyncio close that transport, which SIGKILLs the child
        it already started and waits for it. A task that has not started yet
        never forks at all. Either way its row is settled here, since _run
        never got as far as writing it.
        """
        forking = {
            job_id: self._tasks[job_id]
            for job_id in job_ids
            if job_id in self._tasks and job_id not in self._running
        }
        if not forking:
            return
        for task in forking.values():
            task.cancel()
        _, stuck = await asyncio.wait(forking.values(), timeout=_FORK_ABORT_WAIT_S)
        for job_id, task in forking.items():
            if task in stuck:
                log.error(
                    "job %s did not unwind within %.0fs of shutdown cancelling "
                    "its fork; its row is left for boot recovery",
                    job_id,
                    _FORK_ABORT_WAIT_S,
                )
                continue
            try:
                self.store.finish(job_id, "cancelled", error=FORK_ABORTED_ERROR)
            except (JobConflict, JobNotFound, sqlite3.Error) as exc:
                log.error(
                    "could not record job %s's aborted fork (%s); the row is "
                    "left for boot recovery",
                    job_id,
                    exc,
                )

    async def _cancel_for_shutdown(self, job_id: str) -> None:
        """cancel(), tolerating a row that finished on its own mid-shutdown.

        A raise here used to abandon the whole shutdown: the remaining jobs
        were never cancelled and the caller's close()/unbind() never ran.

        What is tolerated is the STATUS WRITE, never the kill. A row that
        raced us to a terminal status has a settled status and a still-live
        process, and leaving that process running is precisely the orphaned
        group this method exists to prevent -- so the worker is reaped either
        way, and only the row is left alone.
        """
        try:
            await self.cancel(job_id)
        except (JobConflict, JobNotFound) as exc:
            log.info(
                "shutdown left job %s's status alone (%s); reaping its worker "
                "anyway",
                job_id,
                exc,
            )
            await self._reap_without_writing(job_id)
        except (OSError, sqlite3.Error) as exc:
            log.error(
                "shutdown could not cancel job %s (%r); reaping its worker "
                "before the engine lets go of the lock",
                job_id,
                exc,
            )
            await self._reap_without_writing(job_id)

    async def _reap_without_writing(self, job_id: str) -> None:
        """Kill the worker group, leaving the row exactly as it stands."""
        worker = self._running.get(job_id)
        if worker is None:
            return
        # Stops _run racing us to a status it is no longer entitled to write.
        worker.cancelled = True
        outcome = await asyncio.to_thread(reap, worker.identity)
        await worker.proc.wait()
        log.info("shutdown reaped the worker for job %s: %s", job_id, outcome)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """queued -> cancelled outright, or running -> cancelling -> cancelled.

        A QUEUED row is settled in one step and never spawns anything: it has
        no worker, so there is no group whose death has to be proven. That
        transition is a compare-and-swap inside the store against the very
        BEGIN IMMEDIATE the supervisor claims through, so the cancel and the
        claim cannot both win. If the claim got there first, cancel_queued
        reports None and this falls through to the running path -- which is
        the correct answer, not a failure.
        """
        settled = self.store.cancel_queued(job_id)
        if settled is not None:
            return settled
        worker = self._running.get(job_id)
        self.store.begin_cancel(job_id)
        if worker is None:
            return self.store.finish(job_id, "unknown", error=ORPHANED_WORKER_ERROR)
        worker.cancelled = True
        outcome = await asyncio.to_thread(reap, worker.identity)
        # Reaps OUR zombie: the pgid a dead leader still holds is released
        # only once somebody waits on it, and this is that somebody.
        await worker.proc.wait()
        # group_has_live_member, NOT process_group_exists. The latter answers
        # yes for a group whose every member is a zombie, because the pgid
        # stays allocated until each corpse is waited on -- and a corpse this
        # engine did not fork is not one it can wait on. Reading that as
        # "still alive" ended an ordinary cancel of a just-finished worker as
        # 'unknown', which then refused its own re-enqueue.
        still_running = await asyncio.to_thread(
            group_has_live_member, worker.identity.pgid
        )
        if still_running:
            return self.store.finish(
                job_id,
                "unknown",
                error=(
                    "cancel could not confirm the group is dead; it still has "
                    f"a RUNNING member. {outcome}"
                ),
            )
        return self.store.finish(job_id, "cancelled", error=outcome)

    # ----- internals -----------------------------------------------------
    async def _supervise(self) -> None:
        """Claim and spawn until cancelled, surviving a failed drain.

        A store error used to kill this task outright, and with it every
        future job in the process: nothing else claims queued rows. So the
        loop logs and backs off instead. asyncio.CancelledError is a
        BaseException and passes straight through, which is what stop()
        relies on to shut the supervisor down.
        """
        backoff = 0.0
        while True:
            try:
                free = self.max_concurrent - self._in_flight()
                if free > 0:
                    # No await between sizing the drain, claiming against it
                    # and _spawn recording the slots: on one event loop that
                    # makes the reservation atomic with the claim it pays for.
                    for job in self.store.claim_queued(limit=free):
                        self._spawn(job)
            except _ERRORS as exc:
                backoff = min(
                    max(backoff * 2, _DRAIN_BACKOFF_S), _DRAIN_BACKOFF_MAX_S
                )
                log.error(
                    "engine job supervisor could not drain the queue, "
                    "retrying in %.1fs: %r",
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                continue
            backoff = 0.0
            await asyncio.sleep(self.poll_s)

    def _in_flight(self) -> int:
        """How many slots the supervisor has already spent.

        NOT ``len(self._running)``. A job appears there only once ``_run`` has
        got past ``create_subprocess_exec`` -- milliseconds of fork and exec,
        and several polls, after the row was claimed. Every poll inside that
        window saw a slot that was already spoken for, claimed another row and
        spawned another worker, so max_concurrent bounded nothing (C11).

        ``_tasks`` is written by ``_spawn`` in the same uninterrupted step as
        the claim, and cleared by the task's done callback, so it covers the
        WHOLE life of a job including the spawn window. The callback lands one
        loop iteration after the task ends, which errs toward reporting a slot
        as busy slightly too long -- the safe direction for a limit.
        """
        return len(self._tasks)

    def _spawn(self, job: dict[str, Any]) -> None:
        """Reserve the slot and start the run. Synchronous, on purpose."""
        job_id = job["id"]
        task = asyncio.create_task(self._guarded_run(job))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))

    async def _guarded_run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        try:
            await self._run(job)
        except _ERRORS as exc:
            self._record_failure(job_id, f"{type(exc).__name__}: {exc}")
        finally:
            self._running.pop(job_id, None)

    def _record_failure(self, job_id: str, error: str) -> None:
        """Last-chance terminal write for a run that blew up.

        If even THIS write fails there is nowhere left to put the outcome, so
        it goes to the log as loudly as possible and the row is left for boot
        recovery -- which is exactly the case recovery exists for. Swallowing
        it would leave a row 'running' with no worker and no explanation.
        """
        try:
            self.store.finish(job_id, "failed", error=error)
        except JobConflict as exc:
            # Already terminal: cancel() or _run got there first, and their
            # verdict is the true one. Not an error, but worth a trace.
            log.info(
                "job %s was already terminal when its run failed (%s); "
                "original error: %s",
                job_id,
                exc,
                error,
            )
        except (sqlite3.Error, JobNotFound) as exc:
            log.error(
                "could not record the failure of job %s (%s); the row is left "
                "for boot recovery. original error: %s",
                job_id,
                exc,
                error,
            )

    async def _run(self, job: dict[str, Any]) -> None:
        """Spawn the worker, then never return while its tree is still alive.

        Everything after the spawn is guarded: a store write failing here is a
        genuine possibility, and the row it fails is the ONLY record of the
        process group. Letting the exception out without killing the group
        would leave a live worker that nothing points at -- not the runner,
        which has dropped it, and not boot recovery, which reaps what the rows
        name. So the group dies first and the error propagates after, for
        _guarded_run to record.

        The order below is load-bearing, not incidental. From the instant
        create_subprocess_exec returns there is a live process that only
        ``worker_pgid`` can ever name again, so the write that records it is
        the first statement after the fork and NOTHING may yield before it.
        An await slipped into that window widens the one stretch in which an
        engine death orphans a worker beyond anything's reach -- see
        store.SPAWN_WINDOW_ERROR for what the next boot has to say about it.
        """
        job_id = job["id"]
        argv = worker_argv(job["kind"], job["payload"])
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        identity = WorkerIdentity.capture(proc.pid, argv)
        worker = _Worker(proc=proc, identity=identity)
        self._running[job_id] = worker
        try:
            self.store.record_worker(job_id, pid=proc.pid, identity=identity)
            await self._drive_worker(job_id, worker)
        except BaseException:
            await self._abandon(job_id, worker)
            raise

    async def _abandon(self, job_id: str, worker: _Worker) -> None:
        """Take the worker tree down after the run gave up on it."""
        worker.cancelled = True
        outcome = await asyncio.to_thread(reap, worker.identity)
        await worker.proc.wait()
        log.warning(
            "job %s abandoned its worker; reaped the group: %s", job_id, outcome
        )

    async def _drive_worker(self, job_id: str, worker: _Worker) -> None:
        """Read the worker to its end. The identity is ALREADY persisted here:
        _run writes it before this is entered, so the spawn window stays as
        narrow as it can be no matter what is added to the top of this."""
        proc = worker.proc
        identity = worker.identity

        # stderr is drained concurrently: waiting until stdout EOF would
        # deadlock the worker as soon as it filled the 64K stderr pipe.
        stderr_task = asyncio.create_task(
            _collect(proc.stderr, worker.stderr_tail)
        )
        stdout_tail: deque[str] = deque(maxlen=_TAIL_LINES)
        malformed = await self._pump(job_id, proc, stdout_tail)
        await stderr_task

        if malformed is not None:
            outcome = await asyncio.to_thread(reap, identity)
            await proc.wait()
            if worker.cancelled:
                return
            self.store.finish(
                job_id,
                "failed",
                error=_error_blob(malformed, stdout_tail, worker.stderr_tail)
                + f" {outcome}",
            )
            return

        returncode = await proc.wait()
        if worker.cancelled:
            # cancel() owns the terminal write; it is still confirming the
            # group is dead and must not race us to a status.
            return
        if returncode != 0:
            self.store.finish(
                job_id,
                "failed",
                error=_error_blob(
                    f"worker exited {returncode}",
                    stdout_tail,
                    worker.stderr_tail,
                ),
            )
            return
        self.store.set_progress(job_id, 1.0, "done")
        self.store.finish(job_id, "succeeded")

    async def _pump(
        self,
        job_id: str,
        proc: asyncio.subprocess.Process,
        tail: deque[str],
    ) -> str | None:
        """Read the progress protocol. Returns None, or why it broke."""
        stream = proc.stdout
        if stream is None:
            raise RuntimeError("worker was spawned without a stdout pipe")
        writer = _ProgressWriter(self.store, job_id, PROGRESS_WRITE_INTERVAL_S)
        reader = asyncio.create_task(self._read_progress(stream, tail, writer))
        try:
            # Race the reader against the writer: a failed write must fail
            # the run NOW, as the inline write did, not when the worker next
            # prints a line or exits. A worker that goes quiet after its last
            # line would otherwise keep running on a store that is refusing
            # writes.
            await asyncio.wait(
                {reader, writer.stopped}, return_when=asyncio.FIRST_COMPLETED
            )
            if not reader.done():
                writer.raise_if_stopped()
            broken = reader.result()
        except BaseException:
            reader.cancel()
            await asyncio.wait([reader])
            await writer.abort()
            raise
        # Every line read is on disk before the caller writes a terminal
        # status, so no late progress write can land after it. Cancelled
        # mid-flush (shutdown or a job cancel), the in-flight write still has
        # to land before cancellation reaches the caller's terminal write.
        try:
            await writer.close()
        except asyncio.CancelledError:
            await writer.abort()
            raise
        return broken

    @staticmethod
    async def _read_progress(
        stream: asyncio.StreamReader,
        tail: deque[str],
        writer: _ProgressWriter,
    ) -> str | None:
        while True:
            try:
                raw = await stream.readline()
            except ValueError as exc:
                # asyncio raises when a single line exceeds the buffer limit.
                return f"worker wrote an oversized line: {exc}"
            if not raw:
                return None
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            tail.append(line)
            try:
                progress, message, parsed = _parse_progress(line)
            except WorkerProtocolError as exc:
                return str(exc)
            writer.offer(progress, message, parsed)


class _ProgressWriter:
    """Coalesce one job's progress lines into rate-bounded, off-loop writes.

    The pump only queues a line; this task writes the LATEST queued line in a
    worker thread, then waits out ``interval_s`` before the next write. Every
    queued line is then handed to the kind's observer, in order, on the row
    that write produced -- so an observer still sees every line, and still
    never announces a line the store has not recorded.

    A failed write is not swallowed: the pump waits on ``stopped`` alongside
    its reader and re-raises the error the moment the write fails, so the
    runner fails the job and reaps the worker exactly as it did when the
    write ran inline. ``offer`` and ``close`` re-raise it too.
    """

    def __init__(self, store: JobStore, job_id: str, interval_s: float) -> None:
        self._store = store
        self._job_id = job_id
        self._interval_s = interval_s
        self._pending: list[tuple[float, str | None, dict[str, Any]]] = []
        self._wake = asyncio.Event()
        self._closed = asyncio.Event()
        self._task = asyncio.create_task(self._drain())

    @property
    def stopped(self) -> asyncio.Task[None]:
        """The drain task. It finishes before close() only on a failed write."""
        return self._task

    def raise_if_stopped(self) -> None:
        if self._task.done():
            # Raises the write error that stopped it; a clean stop before
            # close() is impossible, so that is a bug worth naming.
            self._task.result()
            raise RuntimeError(
                f"progress writer for job {self._job_id} stopped before its "
                "worker did"
            )

    def offer(
        self, progress: float, message: str | None, parsed: dict[str, Any]
    ) -> None:
        self.raise_if_stopped()
        self._pending.append((progress, message, parsed))
        self._wake.set()

    async def close(self) -> None:
        """Flush what is still queued, then stop. Raises a failed write.

        Shielded: cancelling the caller must not cancel the drain task, whose
        write thread would then commit behind the caller's back (see abort).
        """
        self._closed.set()
        self._wake.set()
        await asyncio.shield(self._task)

    async def abort(self) -> None:
        """The run is already failing: drop what is queued, keep its own error.

        A write already handed to its thread cannot be called back, and
        cancelling the task that awaits it only stops the WAITING: the thread
        still commits, possibly after the caller's terminal write, leaving a
        progress update and its event on a finished row. So abort drains the
        in-flight write to completion instead of cancelling, and the caller's
        terminal write is always the last word on the row.
        """
        self._pending.clear()
        self._closed.set()
        self._wake.set()
        await asyncio.wait([self._task])
        if not self._task.cancelled() and self._task.exception() is not None:
            log.error(
                "progress writer for job %s had also failed: %r",
                self._job_id,
                self._task.exception(),
            )

    async def _drain(self) -> None:
        while self._pending or not self._closed.is_set():
            await self._wake.wait()
            self._wake.clear()
            await self._write_pending()
            if self._closed.is_set():
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._closed.wait(), self._interval_s)

    async def _write_pending(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        progress, message, _parsed = batch[-1]
        row = await asyncio.to_thread(
            self._store.set_progress, self._job_id, progress, message
        )
        for _progress, _message, parsed in batch:
            observe_progress(row, parsed)


def _parse_progress(line: str) -> tuple[float, str | None, dict[str, Any]]:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as exc:
        raise WorkerProtocolError(
            f"worker line is not JSON ({exc.msg})"
        ) from exc
    if not isinstance(parsed, dict):
        raise WorkerProtocolError("worker line is not a JSON object")
    if "progress" not in parsed:
        raise WorkerProtocolError("worker line has no 'progress' key")
    raw_progress = parsed["progress"]
    if isinstance(raw_progress, bool) or not isinstance(
        raw_progress, (int, float)
    ):
        raise WorkerProtocolError(
            f"worker 'progress' is {raw_progress!r}, expected a number"
        )
    message = parsed.get("message")
    if message is not None and not isinstance(message, str):
        raise WorkerProtocolError(
            f"worker 'message' is {message!r}, expected a string"
        )
    # The whole object comes back, not just the two contract keys: a kind's
    # own keys ride alongside them and are handed to its progress observer.
    return float(raw_progress), message, parsed


async def _collect(
    stream: asyncio.StreamReader | None, sink: deque[str]
) -> None:
    if stream is None:
        return
    while True:
        raw = await stream.readline()
        if not raw:
            return
        sink.append(raw.decode("utf-8", errors="replace").rstrip())


def _error_blob(
    reason: str, stdout_tail: deque[str], stderr_tail: deque[str]
) -> str:
    parts = [reason]
    if stdout_tail:
        parts.append("stdout tail: " + " | ".join(stdout_tail))
    if stderr_tail:
        parts.append("stderr tail: " + " | ".join(stderr_tail))
    return ". ".join(parts)


def _log_supervisor_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("engine job supervisor died, jobs stop draining: %r", exc)


__all__ = [
    "FORK_ABORTED_ERROR",
    "ORPHANED_WORKER_ERROR",
    "PROGRESS_WRITE_INTERVAL_S",
    "RECONCILE_RESULTS",
    "JobRunner",
    "ProgressObserver",
    "ReconcileHook",
    "UnknownJobKind",
    "WorkerArgv",
    "WorkerProtocolError",
    "known_kinds",
    "observe_progress",
    "reconcile",
    "register_progress_observer",
    "register_reconcile",
    "register_worker",
    "resolve_and_reenqueue",
    "unregister_worker",
    "worker_argv",
]
