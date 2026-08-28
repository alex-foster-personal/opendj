"""Status transitions: what may overwrite what, and who wins a race.

Two claims are pinned here, both of them about a read-modify-write that used
to span several store calls with nothing holding the row still in between:

  - if two callers both resolve the same 'unknown' row then exactly ONE may
    act on that reading, or the loser re-queues a job the winner already put
    back in flight (two workers, one row, and the first worker's pgid NULLed
    so nothing can reap it)
  - if finish() writes over a status it has no business writing over then the
    outcome that actually happened is silently erased

The race test forces the interleave with a barrier inside the reconcile hook
rather than hoping the scheduler produces it. A race that only fails one run
in fifty is not a regression test.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.jobs.runner import (
    register_reconcile,
    resolve_and_reenqueue,
    unregister_worker,
)
from apps.engine_core.jobs.store import JobConflict, JobNotFound, JobStore


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
    store.recover()
    return store


def _unknown_row(store: JobStore, kind: str) -> dict[str, Any]:
    """A row in exactly the state the reconcile path exists to resolve."""
    job = store.enqueue(kind, {})
    store.claim_queued()
    return store.finish(job["id"], "unknown", error="worker outcome unknown")


def test_concurrent_resolve_lets_exactly_one_caller_through(
    tmp_path: Path,
) -> None:
    """Two threads, one 'unknown' row: the loser is refused, not merged.

    Both callers read the SAME row and both reconcile it. Before the
    compare-and-swap both then wrote: the winner re-queued (attempt 1) and the
    supervisor was free to claim it, then the loser's stale finish() forced
    that running row through failed -> queued a second time (attempt 2),
    spawning a second worker while wiping the first worker's pgid.
    """
    store = _store(tmp_path)
    job = _unknown_row(store, "racy")
    both_read = threading.Barrier(2, timeout=30)

    def _hook(_row: dict[str, Any]) -> str:
        both_read.wait()  # neither caller may write until both have read
        return "failed"

    register_reconcile("racy", _hook)
    results: list[dict[str, Any]] = []
    refusals: list[JobConflict] = []
    lock = threading.Lock()

    def _call() -> None:
        try:
            outcome = resolve_and_reenqueue(store, job["id"])
        except JobConflict as exc:
            with lock:
                refusals.append(exc)
        else:
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "resolve deadlocked"
    finally:
        unregister_worker("racy")

    assert len(results) == 1, f"both callers re-enqueued: {results}"
    assert len(refusals) == 1, f"the losing caller was not refused: {refusals}"
    row = store.get(job["id"])
    assert row["status"] == "queued", row
    assert row["attempt"] == 1, f"the row was re-enqueued twice: {row}"
    store.close()


def test_resolve_refuses_when_the_row_moved_under_the_verdict(
    tmp_path: Path,
) -> None:
    """The reconcile verdict is only valid for the row it was asked about."""
    store = _store(tmp_path)
    job = _unknown_row(store, "moved")

    def _hook(_row: dict[str, Any]) -> str:
        # Simulates the slow-hook window: the row is resolved and re-queued by
        # somebody else while this caller is still deciding.
        store.resolve_and_requeue(
            job["id"],
            expect_status="unknown",
            expect_attempt=0,
            resolution="failed",
            resolution_error="resolved by the other caller",
        )
        return "succeeded"

    register_reconcile("moved", _hook)
    try:
        with pytest.raises(JobConflict) as refusal:
            resolve_and_reenqueue(store, job["id"])
    finally:
        unregister_worker("moved")
    assert "resolved it first" in str(refusal.value), str(refusal.value)
    assert store.get(job["id"])["attempt"] == 1
    store.close()


def test_finish_refuses_to_overwrite_a_terminal_row(tmp_path: Path) -> None:
    """A second terminal write erases what actually happened the first time."""
    store = _store(tmp_path)
    job = store.enqueue("done-once", {})
    store.claim_queued()
    store.finish(job["id"], "succeeded")

    with pytest.raises(JobConflict) as refusal:
        store.finish(job["id"], "failed", error="a late, wrong verdict")
    assert "succeeded" in str(refusal.value), str(refusal.value)
    assert store.get(job["id"])["status"] == "succeeded"
    assert store.get(job["id"])["error"] is None
    store.close()


def test_finish_refuses_a_queued_row(tmp_path: Path) -> None:
    """queued -> running is claim's transition; finish must not shortcut it."""
    store = _store(tmp_path)
    job = store.enqueue("never-ran", {})
    with pytest.raises(JobConflict) as refusal:
        store.finish(job["id"], "failed", error="no worker ever touched this")
    assert "queued" in str(refusal.value), str(refusal.value)
    assert store.get(job["id"])["status"] == "queued"
    store.close()


def test_finish_allows_the_legal_sources(tmp_path: Path) -> None:
    """running, cancelling and unknown are the three rows finish() may end."""
    store = _store(tmp_path)

    running = store.enqueue("from-running", {})
    store.claim_queued()
    assert store.finish(running["id"], "succeeded")["status"] == "succeeded"

    cancelling = store.enqueue("from-cancelling", {})
    store.claim_queued()
    store.begin_cancel(cancelling["id"])
    assert store.finish(cancelling["id"], "cancelled")["status"] == "cancelled"

    unknown = store.enqueue("from-unknown", {})
    store.claim_queued()
    store.finish(unknown["id"], "unknown", error="lost the worker")
    assert store.finish(unknown["id"], "failed", error="reconciled")[
        "status"
    ] == "failed"
    store.close()


def test_finish_on_a_missing_row_is_not_found(tmp_path: Path) -> None:
    """A silent no-op UPDATE used to make a typo look like a successful write."""
    store = _store(tmp_path)
    with pytest.raises(JobNotFound):
        store.finish("no-such-job", "failed", error="x")
    store.close()


def test_reenqueue_refuses_a_stale_attempt(tmp_path: Path) -> None:
    """The caller's view of the row is part of what it is asking for."""
    store = _store(tmp_path)
    job = store.enqueue("stale", {})
    store.claim_queued()
    store.finish(job["id"], "failed", error="boom")
    store.reenqueue(job["id"], expect_attempt=0)

    store.claim_queued()
    store.finish(job["id"], "failed", error="boom again")
    with pytest.raises(JobConflict) as refusal:
        store.reenqueue(job["id"], expect_attempt=0)
    assert "attempt" in str(refusal.value), str(refusal.value)
    assert store.get(job["id"])["attempt"] == 1
    store.close()
