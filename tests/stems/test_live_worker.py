"""Real scheduling tests for the future live-stems boundary (issue #12448).

The inference dependencies below are deterministic test doubles, not generators.
[if] inference work crosses the seam [then] isolate and serialize it, [else stop].
"""

from __future__ import annotations

import json
import threading
from dataclasses import FrozenInstanceError, replace
from typing import Literal, cast

import pytest

from apps.stems.live_worker import (
    LiveStemsJob,
    LiveStemsOutcome,
    LiveStemsRunner,
    LiveStemsWorker,
    LiveStemsWorkerError,
)

pytestmark = pytest.mark.requirement("RB-PERFORMANCE-12869")


class _Worker:
    kind: Literal["live-stems-inference"] = "live-stems-inference"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.threads: list[threading.Thread] = []
        self.jobs: list[LiveStemsJob] = []

    def run(self, job: LiveStemsJob) -> str:
        self.jobs.append(job)
        self.threads.append(threading.current_thread())
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("test worker barrier was not released")
        if job.source_ref == "raise":
            raise ValueError()
        if job.source_ref == "invalid-output":
            return cast(str, object())
        return f"test-output:{job.job_id}"


class _AudioCallback:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class _TransportCallback(_AudioCallback):
    pass


def _job(job_id: str = "one", source_ref: str = "track:one") -> LiveStemsJob:
    return LiveStemsJob(job_id=job_id, source_ref=source_ref, start_ms=0, duration_ms=100)


def test_submit_uses_dedicated_serial_worker() -> None:
    """[if] jobs are submitted [then] execution is serial and off-thread, [else stop]."""
    worker = _Worker()
    with LiveStemsRunner(worker, max_pending_jobs=2) as runner:
        try:
            assert runner.submit(_job()) == "one"
            assert worker.started.wait(5)
            assert runner.submit(_job("two")) == "two"
            assert [job.job_id for job in worker.jobs] == ["one"]
            assert worker.threads[0] is not threading.current_thread()
            assert worker.threads[0].name.startswith("live-stems")
        finally:
            worker.release.set()
        for job_id in ("one", "two"):
            outcome = runner.result(job_id, timeout=5)
            assert outcome == LiveStemsOutcome(job_id, "completed", f"test-output:{job_id}")
    assert worker.threads[0] is worker.threads[1]
    assert not worker.threads[0].is_alive()


@pytest.mark.parametrize("callback_type", [_AudioCallback, _TransportCallback])
def test_callbacks_are_rejected_as_jobs_and_workers(callback_type: type[_AudioCallback]) -> None:
    """[if] a P0 callback reaches the boundary [then] reject without invoking it, [else stop]."""
    callback = callback_type()
    with pytest.raises(LiveStemsWorkerError, match="inference worker"):
        LiveStemsRunner(cast(LiveStemsWorker, callback), max_pending_jobs=1)
    worker = _Worker()
    worker.release.set()
    with LiveStemsRunner(worker, max_pending_jobs=1) as runner:
        with pytest.raises(LiveStemsWorkerError, match="LiveStemsJob"):
            runner.submit(cast(LiveStemsJob, callback))
        assert not worker.jobs
        runner.submit(_job())
        assert runner.result("one", timeout=5).status == "completed"
    assert callback.calls == 0


@pytest.mark.parametrize("value", [object(), {}, lambda: None])
def test_untyped_work_is_rejected(value: object) -> None:
    """[if] untyped work is supplied [then] reject jobs and worker dependencies, [else stop]."""
    with pytest.raises(LiveStemsWorkerError):
        LiveStemsRunner(cast(LiveStemsWorker, value), max_pending_jobs=1)
    with LiveStemsRunner(_Worker(), max_pending_jobs=1) as runner, pytest.raises(LiveStemsWorkerError):
        runner.submit(cast(LiveStemsJob, value))


@pytest.mark.parametrize("callback_type", [_AudioCallback, _TransportCallback])
@pytest.mark.parametrize("field", ["job_id", "source_ref", "start_ms", "duration_ms", "kind"])
def test_callback_payloads_are_rejected(field: str, callback_type: type[_AudioCallback]) -> None:
    """[if] callbacks occupy scalar fields [then] strict deserialization refuses, [else stop]."""
    callback = callback_type()
    data = _job().to_dict()
    data[field] = callback
    with pytest.raises(LiveStemsWorkerError):
        LiveStemsJob.from_dict(data)
    assert callback.calls == 0


@pytest.mark.parametrize("field", ["audio_callback", "transport_callback", "payload"])
def test_extra_payload_keys_are_rejected(field: str) -> None:
    """[if] a payload has callback or unknown keys [then] reject before execution, [else stop]."""
    callback = _AudioCallback()
    with pytest.raises(LiveStemsWorkerError):
        LiveStemsJob.from_dict({**_job().to_dict(), field: callback})
    assert callback.calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", ""),
        ("source_ref", " "),
        ("start_ms", -1),
        ("start_ms", True),
        ("start_ms", 0.0),
        ("duration_ms", 0),
        ("duration_ms", -1),
        ("duration_ms", False),
        ("duration_ms", 1.0),
        ("kind", "audio-callback"),
        ("kind", "transport"),
    ],
)
def test_invalid_job_fields_are_rejected(field: str, value: object) -> None:
    """[if] job fields violate the scalar contract [then] fail explicitly, [else stop]."""
    with pytest.raises(LiveStemsWorkerError):
        LiveStemsJob.from_dict({**_job().to_dict(), field: value})


def test_submit_revalidates_frozen_job() -> None:
    """[if] construction validation is bypassed [then] submission still refuses, [else stop]."""
    job = _job()
    callback = _TransportCallback()
    object.__setattr__(job, "source_ref", callback)
    worker = _Worker()
    with LiveStemsRunner(worker, max_pending_jobs=1) as runner, pytest.raises(LiveStemsWorkerError):
        runner.submit(job)
    assert not worker.jobs
    assert callback.calls == 0


@pytest.mark.parametrize("source_ref", ["track:one", "raise", "invalid-output"])
def test_terminal_outcome_and_recovery(source_ref: str) -> None:
    """[if] work succeeds or fails [then] report correlated terminal data and recover, [else stop]."""
    worker = _Worker()
    worker.release.set()
    with LiveStemsRunner(worker, max_pending_jobs=1) as runner:
        runner.submit(_job(source_ref=source_ref))
        outcome = runner.result("one", timeout=5)
        if source_ref == "track:one":
            assert outcome == LiveStemsOutcome("one", "completed", "test-output:one")
        elif source_ref == "raise":
            assert outcome == LiveStemsOutcome("one", "failed", error_type="ValueError", error_message="")
        elif source_ref == "invalid-output":
            assert outcome.status == "failed"
            assert outcome.output_ref is None
            assert outcome.error_type == "LiveStemsWorkerError"
            assert outcome.error_message
        runner.submit(_job("next"))
        assert runner.result("next", timeout=5).status == "completed"
    assert LiveStemsOutcome.from_dict(json.loads(json.dumps(outcome.to_dict(), allow_nan=False))) == outcome


def test_contracts_are_immutable_and_strictly_serializable() -> None:
    """[if] job and outcome cross JSON [then] retain immutable scalar contracts, [else stop]."""
    job = _job()
    assert LiveStemsJob.from_dict(json.loads(json.dumps(job.to_dict(), allow_nan=False))) == job
    with pytest.raises(FrozenInstanceError):
        job.job_id = "changed"  # type: ignore[misc]  # Assert frozen runtime enforcement.
    outcome = LiveStemsOutcome("one", "completed", "output:one")
    with pytest.raises(FrozenInstanceError):
        outcome.status = "failed"  # type: ignore[misc]  # Assert frozen runtime enforcement.
    for contract in (job, outcome):
        with pytest.raises(LiveStemsWorkerError):
            type(contract).from_dict({})
        with pytest.raises(LiveStemsWorkerError):
            type(contract).from_dict({**contract.to_dict(), "extra": 1})
        with pytest.raises(LiveStemsWorkerError):
            type(contract).from_dict([])


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "pending"},
        {"job_id": ""},
        {"output_ref": ""},
        {"error_type": "ValueError"},
        {"error_message": "oops"},
        {"status": "failed"},
        {"status": "failed", "output_ref": None, "error_type": "", "error_message": "oops"},
        {"status": "failed", "output_ref": None, "error_type": "ValueError"},
        {"output_ref": _AudioCallback()},
    ],
)
def test_inconsistent_outcomes_are_rejected(fields: dict[str, object]) -> None:
    """[if] terminal fields disagree [then] refuse malformed outcome data, [else stop]."""
    data = LiveStemsOutcome("one", "completed", "output:one").to_dict()
    with pytest.raises(LiveStemsWorkerError):
        LiveStemsOutcome.from_dict({**data, **fields})


def test_capacity_timeout_and_handle_lifecycle() -> None:
    """[if] handles are busy or unknown [then] refuse without losing accepted work, [else stop]."""
    worker = _Worker()
    with LiveStemsRunner(worker, max_pending_jobs=1) as runner:
        try:
            runner.submit(_job())
            assert worker.started.wait(5)
            with pytest.raises(LiveStemsWorkerError, match="duplicate"):
                runner.submit(_job())
            with pytest.raises(LiveStemsWorkerError, match="capacity"):
                runner.submit(_job("two"))
            with pytest.raises(TimeoutError):
                runner.result("one", timeout=0)
            with pytest.raises(LiveStemsWorkerError, match="unknown"):
                runner.result("missing", timeout=0)
        finally:
            worker.release.set()
        assert runner.result("one", timeout=5).status == "completed"
        with pytest.raises(LiveStemsWorkerError, match="unknown"):
            runner.result("one", timeout=0)
        runner.submit(_job())
        assert runner.result("one", timeout=5).status == "completed"


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), True, "1", None])
def test_invalid_result_timeout(timeout: object) -> None:
    """[if] result timeout is invalid [then] refuse and retain the job, [else stop]."""
    worker = _Worker()
    worker.release.set()
    with LiveStemsRunner(worker, max_pending_jobs=1) as runner:
        runner.submit(_job())
        with pytest.raises(LiveStemsWorkerError, match="timeout"):
            runner.result("one", timeout=cast(float, timeout))
        assert runner.result("one", timeout=5).status == "completed"


@pytest.mark.parametrize("bound", [0, -1, True, 1.0, None])
def test_invalid_capacity(bound: object) -> None:
    """[if] pending capacity is not a positive integer [then] refuse construction, [else stop]."""
    with pytest.raises(LiveStemsWorkerError, match="max_pending_jobs"):
        LiveStemsRunner(_Worker(), max_pending_jobs=cast(int, bound))


def test_close_drains_and_is_idempotent() -> None:
    """[if] the runner closes [then] drain accepted work and reject new jobs, [else stop]."""
    worker = _Worker()
    worker.release.set()
    runner = LiveStemsRunner(worker, max_pending_jobs=2)
    runner.submit(_job())
    runner.submit(replace(_job(), job_id="two"))
    runner.close()
    runner.close()
    assert all(not thread.is_alive() for thread in worker.threads)
    assert runner.result("one", timeout=0).status == "completed"
    assert runner.result("two", timeout=0).status == "completed"
    with pytest.raises(LiveStemsWorkerError, match="closed"):
        runner.submit(_job("three"))
