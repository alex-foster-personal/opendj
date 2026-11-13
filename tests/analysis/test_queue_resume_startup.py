"""Pool startup width and lease cleanup (NATIVE-10).

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


def test_pool_width_is_sized_from_live_capacity_at_startup(
    tmp_path: Path,
    probe_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup sizes the pool from a live capacity read on the real path.

    [if] a batch runs with no injected host [then] startup reads live capacity after the real warm-up and persists that width, [else stop].
    """
    import logging

    conn = open_conn(tmp_path / "state.db")
    tight = HostCapacity(available_mb=700.0, cpu_count=8)
    result = queue_api.enqueue(conn, _candidates(4, tmp_path), host=tight)
    assert result.workers == 1

    live = size_workers_for_host(duration_cap=4)
    if live.workers == 1:
        pytest.skip(
            "UNAVAILABLE: live host capacity also caps at 1 worker, so startup cannot be distinguished from admission"
        )

    caplog.set_level(logging.INFO, logger="apps.analysis.queue_runner")
    summary = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)
    assert summary.workers == live.workers
    assert summary.workers > result.workers
    assert summary.completed == 4
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.workers == summary.workers
    assert batch.band == admission.BAND_UNDER_20_MIN
    assert batch.state == queue_store.BATCH_DONE
    assert "analysis pool startup" in caplog.text
    assert "widens to" in caplog.text
    # The reason quotes live free RAM, which moves between this read and the
    # runner's own on a busy host; the chosen width is the stable part.
    assert f"chose {live.workers} worker(s)" in caplog.text
    conn.close()


@pytest.mark.parametrize("parked_state", [queue_store.ITEM_DEFERRED, queue_store.ITEM_CANCELLED])
def test_startup_sizes_for_a_long_item_a_resume_could_hand_back(
    tmp_path: Path, probe_dir: Path, parked_state: str
) -> None:
    """A long parked item must hold the pool to its band, not the short item's.

    [if] a short pending item sits beside a long deferred or cancelled one [then] startup sizes for the long one, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    candidates = _candidates(1, tmp_path, minutes=50.0) + _candidates(2, tmp_path, minutes=3.0)[1:]
    result = queue_api.enqueue(conn, candidates, host=AMPLE_HOST)
    # Control: the short item alone would be planned wider than one worker.
    assert admission.duration_band_cap([3.0 * 60.0])[0] > 1
    conn.execute(
        "UPDATE analysis_queue_item SET state = ? WHERE batch_id = ? AND stable_id = ?",
        (parked_state, result.batch_id, "sid_0"),
    )
    summary = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1, host=AMPLE_HOST)
    assert summary.workers == 1
    assert summary.completed == 1
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert (batch.workers, batch.band) == (1, admission.BAND_OVER_45_MIN)
    parked = [
        item.state for item in queue_store.list_items(conn, result.batch_id, limit=None) if item.stable_id == "sid_0"
    ]
    assert parked == [parked_state]
    conn.close()


def test_startup_failure_does_not_uncancel_a_batch(tmp_path: Path) -> None:
    """A cancel that wins during startup must stay cancelled.

    [if] a cancel commits before startup failure returns [then] the batch stays cancelled, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    result = queue_api.enqueue(conn, _candidates(1, tmp_path), host=AMPLE_HOST)
    queue_store.take_batch_runner(conn, result.batch_id, runner_id="startup", runner_pid=os.getpid())
    queue_api.cancel(conn, result.batch_id)
    _abandon_failed_startup(conn, result.batch_id)
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.state == queue_store.BATCH_CANCELLED
    row = conn.execute(
        "SELECT active_runner_id, active_runner_pid FROM analysis_queue_batch WHERE batch_id = ?",
        (result.batch_id,),
    ).fetchone()
    assert row == (None, None)
    items = queue_store.list_items(conn, result.batch_id, limit=None)
    assert items
    assert all(item.state == queue_store.ITEM_CANCELLED for item in items)
    conn.close()


def test_startup_memory_refusal_clears_the_startup_lease(tmp_path: Path) -> None:
    """A real startup failure after the lease is taken must drop the lease.

    [if] startup sizing refuses for lack of memory after the lease is taken [then] the batch is queued again, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    result = queue_api.enqueue(conn, _candidates(1, tmp_path), host=AMPLE_HOST)
    starved = HostCapacity(available_mb=1.0, cpu_count=8)
    with pytest.raises(queue_api.QueueError, match="cannot fit even one"):
        run_batch(
            conn,
            result.batch_id,
            backend_cls=BeatgridProbeV1,
            host=starved,
        )
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert batch.state == queue_store.BATCH_QUEUED
    row = conn.execute(
        "SELECT active_runner_id, active_runner_pid FROM analysis_queue_batch WHERE batch_id = ?",
        (result.batch_id,),
    ).fetchone()
    assert row == (None, None)
    conn.close()


def test_enqueue_memory_refusal_is_a_queue_error(tmp_path: Path) -> None:
    conn = open_conn(tmp_path / "q.db")
    with pytest.raises(queue_api.QueueError, match="cannot fit even one"):
        queue_api.enqueue(
            conn,
            _candidates(2, tmp_path),
            host=HostCapacity(available_mb=100.0, cpu_count=8),
        )


def test_pool_startup_stays_at_zero_when_nothing_is_runnable(tmp_path: Path, probe_dir: Path) -> None:
    """True-empty contract at pool startup, through the real runner.

    [if] a real drain leaves no runnable item [then] the next run_batch starts no
    pool, persists workers=0 and band=empty, and settles the batch done, [else stop].
    """
    conn = open_conn(tmp_path / "state.db")
    result = queue_api.enqueue(conn, _candidates(2, tmp_path), host=AMPLE_HOST)
    assert result.workers > 0
    first = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1, host=AMPLE_HOST)
    assert first.completed == 2
    second = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1, host=AMPLE_HOST)
    assert second.workers == 0
    assert second.completed == 0
    assert _marker_counts(probe_dir) == {"sid_0": 1, "sid_1": 1}
    batch = queue_store.get_batch(conn, result.batch_id)
    assert batch is not None
    assert (batch.workers, batch.band) == (0, admission.BAND_EMPTY)
    assert batch.state == queue_store.BATCH_DONE
    conn.close()
