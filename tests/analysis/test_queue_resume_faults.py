"""Machine faults are not per-track verdicts (NATIVE-10).

[if] a backfill is cancelled and restarted [then] it resumes and reruns idempotently, [else stop].
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from apps.analysis import admission, queue_store
from apps.analysis import queue as queue_api
from apps.analysis.queue_runner import (
    BackendUnavailable,
    _abandon_failed_startup,
    _commit_record,
    run_batch,
)
from apps.analysis.store import open_conn
from apps.analysis.worker_sizing import HostCapacity, size_workers_for_host

from .queue_probe_backends import (
    PROBE_DB_ENV,
    PROBE_DIR_ENV,
    PROBE_SLEEP_ENV,
    BeatgridProbeV1,
    UnavailableBeatgridProbe,
)
from tests.analysis.queue_resume_support import (
    AMPLE_HOST,
    REPO_ROOT,
    _candidates,
    _kill_group,
    _marker_counts,
    _spawn_runner,
    probe_dir,
)

pytestmark = pytest.mark.requirement("NATIVE-10")

# --- machine faults are not track verdicts --------------------------------


def test_a_cancel_that_lands_mid_analysis_beats_the_worker_settling_it(tmp_path: Path, probe_dir: Path) -> None:
    """The race /cancel loses when settlement is unconditional.

    Driven at the commit seam rather than through a real pool, because the
    window is between "the worker returned a record" and "the runner writes
    it", and a pool test can only hit that window by luck. The claim is taken
    away exactly as `/cancel` takes it, then the settlement is attempted.
    """
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(1, tmp_path))
    assert result.admitted == 1

    item = queue_store.claim_next(conn, result.batch_id, runner_id="runner-a")
    assert item is not None
    assert item.state == queue_store.ITEM_RUNNING

    # The worker is still analyzing at this point; the operator cancels.
    assert queue_api.cancel(conn, result.batch_id) == 1

    record = BeatgridProbeV1.analyze(Path(item.file_path), item.stable_id)
    outcomes = _commit_record(
        conn,
        batch_id=result.batch_id,
        item=item,
        record=record,
        runner_id="runner-a",
    )
    assert outcomes is None, "the settlement must refuse a claim that was cancelled"

    counts = queue_store.counts_by_state(conn, result.batch_id)
    assert counts[queue_store.ITEM_CANCELLED] == 1
    assert counts[queue_store.ITEM_DONE] == 0
    # The whole transaction rolled back, so no record survives either.
    assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 0

    # Positive control: the same settlement under a LIVE claim does commit,
    # so the refusal above is about the cancel and not about the arguments.
    assert queue_api.resume(conn, result.batch_id) == 1
    live = queue_store.claim_next(conn, result.batch_id, runner_id="runner-b")
    assert live is not None
    assert (
        _commit_record(
            conn,
            batch_id=result.batch_id,
            item=live,
            record=record,
            runner_id="runner-b",
        )
        is not None
    )
    assert queue_store.counts_by_state(conn, result.batch_id)[queue_store.ITEM_DONE] == 1
    conn.close()


def test_a_backend_wide_outage_stops_the_batch_and_keeps_it_retryable(tmp_path: Path, probe_dir: Path) -> None:
    """A missing capability is a host fact, not a verdict on four tracks."""
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(4, tmp_path))
    assert result.admitted == 4

    with pytest.raises(BackendUnavailable, match="not available on this host"):
        run_batch(conn, result.batch_id, backend_cls=UnavailableBeatgridProbe)

    counts = queue_store.counts_by_state(conn, result.batch_id)
    assert counts[queue_store.ITEM_FAILED] == 0, (
        "a capability outage recorded as a terminal per-track failure is unrecoverable by resume"
    )
    assert counts[queue_store.ITEM_DONE] == 0
    # Everything is still queued: the item that met the outage was released
    # and the rest were never attempted.
    assert counts[queue_store.ITEM_PENDING] == 4
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.state == queue_store.BATCH_QUEUED

    # Install the capability (here: swap in the working producer) and re-run.
    summary = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)
    assert summary.completed == 4
    assert summary.failed == 0
    assert _marker_counts(probe_dir)["sid_0"] >= 1
    conn.close()


def test_startup_memory_refusal_clears_the_batch_lease(tmp_path: Path, probe_dir: Path) -> None:
    """A host that cannot fit one worker must leave the batch retryable.

    [if] pool startup refuses for memory [then] the batch lease is cleared, [else stop].
    """
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(2, tmp_path), host=AMPLE_HOST)
    with pytest.raises(queue_api.QueueError, match="cannot fit even one"):
        run_batch(
            conn,
            result.batch_id,
            backend_cls=BeatgridProbeV1,
            host=HostCapacity(available_mb=100.0, cpu_count=8),
        )
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.state == queue_store.BATCH_QUEUED
    row = conn.execute(
        "SELECT active_runner_id, active_runner_pid FROM analysis_queue_batch WHERE batch_id = ?",
        (result.batch_id,),
    ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
    summary = run_batch(
        conn,
        result.batch_id,
        backend_cls=BeatgridProbeV1,
        host=AMPLE_HOST,
    )
    assert summary.completed == 2
    conn.close()
