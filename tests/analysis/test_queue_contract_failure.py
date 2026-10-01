"""One track's bad record fails THAT track, never the batch behind it.

Found live on demon-llama, Thu 1 Oct 2026: a beatgrid record whose last fitted
beat sat 0.011 s past its frame-derived duration raised from the record
contract inside the PARENT's commit, escaped ``run_batch``, exited the queue
CLI 5, and left the other claimed items ``running`` forever. The ahead-of-time
drain then read four identical exit-5 reasons as a host-wide fault and closed
the lane for the session, so 900 tracks never got a BPM or key.

-Claude
"""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import admission, queue_store
from apps.analysis import queue_runner
from apps.analysis import queue as queue_api
from apps.analysis.lanes import LaneResult
from apps.analysis.queue_runner import RunSummary, _RunContext, _settle_one, run_batch
from apps.analysis.store import open_conn

from .queue_probe_backends import BeatgridProbeV1, _base_record, _beatgrid_payload


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


def _ctx(conn: Any, batch_id: str) -> _RunContext:
    return _RunContext(
        conn=conn,
        batch_id=batch_id,
        backend_cls=BeatgridProbeV1,
        backend_name=BeatgridProbeV1.name,
        version=BeatgridProbeV1.version,
        runner_id="runner-a",
        workers=1,
        summary=RunSummary(batch_id=batch_id, runner_id="runner-a", workers=1),
        on_item=None,
    )


def _future_with(stable_id: str, *, last_beat_t: float) -> Future:
    payload = _beatgrid_payload()
    payload["beats"][-1]["t"] = last_beat_t  # type: ignore[index]
    record = _base_record(
        stable_id, BeatgridProbeV1.name, BeatgridProbeV1.version, "beatgrid",
        LaneResult(status="ok", confidence=0.9, payload=payload),
    )
    fut: Future = Future()
    fut.set_result((stable_id, record, None))
    return fut


def test_a_record_past_its_duration_fails_that_track_and_keeps_the_batch(tmp_path: Path) -> None:
    conn = open_conn(tmp_path / "state.db")
    batch = queue_api.enqueue(conn, [_candidate("a", tmp_path), _candidate("b", tmp_path)])
    item = queue_store.claim_next(conn, batch.batch_id, runner_id="runner-a")
    assert item is not None
    ctx = _ctx(conn, batch.batch_id)

    # duration_s on the probe record is 180.0; this beat is past it.
    _settle_one(ctx, _future_with(item.stable_id, last_beat_t=180.011), item)

    assert ctx.summary.failed == 1, "if a contract breach does not count as one failed track then broken"
    rows = {i.stable_id: i for i in queue_store.list_items(conn, batch.batch_id, limit=10)}
    assert rows[item.stable_id].state == queue_store.ITEM_FAILED
    assert "beyond the record's duration_s" in (rows[item.stable_id].reason or ""), (
        "if the failed item does not name the contract it broke then broken"
    )
    other = "b" if item.stable_id == "a" else "a"
    assert rows[other].state == queue_store.ITEM_PENDING, "if the sibling is touched then broken"
    conn.close()


def test_a_valid_record_still_commits_done(tmp_path: Path) -> None:
    """Control in the opposite direction: catching contract errors must not
    swallow a good record."""
    conn = open_conn(tmp_path / "state.db")
    batch = queue_api.enqueue(conn, [_candidate("a", tmp_path)])
    item = queue_store.claim_next(conn, batch.batch_id, runner_id="runner-a")
    assert item is not None
    ctx = _ctx(conn, batch.batch_id)

    _settle_one(ctx, _future_with(item.stable_id, last_beat_t=179.9), item)

    assert ctx.summary.completed == 1 and ctx.summary.failed == 0
    assert queue_store.counts_by_state(conn, batch.batch_id)[queue_store.ITEM_DONE] == 1
    conn.close()


def test_an_unexpected_crash_returns_claimed_items_to_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_conn(tmp_path / "state.db")
    batch = queue_api.enqueue(conn, [_candidate("a", tmp_path), _candidate("b", tmp_path)])

    def boom(ctx: _RunContext, pool: Any, seen: Any) -> None:
        queue_store.claim_next(ctx.conn, ctx.batch_id, runner_id=ctx.runner_id)
        raise RuntimeError("runner bug")

    monkeypatch.setattr(queue_runner, "_pump_pool", boom)
    with pytest.raises(RuntimeError, match="runner bug"):
        run_batch(conn, batch.batch_id, backend_cls=BeatgridProbeV1)

    counts = queue_store.counts_by_state(conn, batch.batch_id)
    assert counts[queue_store.ITEM_RUNNING] == 0, "if a crashed run leaves items running then broken"
    assert counts[queue_store.ITEM_PENDING] == 2
    conn.close()
