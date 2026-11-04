"""Issue #6114: reject analysis completions after folder byte replacement."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from pathlib import Path

import pytest

from apps.analysis import admission, queue_store
from apps.analysis import queue as queue_api
from apps.analysis import run as analysis_run
from apps.analysis.lanes import LaneResult
from apps.analysis.queue_runner import RunSummary, _RunContext, _settle_one
from apps.analysis.source_bytes import StaleSourceBytesError
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


def _ctx(conn, batch_id: str) -> _RunContext:
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


def _future_record(stable_id: str) -> Future:
    record = _base_record(
        stable_id,
        BeatgridProbeV1.name,
        BeatgridProbeV1.version,
        "beatgrid",
        LaneResult(status="ok", confidence=0.9, payload=_beatgrid_payload()),
    )
    fut: Future = Future()
    fut.set_result((stable_id, record, None))
    return fut


def _seed_track(conn, stable_id: str, audio_hash: str, path: Path) -> None:
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
        "file_path, audio_hash, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (stable_id, stable_id, 180_000, str(path), audio_hash),
    )
    conn.commit()


@pytest.mark.requirement("NATIVE-10")
def test_stale_completion_after_audio_hash_change_is_rejected(tmp_path: Path) -> None:
    """[if] bytes change mid-job [then] the late write fails and stays out of analysis, [else stop]."""
    db_path = tmp_path / "state.db"
    conn = open_conn(db_path)
    audio = tmp_path / "track-a.wav"
    audio.write_bytes(b"\0")
    _seed_track(conn, "track-a", "hash-a", audio)

    batch = queue_api.enqueue(conn, [_candidate("track-a", tmp_path)])
    item = queue_store.claim_next(conn, batch.batch_id, runner_id="runner-a")
    assert item is not None
    assert item.claimed_audio_hash == "hash-a"

    conn.execute(
        "UPDATE tracks SET audio_hash = ? WHERE stable_id = ?",
        ("hash-b", "track-a"),
    )
    conn.commit()

    ctx = _ctx(conn, batch.batch_id)
    _settle_one(ctx, _future_record("track-a"), item)

    assert ctx.summary.failed == 1
    row = queue_store.get_item(conn, batch.batch_id, "track-a", "beatgrid")
    assert row is not None
    assert row.state == queue_store.ITEM_FAILED
    assert "StaleSourceBytesError" in (row.reason or "")
    stored = conn.execute(
        "SELECT 1 FROM analysis WHERE stable_id = ?",
        ("track-a",),
    ).fetchone()
    assert stored is None


@pytest.mark.requirement("NATIVE-10")
def test_cli_write_waits_for_a_rescan_and_refuses_its_stale_record(tmp_path: Path) -> None:
    """[if] a rescan commits between the CLI check and write [then] the write fails, [else stop]."""
    db_path = tmp_path / "state.db"
    conn = open_conn(db_path)
    audio = tmp_path / "track-a.wav"
    audio.write_bytes(b"\0")
    _seed_track(conn, "track-a", "hash-a", audio)
    record = _future_record("track-a").result()[1]

    # A folder rescan holds the write lock with the replacement not yet committed.
    rescan = open_conn(db_path)
    rescan.execute("BEGIN IMMEDIATE")
    rescan.execute("UPDATE tracks SET audio_hash = ? WHERE stable_id = ?", ("hash-b", "track-a"))
    outcome: list[object] = []

    def cli_write() -> None:
        writer = open_conn(db_path)
        try:
            outcome.append(
                analysis_run.upsert_unless_source_moved(writer, "track-a", record, "hash-a", db_path=db_path)
            )
        except BaseException as exc:  # noqa: BLE001 - the assertion below names it
            outcome.append(exc)
        finally:
            writer.close()

    thread = threading.Thread(target=cli_write)
    thread.start()
    thread.join(timeout=0.5)  # long enough for an unguarded check to pass on the old hash
    rescan.execute("COMMIT")
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], StaleSourceBytesError), outcome
    stored = conn.execute("SELECT 1 FROM analysis WHERE stable_id = ?", ("track-a",)).fetchone()
    assert stored is None, "if the stale record was written then ingest's drop was undone"
