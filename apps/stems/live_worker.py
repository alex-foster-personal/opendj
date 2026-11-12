"""Dedicated future live-stems scheduling boundary (issue #12448, PERFBATCH-06).

Implements the worker separation in (private path), CPU budget.
No inference, model loading, audio context, transport or batch-worker dependency.
The injected inference implementation is trusted to honor its declared role.
Thread isolation alone proves no CPU, GIL, XRUN or audible-latency budget.

Verified requirements:
  [if] an inference job is submitted [then] run on the owned worker thread.
  [if] audio or transport callbacks are supplied [then] reject before enqueue.
  [if] work completes or fails [then] return immutable, serializable terminal data.

Submission, result waiting and shutdown belong to the control plane, never P0.
"""

from __future__ import annotations

import math
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from types import TracebackType
from typing import Literal, Protocol, cast

InferenceKind = Literal["live-stems-inference"]
OutcomeStatus = Literal["completed", "failed"]


class LiveStemsWorkerError(ValueError):
    """Invalid live-stems boundary data, worker, capacity or handle lifecycle."""


def _require_string(value: object, field: str, *, allow_empty: bool = False) -> None:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise LiveStemsWorkerError(f"{field} must be a {'string' if allow_empty else 'nonempty string'}")


def _require_fields(data: object, fields: set[str]) -> dict[str, object]:
    if type(data) is not dict or data.keys() != fields:
        raise LiveStemsWorkerError(f"expected exactly these fields: {sorted(fields)}")
    return cast(dict[str, object], data)


@dataclass(frozen=True)
class LiveStemsJob:
    """Inference-only scalar request; source_ref is opaque and never opened here."""

    job_id: str
    source_ref: str
    start_ms: int
    duration_ms: int
    kind: InferenceKind = "live-stems-inference"

    def __post_init__(self) -> None:
        _require_string(self.job_id, "job_id")
        _require_string(self.source_ref, "source_ref")
        _require_string(self.kind, "kind")
        if self.kind != "live-stems-inference":
            raise LiveStemsWorkerError("kind must be live-stems-inference")
        for field, value, minimum in (("start_ms", self.start_ms, 0), ("duration_ms", self.duration_ms, 1)):
            if type(value) is not int or value < minimum:
                raise LiveStemsWorkerError(f"{field} must be an integer >= {minimum}")

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "source_ref": self.source_ref,
            "start_ms": self.start_ms,
            "duration_ms": self.duration_ms,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: object) -> LiveStemsJob:
        fields = _require_fields(data, {"job_id", "source_ref", "start_ms", "duration_ms", "kind"})
        return cls(
            job_id=cast(str, fields["job_id"]),
            source_ref=cast(str, fields["source_ref"]),
            start_ms=cast(int, fields["start_ms"]),
            duration_ms=cast(int, fields["duration_ms"]),
            kind=cast(InferenceKind, fields["kind"]),
        )


class LiveStemsWorker(Protocol):
    """Future inference dependency, deliberately not a generic callable executor."""

    @property
    def kind(self) -> InferenceKind: ...

    def run(self, job: LiveStemsJob) -> str:
        """Return an opaque output reference or raise an ordinary inference error."""
        ...


@dataclass(frozen=True)
class LiveStemsOutcome:
    """Terminal job data with no exceptions, traceback objects or callback handles."""

    job_id: str
    status: OutcomeStatus
    output_ref: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.job_id, "job_id")
        _require_string(self.status, "status")
        if self.status == "completed":
            _require_string(self.output_ref, "output_ref")
            if self.error_type is not None or self.error_message is not None:
                raise LiveStemsWorkerError("completed outcome cannot contain error fields")
        elif self.status == "failed":
            _require_string(self.error_type, "error_type")
            _require_string(self.error_message, "error_message", allow_empty=True)
            if self.output_ref is not None:
                raise LiveStemsWorkerError("failed outcome cannot contain output_ref")
        else:
            raise LiveStemsWorkerError("status must be completed or failed")

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "output_ref": self.output_ref,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: object) -> LiveStemsOutcome:
        fields = _require_fields(data, {"job_id", "status", "output_ref", "error_type", "error_message"})
        return cls(
            job_id=cast(str, fields["job_id"]),
            status=cast(OutcomeStatus, fields["status"]),
            output_ref=cast(str | None, fields["output_ref"]),
            error_type=cast(str | None, fields["error_type"]),
            error_message=cast(str | None, fields["error_message"]),
        )


class LiveStemsRunner:
    """Bounded, dedicated serial inference worker, polled by control-plane callers.

    Capacity includes running, queued and completed-but-uncollected jobs.
    Result timeout leaves the handle available for collection. No futures or
    callback registration escape this boundary. close() drains accepted work;
    completed results remain collectable after shutdown.
    """

    def __init__(self, worker: LiveStemsWorker, *, max_pending_jobs: int) -> None:
        if getattr(worker, "kind", None) != "live-stems-inference" or not callable(getattr(worker, "run", None)):
            raise LiveStemsWorkerError("expected an inference worker with kind live-stems-inference and run(job)")
        if type(max_pending_jobs) is not int or max_pending_jobs <= 0:
            raise LiveStemsWorkerError("max_pending_jobs must be a positive integer")
        self._worker = worker
        self._max_pending_jobs = max_pending_jobs
        self._pending: dict[str, Future[LiveStemsOutcome]] = {}
        self._closed = False
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-stems")

    def submit(self, job: LiveStemsJob) -> str:
        """Validate and enqueue scalar inference work without waiting for execution."""
        if type(job) is not LiveStemsJob:
            raise LiveStemsWorkerError("expected a LiveStemsJob, never a callback or arbitrary work object")
        # Revalidate and snapshot even if a caller bypassed frozen construction.
        job = LiveStemsJob.from_dict(job.to_dict())
        with self._lock:
            if self._closed:
                raise LiveStemsWorkerError("runner is closed")
            if job.job_id in self._pending:
                raise LiveStemsWorkerError(f"duplicate outstanding job: {job.job_id}")
            if len(self._pending) >= self._max_pending_jobs:
                raise LiveStemsWorkerError("runner capacity reached; collect a result before submitting")
            self._pending[job.job_id] = self._executor.submit(self._run, job)
        return job.job_id

    def _run(self, job: LiveStemsJob) -> LiveStemsOutcome:
        try:
            output_ref = self._worker.run(job)
            return LiveStemsOutcome(job.job_id, "completed", output_ref=output_ref)
        except Exception as exc:  # noqa: BLE001 - serialize inference failures at the worker boundary.
            return LiveStemsOutcome(job.job_id, "failed", error_type=type(exc).__name__, error_message=str(exc))

    def result(self, job_id: str, *, timeout: float) -> LiveStemsOutcome:
        """Collect exactly once; waiting is control-plane work, never audio work."""
        _require_string(job_id, "job_id")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise LiveStemsWorkerError("timeout must be finite and nonnegative")
        with self._lock:
            future = self._pending.get(job_id)
            if future is None:
                raise LiveStemsWorkerError(f"unknown job: {job_id}")
        outcome = future.result(timeout=timeout)
        with self._lock:
            if self._pending.get(job_id) is not future:
                raise LiveStemsWorkerError(f"unknown or already collected job: {job_id}")
            del self._pending[job_id]
        return outcome

    def close(self) -> None:
        """Idempotently drain work and join the owned thread from the control plane."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)

    def __enter__(self) -> LiveStemsRunner:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
