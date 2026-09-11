"""Review-thread regressions for PR #1586 / issue #1557.

Single-line intent, one assertion block each:
- if stable_id resolution binds more than SQLite's cap in one IN clause, a
  whole-library enqueue dies before planning -- broken.
- if a second live runner takes over a batch, two pools analyze the same
  tracks -- broken.
- if TrackVanished is terminal failed, restoring the file and resuming never
  retries it -- broken.
"""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest

from apps.analysis import admission, queue_store
from apps.analysis import queue as queue_api
from apps.analysis.backends.base import TrackVanished
from apps.analysis.queue_runner import RunSummary, _RunContext, _settle_one, run_batch
from apps.analysis.queue_targets import candidates_from_state
from apps.analysis.store import open_conn

from .queue_probe_backends import BeatgridProbeV1


def _candidate(stable_id: str, tmp_path: Path) -> admission.Candidate:
    audio = tmp_path / f"{stable_id}.wav"
    audio.write_bytes(b"\0")
    return admission.Candidate(
        stable_id=stable_id,
        lane="beatgrid",
        backend=BeatgridProbeV1.name,
        file_path=str(audio),
        duration_s=180.0,
    )


def test_candidates_from_state_chunks_past_the_sqlite_bind_limit(
    tmp_path: Path,
) -> None:
    conn = open_conn(tmp_path / "state.db")
    ids = [f"sid_{i}" for i in range(1005)]
    for sid in ids:
        audio = tmp_path / f"{sid}.wav"
        audio.write_bytes(b"\0")
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
            "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, "
            "'2026-09-11T00:00:00Z', '2026-09-11T00:00:00Z')",
            (sid, sid, 180_000, str(audio)),
        )
    conn.commit()
    out = candidates_from_state(
        conn, ids, lane="beatgrid", backend=BeatgridProbeV1.name
    )
    assert len(out) == len(ids)
    conn.close()


def test_a_live_runner_refuses_a_second_concurrent_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, [_candidate("a", tmp_path)])
    conn.execute(
        "UPDATE analysis_queue_batch SET state = ?, active_runner_id = ?, "
        "active_runner_pid = ? WHERE batch_id = ?",
        (
            queue_store.BATCH_RUNNING,
            "runner-a",
            999_999,
            result.batch_id,
        ),
    )
    conn.commit()
    monkeypatch.setattr(queue_store, "pid_is_alive", lambda pid: int(pid) == 999_999)
    with pytest.raises(queue_api.QueueError, match="already being drained"):
        run_batch(conn, result.batch_id, backend_cls=BeatgridProbeV1)
    conn.close()


def test_track_vanished_is_deferred_and_retries_on_resume(
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.db"
    conn = open_conn(db)
    result = queue_api.enqueue(conn, [_candidate("a", tmp_path)])
    item = queue_store.claim_next(conn, result.batch_id, runner_id="runner-a")
    assert item is not None

    class DoneFuture(Future):
        def result(self, timeout=None):
            return (
                item.stable_id,
                None,
                f"{TrackVanished.__name__}: file gone",
            )

    ctx = _RunContext(
        conn=conn,
        batch_id=result.batch_id,
        backend_cls=BeatgridProbeV1,
        backend_name=BeatgridProbeV1.name,
        version=BeatgridProbeV1.version,
        runner_id="runner-a",
        workers=1,
        summary=RunSummary(
            batch_id=result.batch_id, runner_id="runner-a", workers=1
        ),
        on_item=None,
    )
    _settle_one(ctx, DoneFuture(), item)
    assert ctx.summary.deferred == 1
    assert ctx.summary.failed == 0
    counts = queue_store.counts_by_state(conn, result.batch_id)
    assert counts[queue_store.ITEM_DEFERRED] == 1

    revived = queue_api.resume(conn, result.batch_id)
    assert revived == 1
    conn.close()
