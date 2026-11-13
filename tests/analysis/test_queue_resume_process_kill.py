"""Arm (b): SIGKILL kill and fresh-process resume (NATIVE-10).

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


def test_killed_runner_resumes_in_a_fresh_process_exactly_once(tmp_path: Path) -> None:
    """SIGKILL the runner mid-batch; a fresh process finishes the job.

    The kill is ``SIGKILL`` on the runner's whole process group, not a
    graceful stop call: a stop call gives the runner a chance to tidy up,
    which is precisely the code path a crash does not run, so a test that
    used one would be arm (a) in a costume.
    """
    db = tmp_path / "state.db"
    probe_dir = tmp_path / "markers"
    probe_dir.mkdir()
    conn = open_conn(db)
    # One worker and a slow backend so the kill lands with work still
    # pending AND one item in flight, which is the state arm (b) is about.
    result = queue_api.enqueue(conn, _candidates(6, tmp_path, minutes=50.0), host=AMPLE_HOST)
    assert result.workers == 1, "a >45 min batch must run at one worker"
    conn.close()

    proc = _spawn_runner(db, result.batch_id, probe_dir, sleep_s=1.5)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if len(list(probe_dir.glob("*.json"))) >= 2:
            break
        if proc.poll() is not None:
            pytest.fail(f"runner exited early: {proc.returncode}\n{proc.stderr.read()}")
        time.sleep(0.1)
    else:  # pragma: no cover - only on a pathologically slow machine
        proc.kill()
        pytest.fail("runner never started analyzing")

    # Let the second track's record commit, then kill hard while a third is
    # in flight.
    time.sleep(1.2)
    _kill_group(proc)
    assert proc.returncode in (-signal.SIGKILL, 137), proc.returncode

    conn = open_conn(db)
    before = queue_store.counts_by_state(conn, result.batch_id)
    done_before = before[queue_store.ITEM_DONE]
    assert done_before >= 1, "the kill landed before anything committed"
    assert done_before < 6, "the kill landed after the batch finished"
    committed_ids = {
        row[0]
        for row in conn.execute(
            "SELECT stable_id FROM analysis WHERE backend = ?",
            (BeatgridProbeV1.name,),
        )
    }
    # Every done item has a record behind it, and no record exists under an
    # item that is not done: no torn record in either direction.
    done_ids = {i.stable_id for i in queue_store.list_items(conn, result.batch_id, states=(queue_store.ITEM_DONE,))}
    assert done_ids == committed_ids

    markers_before = _marker_counts(probe_dir)
    # A fresh process takes over the persisted batch.
    os.environ[PROBE_DIR_ENV] = str(probe_dir)
    revived = queue_api.resume(conn, result.batch_id)
    assert revived >= 1, "the item the kill was holding must come back"
    summary = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)

    final = queue_store.counts_by_state(conn, result.batch_id)
    assert final[queue_store.ITEM_DONE] == 6
    assert final[queue_store.ITEM_PENDING] == 0
    assert final[queue_store.ITEM_RUNNING] == 0

    markers_after = _marker_counts(probe_dir)
    # Completed-before-the-kill items were NOT re-run: their marker count is
    # unchanged. Everything else ran exactly once in total.
    for sid, count in markers_before.items():
        if sid in done_ids:
            assert markers_after[sid] == count, f"{sid} committed before the kill and ran again on resume"
    for i in range(6):
        sid = f"sid_{i}"
        if sid in done_ids:
            continue
        assert markers_after.get(sid, 0) >= 1, f"{sid} was never run"
    # Exactly once, counted the other way: 6 tracks, and the only track that
    # may carry two markers is the one that was in flight when the kill
    # landed (its first attempt never committed).
    twice = [sid for sid, n in markers_after.items() if n > 1]
    assert len(twice) <= 1, f"more than one track ran twice: {twice}"
    for sid in twice:
        assert sid not in done_ids, f"{sid} was already done and ran again"
    assert summary.completed == 6 - done_before
    conn.close()


@pytest.mark.requirement("NATIVE-8905")
def test_resume_refuses_while_a_live_runner_holds_the_batch(tmp_path: Path) -> None:
    """Resume must not return in-flight items to pending while a pool drains.

    [if] resume runs while a live runner holds the lease [then] it refuses
    naming that pid, [else stop].
    """
    db = tmp_path / "state.db"
    probe_dir = tmp_path / "markers"
    probe_dir.mkdir()
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(4, tmp_path, minutes=50.0), host=AMPLE_HOST)
    assert result.workers == 1
    batch_id = result.batch_id
    conn.close()

    proc = _spawn_runner(db, batch_id, probe_dir, sleep_s=1.5)
    try:
        live_pid: int | None = None
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"runner exited early: {proc.returncode}\n{proc.stderr.read()}")
            poll_conn = open_conn(db)
            batch = queue_store.get_batch(poll_conn, batch_id)
            assert batch is not None
            live_pid = queue_store.live_runner_pid(poll_conn, batch_id)
            markers = list(probe_dir.glob("*.json"))
            if batch.state == queue_store.BATCH_RUNNING and live_pid is not None and markers:
                counts = queue_store.counts_by_state(poll_conn, batch_id)
                if counts.get(queue_store.ITEM_RUNNING, 0) >= 1:
                    running_stable_ids = {
                        i.stable_id
                        for i in queue_store.list_items(
                            poll_conn,
                            batch_id,
                            states=(queue_store.ITEM_RUNNING,),
                        )
                    }
                    poll_conn.close()
                    break
            poll_conn.close()
            time.sleep(0.1)
        else:
            pytest.fail("live runner never took the batch lease with work in flight")

        assert live_pid is not None
        resume_conn = open_conn(db)
        with pytest.raises(queue_api.QueueError) as exc_info:
            queue_api.resume(resume_conn, batch_id)
        msg = str(exc_info.value)
        assert str(live_pid) in msg
        assert "drained" in msg or "runner" in msg

        batch_after = queue_store.get_batch(resume_conn, batch_id)
        assert batch_after is not None
        assert batch_after.state == queue_store.BATCH_RUNNING
        counts = queue_store.counts_by_state(resume_conn, batch_id)
        assert counts.get(queue_store.ITEM_RUNNING, 0) >= 1
        for item in queue_store.list_items(resume_conn, batch_id):
            if item.stable_id in running_stable_ids:
                assert item.state == queue_store.ITEM_RUNNING
        markers = _marker_counts(probe_dir)
        assert all(n <= 1 for n in markers.values())

        with pytest.raises(queue_api.QueueError, match="already being drained"):
            run_batch(resume_conn, batch_id, backend_cls=BeatgridProbeV1)
        resume_conn.close()
    finally:
        _kill_group(proc)

    conn = open_conn(db)
    revived = queue_api.resume(conn, batch_id)
    assert revived >= 1
    conn.close()
