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
import json
import logging
import sqlite3
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

RECONCILE_RESULTS: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "unknown"}
)

_WORKERS: dict[str, WorkerArgv] = {}
_RECONCILERS: dict[str, ReconcileHook] = {}

_TAIL_LINES: int = 20

# sqlite3.Error is NOT an OSError, so without it here a store write failing
# mid-run escaped the guard entirely and left the row 'running' forever with
# no worker behind it.
_ERRORS = (OSError, ValueError, RuntimeError, LookupError, sqlite3.Error)

# The supervisor is the ONLY thing draining the queue, so a transient store
# error must not end queue supervision for the life of the process. It backs
# off instead of dying, and resets the moment a drain succeeds.
_DRAIN_BACKOFF_S: float = 1.0
_DRAIN_BACKOFF_MAX_S: float = 30.0


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
    ) -> None:
        self.store = store
        self.poll_s = poll_s
        self.max_concurrent = max_concurrent
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
        """
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        for job_id in list(self._running):
            await self._cancel_for_shutdown(job_id)
        if self._tasks:
            await asyncio.gather(
                *self._tasks.values(), return_exceptions=True
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
            return self.store.finish(
                job_id,
                "unknown",
                error=(
                    "cancel requested but this engine holds no worker for the "
                    "row; the outcome cannot be established from here"
                ),
            )
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
        genuine possibility (that is C5), and the row it fails is the ONLY
        record of the process group. Letting the exception out without killing
        the group would leave a live worker that nothing points at -- not the
        runner, which has dropped it, and not boot recovery, which reaps what
        the rows name. So the group dies first and the error propagates after,
        for _guarded_run to record.
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
        proc = worker.proc
        identity = worker.identity
        self.store.record_worker(job_id, pid=proc.pid, identity=identity)

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
                progress, message = _parse_progress(line)
            except WorkerProtocolError as exc:
                return str(exc)
            self.store.set_progress(job_id, progress, message)


def _parse_progress(line: str) -> tuple[float, str | None]:
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
    return float(raw_progress), message


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
    "RECONCILE_RESULTS",
    "JobRunner",
    "ReconcileHook",
    "UnknownJobKind",
    "WorkerArgv",
    "WorkerProtocolError",
    "known_kinds",
    "reconcile",
    "register_reconcile",
    "register_worker",
    "resolve_and_reenqueue",
    "unregister_worker",
    "worker_argv",
]
