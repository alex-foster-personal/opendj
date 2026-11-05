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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import admission, queue_runner, queue_store
from apps.analysis import queue as queue_api
from apps.analysis.lanes import LaneContractError, LaneResult
from apps.analysis.queue_runner import RunSummary, _RunContext, _settle_one, run_batch
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import StoredRecordError, open_conn, upsert_record
from apps.shared.state import analysis_stale

from .queue_probe_backends import PROBE_DIR_ENV, BeatgridProbeV1, _base_record, _beatgrid_payload


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


def test_a_malformed_stored_record_fails_that_track_and_keeps_the_batch(tmp_path: Path) -> None:
    """[if] one track's stored record is malformed [then] only that item fails, [else stop]."""
    conn = open_conn(tmp_path / "state.db")
    batch = queue_api.enqueue(conn, [_candidate("a", tmp_path), _candidate("b", tmp_path)])
    item = queue_store.claim_next(conn, batch.batch_id, runner_id="runner-a")
    assert item is not None
    ctx = _ctx(conn, batch.batch_id)
    # A row for this track already exists and its lanes value has the wrong shape.
    good = _future_with(item.stable_id, last_beat_t=179.9).result()[1]
    conn.execute(
        "INSERT INTO analysis (stable_id, backend, backend_version, analyzed_at, duration_s, "
        "sample_rate, bpm, bpm_confidence, key_camelot, key_openkey, key_confidence, energy, "
        "energy_source, record_json) VALUES (?, ?, ?, ?, 180.0, 44100, 120.0, 0.9, '8A', '1m', "
        "0.9, 5, 'derived', ?)",
        (item.stable_id, good.backend, good.backend_version, "2026-10-08T00:00:00Z", '{"lanes": []}'),
    )

    _settle_one(ctx, _future_with(item.stable_id, last_beat_t=179.9), item)

    assert ctx.summary.failed == 1 and ctx.summary.completed == 0
    rows = {i.stable_id: i for i in queue_store.list_items(conn, batch.batch_id, limit=10)}
    assert rows[item.stable_id].state == queue_store.ITEM_FAILED
    assert "StoredRecordError" in (rows[item.stable_id].reason or "")
    other = "b" if item.stable_id == "a" else "a"
    assert rows[other].state == queue_store.ITEM_PENDING, "the sibling claim must be untouched"
    stored = conn.execute(
        "SELECT record_json FROM analysis WHERE stable_id = ?", (item.stable_id,)
    ).fetchone()[0]
    assert stored == '{"lanes": []}', "the malformed row must not be overwritten"
    conn.close()


def test_explicit_null_lanes_fails_that_track_and_leaves_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a stored record has explicit null lanes [then] StoredRecordError and no overwrite, [else stop].

    Driven through ``run_batch``: the production pool runs the queue suite's
    own beatgrid backend in a worker and the production settle commits its
    record against the malformed row already in state.db, so no completed
    future or hand-built result stands in for the runner.
    """
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    monkeypatch.setenv(PROBE_DIR_ENV, str(probe_dir))  # the backend's per-track execution marker
    conn = open_conn(tmp_path / "state.db")
    batch = queue_api.enqueue(conn, [_candidate("a", tmp_path), _candidate("b", tmp_path)])
    malformed = '{"lanes": null}'
    conn.execute(
        "INSERT INTO analysis (stable_id, backend, backend_version, analyzed_at, duration_s, "
        "sample_rate, bpm, bpm_confidence, key_camelot, key_openkey, key_confidence, energy, "
        "energy_source, record_json) VALUES (?, ?, ?, ?, 180.0, 44100, 120.0, 0.9, '8A', '1m', "
        "0.9, 5, 'derived', ?)",
        ("a", BeatgridProbeV1.name, BeatgridProbeV1.version, "2026-10-08T00:00:00Z", malformed),
    )
    # The dependency cascade's own invalidation, so the runner re-analyzes the
    # track instead of skipping it as already current.
    analysis_stale.ensure_stale_table(conn)
    analysis_stale.mark_stale(
        conn,
        stable_id="a",
        lane="beatgrid",
        backend=BeatgridProbeV1.name,
        backend_version=BeatgridProbeV1.version,
        reason="cascade: upstream lane changed",
    )
    conn.commit()

    summary = run_batch(conn, batch.batch_id, backend_cls=BeatgridProbeV1)

    assert summary.failed == 1, "if the null-lanes track does not fail on its own then broken"
    assert summary.completed == 1, "if the sibling track does not still complete then broken"
    rows = {i.stable_id: i for i in queue_store.list_items(conn, batch.batch_id, limit=10)}
    assert rows["a"].state == queue_store.ITEM_FAILED
    assert "explicit null lanes" in (rows["a"].reason or "")
    assert rows["b"].state == queue_store.ITEM_DONE
    stored = conn.execute("SELECT record_json FROM analysis WHERE stable_id = ?", ("a",)).fetchone()[0]
    assert stored == malformed, "if the malformed row is overwritten then broken"
    conn.close()


def _non_own_beatgrid_record(stable_id: str, payload: dict[str, object]) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
        duration_s=180.0,
        sample_rate=44100,
        bpm=128.0,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="1m",
        key_confidence=0.8,
        energy=5,
        lanes={
            "beatgrid": LaneResult(status="ok", confidence=0.9, payload=payload),
        },
    )


@pytest.mark.requirement("NATIVE-34")
def test_upsert_ok_beatgrid_without_beats_raises_and_inserts_nothing(tmp_path: Path) -> None:
    """[if] a new ok beatgrid has no beats list [then] upsert raises, [else stop]."""
    db = tmp_path / "state.db"
    record = _non_own_beatgrid_record("t1", {"bpm": 128.0, "static_grid_untrusted": False})
    with pytest.raises(StoredRecordError, match="no beats"):
        upsert_record(record, db_path=db, cascade=False, publish_live_event=False)
    conn = open_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.requirement("NATIVE-34")
def test_upsert_ok_beatgrid_without_beats_leaves_seeded_row(tmp_path: Path) -> None:
    """[if] an update ok beatgrid has no beats [then] the stored row is untouched, [else stop]."""
    db = tmp_path / "state.db"
    good_payload = {
        "beats": [{"n": 1, "bpm": 128.0, "t": 0.0}],
        "bpm": 128.0,
        "static_grid_untrusted": False,
    }
    good = _non_own_beatgrid_record("t1", good_payload)
    upsert_record(good, db_path=db, cascade=False, publish_live_event=False)
    conn = open_conn(db)
    try:
        seeded = conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id = ?", ("t1",)
        ).fetchone()[0]
    finally:
        conn.close()
    bad = _non_own_beatgrid_record("t1", {"bpm": 129.0, "static_grid_untrusted": False})
    with pytest.raises(StoredRecordError, match="no beats"):
        upsert_record(bad, db_path=db, cascade=False, publish_live_event=False)
    conn = open_conn(db)
    try:
        assert (
            conn.execute(
                "SELECT record_json FROM analysis WHERE stable_id = ?", ("t1",)
            ).fetchone()[0]
            == seeded
        )
    finally:
        conn.close()


@pytest.mark.requirement("NATIVE-34")
def test_upsert_ok_beatgrid_non_bool_trust_flag_raises(tmp_path: Path) -> None:
    """[if] bar_phase_untrusted is not a bool [then] upsert raises, [else stop]."""
    db = tmp_path / "state.db"
    record = _non_own_beatgrid_record(
        "t1",
        {
            "beats": [],
            "bpm": 128.0,
            "bar_phase_untrusted": "false",
        },
    )
    with pytest.raises(LaneContractError, match="must be a bool"):
        upsert_record(record, db_path=db, cascade=False, publish_live_event=False)
    conn = open_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 0
    finally:
        conn.close()
