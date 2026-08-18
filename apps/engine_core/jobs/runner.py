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
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.engine_core.jobs.reap import (
    WorkerIdentity,
    process_group_exists,
    reap,
)
from apps.engine_core.jobs.store import JobConflict, JobStore

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
_ERRORS = (OSError, ValueError, RuntimeError, LookupError)


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
    """Re-enqueue, refusing while the previous outcome is still unknown."""
    job = store.get(job_id)
    if job["status"] == "unknown":
        resolved = reconcile(job)
        if resolved == "unknown":
            raise JobConflict(
                f"job {job_id} ended 'unknown' and no reconcile hook for kind "
                f"{job['kind']!r} could establish what the worker actually "
                "did. Re-enqueueing could repeat a side effect that already "
                "landed. Register a reconcile hook or resolve it by hand."
            )
        store.finish(
            job_id,
            resolved,
            error=f"reconciled by the {job['kind']} hook as {resolved}",
        )
    return store.reenqueue(job_id)


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
            await self.cancel(job_id)
        if self._tasks:
            await asyncio.gather(
                *self._tasks.values(), return_exceptions=True
            )

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """running -> cancelling -> (group proven dead) -> cancelled."""
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
        await worker.proc.wait()
        still_alive = await asyncio.to_thread(
            process_group_exists, worker.identity.pgid
        )
        if still_alive:
            return self.store.finish(
                job_id,
                "unknown",
                error=f"cancel could not confirm the group is dead. {outcome}",
            )
        return self.store.finish(job_id, "cancelled", error=outcome)

    # ----- internals -----------------------------------------------------
    async def _supervise(self) -> None:
        while True:
            free = self.max_concurrent - len(self._running)
            if free > 0:
                for job in self.store.claim_queued(limit=free):
                    self._spawn(job)
            await asyncio.sleep(self.poll_s)

    def _spawn(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        task = asyncio.create_task(self._guarded_run(job))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._tasks.pop(jid, None))

    async def _guarded_run(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        try:
            await self._run(job)
        except _ERRORS as exc:
            self.store.finish(
                job_id, "failed", error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            self._running.pop(job_id, None)

    async def _run(self, job: dict[str, Any]) -> None:
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
