"""Arm (a): same-process cancel and resume (NATIVE-10).

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

# --- arm (a): same-process cancel and resume ------------------------------


def test_same_process_cancel_then_resume_completes_every_item_exactly_once(tmp_path: Path, probe_dir: Path) -> None:
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(4, tmp_path), host=AMPLE_HOST)
    assert result.admitted == 4
    assert result.workers == 4

    # Cancel before any runner touches it: pending items go to cancelled.
    cancelled = queue_api.cancel(conn, result.batch_id)
    assert cancelled == 4
    counts = queue_store.counts_by_state(conn, result.batch_id)
    assert counts[queue_store.ITEM_CANCELLED] == 4
    assert counts[queue_store.ITEM_DONE] == 0
    # Nothing ran, so nothing was written: a cancel cannot leave a torn record.
    assert _marker_counts(probe_dir) == {}
    assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 0

    # A cancelled batch refuses to run until it is resumed. That is the
    # negative control for the resume below: if run_batch were happy to run a
    # cancelled batch, "resume works" would prove nothing.
    with pytest.raises(queue_api.QueueError, match="cancelled"):
        run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1, host=AMPLE_HOST)

    revived = queue_api.resume(conn, result.batch_id, host=AMPLE_HOST)
    assert revived == 4
    summary = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1, host=AMPLE_HOST)
    assert summary.completed == 4
    assert summary.failed == 0
    assert _marker_counts(probe_dir) == {f"sid_{i}": 1 for i in range(4)}

    prog = queue_api.progress(conn, result.batch_id)
    assert prog.counts[queue_store.ITEM_DONE] == 4
    assert prog.state == queue_store.BATCH_DONE
    conn.close()


def test_resuming_a_partly_done_batch_skips_what_already_completed(tmp_path: Path, probe_dir: Path) -> None:
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(3, tmp_path))
    run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)
    assert _marker_counts(probe_dir) == {f"sid_{i}": 1 for i in range(3)}

    # A second drain of the same batch has nothing pending, so it runs
    # nothing at all -- not "runs and overwrites identically".
    second = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)
    assert second.completed == 0
    assert _marker_counts(probe_dir) == {f"sid_{i}": 1 for i in range(3)}
    conn.close()


def test_a_fresh_enqueue_of_analyzed_tracks_skips_them_by_name(tmp_path: Path, probe_dir: Path) -> None:
    """Idempotent re-run: a no-op, and it SAYS it was a no-op."""
    db = tmp_path / "state.db"
    conn = open_conn(db)
    first = queue_api.enqueue(conn, _candidates(2, tmp_path))
    run_batch(conn, first.batch_id, backend_cls=BeatgridProbeV1)

    second = queue_api.enqueue(conn, _candidates(2, tmp_path))
    summary = run_batch(conn, second.batch_id, backend_cls=BeatgridProbeV1)
    assert summary.completed == 0
    assert summary.skipped == 2
    assert _marker_counts(probe_dir) == {"sid_0": 1, "sid_1": 1}
    items = queue_store.list_items(conn, second.batch_id)
    assert {i.state for i in items} == {queue_store.ITEM_SKIPPED}
    assert {i.reason for i in items} == {queue_store.SKIP_ALREADY_CURRENT}
    conn.close()
