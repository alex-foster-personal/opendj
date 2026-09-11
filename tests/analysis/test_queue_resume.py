"""Cancel and resume the backfill queue, in one process and across a kill.

Lane brief `specs/native-analysis-v1-lanes/nav1-queue.md` item 4, requirement
NATIVE-10 ("[if] a backfill is cancelled and restarted [then] it resumes and
re-runs idempotently, reporting progress throughout").

TWO ARMS, and the brief is explicit that only the second one proves the
contract:

* arm (a) cancels a live batch and resumes it in the SAME process. An
  entirely in-memory queue passes this, so it is necessary and not
  sufficient.
* arm (b) KILLS the worker process with SIGKILL mid-batch and starts a FRESH
  process against the persisted queue state. Items completed before the kill
  must be no-ops on the fresh process (not re-run, not double-counted) while
  pending and in-flight-at-kill items resume and complete EXACTLY ONCE.

Counting real executions, not rows: the record write is idempotent on
(stable_id, backend, backend_version), so a second run of a completed track
leaves the row identical and is invisible in the database. The probe backends
write one marker file per ACTUAL analyze() call, so a double-run shows up as
two markers for one track. Without that, "no double work" would be an
absence claim with no instrument behind it.

Single-line intent, one assertion block each:
- if a cancel leaves an in-flight item marked done with no record behind it,
  the queue has a torn record -- broken.
- if a resume re-runs a completed track, a resumed backfill costs more than
  the work it has left -- broken.
- if a fresh process cannot see what the killed one committed, the queue is
  not persistent and the NATIVE-10 line is unproven -- broken.
- if an item the killed process was holding is lost rather than re-run, a
  kill silently drops work -- broken.
- if a second enqueue of an already-analyzed track runs it again, the
  idempotent-re-run promise is decoration -- broken.
- if a worker that finishes AFTER a live cancel settles its item back to
  done, cancellation does not cancel and resume never revisits the item --
  broken.
- if a backend-wide availability failure is written as a per-track `failed`,
  installing the missing capability and resuming cannot recover the batch --
  broken.
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
    _commit_record,
    run_batch,
)
from apps.analysis.store import open_conn

from .queue_probe_backends import (
    PROBE_DB_ENV,
    PROBE_DIR_ENV,
    PROBE_SLEEP_ENV,
    BeatgridProbeV1,
    UnavailableBeatgridProbe,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _candidates(n: int, tmp_path: Path, *, minutes: float = 3.0) -> list:
    out = []
    for i in range(n):
        audio = tmp_path / f"t{i}.wav"
        audio.write_bytes(b"\0")
        out.append(
            admission.Candidate(
                stable_id=f"sid_{i}",
                lane="beatgrid",
                backend=BeatgridProbeV1.name,
                file_path=str(audio),
                duration_s=minutes * 60.0,
            )
        )
    return out


def _marker_counts(probe_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in probe_dir.glob("*.json"):
        sid = json.loads(path.read_text())["stable_id"]
        counts[sid] = counts.get(sid, 0) + 1
    return counts


@pytest.fixture()
def probe_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "markers"
    d.mkdir()
    monkeypatch.setenv(PROBE_DIR_ENV, str(d))
    return d


# --- arm (a): same-process cancel and resume ------------------------------

def test_same_process_cancel_then_resume_completes_every_item_exactly_once(
    tmp_path: Path, probe_dir: Path
) -> None:
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(4, tmp_path))
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
        run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)

    revived = queue_api.resume(conn, result.batch_id)
    assert revived == 4
    summary = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)
    assert summary.completed == 4
    assert summary.failed == 0
    assert _marker_counts(probe_dir) == {f"sid_{i}": 1 for i in range(4)}

    prog = queue_api.progress(conn, result.batch_id)
    assert prog.counts[queue_store.ITEM_DONE] == 4
    assert prog.state == queue_store.BATCH_DONE
    conn.close()


def test_resuming_a_partly_done_batch_skips_what_already_completed(
    tmp_path: Path, probe_dir: Path
) -> None:
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


def test_a_fresh_enqueue_of_analyzed_tracks_skips_them_by_name(
    tmp_path: Path, probe_dir: Path
) -> None:
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


# --- arm (b): a real process kill -----------------------------------------

def _spawn_runner(db: Path, batch_id: str, probe_dir: Path, sleep_s: float):
    env = {
        **os.environ,
        PROBE_DIR_ENV: str(probe_dir),
        PROBE_DB_ENV: str(db),
        PROBE_SLEEP_ENV: str(sleep_s),
        "PYTHONPATH": str(REPO_ROOT),
    }
    return subprocess.Popen(
        [
            sys.executable, "-m", "apps.analysis.queue_cli",
            "--db", str(db), "--json", "run",
            "--batch-id", batch_id,
            "--backend", "tests.analysis.queue_probe_backends:BeatgridProbeV1",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Its own process group, so the kill below can take the runner AND
        # the ProcessPoolExecutor children it spawned. Killing the runner
        # pid alone reparents those children to init and leaves them
        # resident: measured on this Mac, one run of this test left a
        # `multiprocessing.spawn` worker and its `resource_tracker` alive
        # after the test passed. A suite that leaks two processes per run is
        # a suite that eventually takes the host down, and the docstring
        # below already claimed a process-group kill it was not performing.
        start_new_session=True,
    )


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the runner's whole process group, then prove it is gone.

    The assertion is the point: ``killpg`` returning cleanly says the signal
    was delivered, not that the group is empty, and an orphaned pool worker
    is exactly the thing that survives a signal aimed at one pid.
    """
    # Read the group id BEFORE the kill. After ``wait`` reaps the runner,
    # ``getpgid(proc.pid)`` raises whether or not the group still holds a
    # leaked worker, so a probe built on it could never report one.
    pgid = os.getpgid(proc.pid)
    os.killpg(pgid, signal.SIGKILL)
    proc.wait(timeout=30)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(f"process group {pgid} still has members after SIGKILL")


def test_killed_runner_resumes_in_a_fresh_process_exactly_once(
    tmp_path: Path
) -> None:
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
    result = queue_api.enqueue(conn, _candidates(6, tmp_path, minutes=50.0))
    assert result.workers == 1, "a >45 min batch must run at one worker"
    conn.close()

    proc = _spawn_runner(db, result.batch_id, probe_dir, sleep_s=1.5)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if len(list(probe_dir.glob("*.json"))) >= 2:
            break
        if proc.poll() is not None:
            pytest.fail(
                f"runner exited early: {proc.returncode}\n{proc.stderr.read()}"
            )
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
    done_ids = {
        i.stable_id
        for i in queue_store.list_items(
            conn, result.batch_id, states=(queue_store.ITEM_DONE,)
        )
    }
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
            assert markers_after[sid] == count, (
                f"{sid} committed before the kill and ran again on resume"
            )
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


# --- machine faults are not track verdicts --------------------------------

def test_a_cancel_that_lands_mid_analysis_beats_the_worker_settling_it(
    tmp_path: Path, probe_dir: Path
) -> None:
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
    assert (
        queue_store.counts_by_state(conn, result.batch_id)[queue_store.ITEM_DONE] == 1
    )
    conn.close()


def test_a_backend_wide_outage_stops_the_batch_and_keeps_it_retryable(
    tmp_path: Path, probe_dir: Path
) -> None:
    """A missing capability is a host fact, not a verdict on four tracks."""
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, _candidates(4, tmp_path))
    assert result.admitted == 4

    with pytest.raises(BackendUnavailable, match="not available on this host"):
        run_batch(conn, result.batch_id, backend_cls=UnavailableBeatgridProbe)

    counts = queue_store.counts_by_state(conn, result.batch_id)
    assert counts[queue_store.ITEM_FAILED] == 0, (
        "a capability outage recorded as a terminal per-track failure is "
        "unrecoverable by resume"
    )
    assert counts[queue_store.ITEM_DONE] == 0
    # Everything is still queued: the item that met the outage was released
    # and the rest were never attempted.
    assert counts[queue_store.ITEM_PENDING] == 4
    assert (
        queue_store.get_batch(conn, result.batch_id).state
        == queue_store.BATCH_QUEUED
    )

    # Install the capability (here: swap in the working producer) and re-run.
    summary = run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)
    assert summary.completed == 4
    assert summary.failed == 0
    assert _marker_counts(probe_dir)["sid_0"] >= 1
    conn.close()
