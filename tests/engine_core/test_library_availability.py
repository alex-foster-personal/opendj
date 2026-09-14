"""Engine-owned ``track_availability`` probing (issue #2588)."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.engine_core.app import create_app
from apps.engine_core.availability_api import PROBE_PATH, STATUS_PATH
from apps.engine_core.config import EngineConfig
from apps.engine_core.library_availability import LibraryAvailabilityWorker
from apps.mik import availability as avail
from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from tests.waits import THREAD_HANG_GUARD_S

pytestmark = pytest.mark.requirement("LIBM-09")


def _stable(prefix: str) -> str:
    return (prefix * 40)[:40]


def _upsert_track(
    conn: sqlite3.Connection,
    stable_id: str,
    file_path: str,
    *,
    title: str | None = None,
) -> None:
    with StateWriter(conn, actor="test-availability") as writer:
        writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier="inferred",
            title=title or stable_id,
            artists=[],
            album=None,
            isrc=None,
            duration_ms=None,
            file_path=file_path,
        )
    conn.commit()


def _independent_present_count(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT file_path FROM tracks WHERE deleted_at IS NULL"
    ).fetchall()
    return sum(1 for (path,) in rows if path and os.path.exists(path))


def _wait_status_complete(client: TestClient, *, guard_s: float) -> dict[str, object]:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        status = client.get(STATUS_PATH).json()
        if status["complete"] and status["pending"] == 0:
            return status
        time.sleep(0.05)
    raise AssertionError("availability status never completed")


def _wait_worker_complete(worker: LibraryAvailabilityWorker, *, guard_s: float) -> None:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.complete and snapshot.pending == 0:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"TIMEOUT: availability worker still pending={worker.status().pending} "
        f"phase={worker.status().phase} after {guard_s}s"
    )


def _wait_probe_pending(client: TestClient, *, guard_s: float) -> dict[str, object]:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        status = client.get(STATUS_PATH).json()
        if status["pending"] > 0 and status["phase"] in {"queued", "running"}:
            return status
        time.sleep(0.02)
    raise AssertionError("availability probe never entered a pending state")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    return target


@pytest.fixture
def state_db_path(data_dir: Path) -> Path:
    return data_dir / "state" / "state.db"


@pytest.fixture
def engine_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MDT_LIBRARY_MODE", "local")


def test_engine_lifespan_probe_matches_independent_exists_count(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
    engine_env: None,
) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    present_paths: list[str] = []
    for index in range(3):
        audio = audio_dir / f"track-{index}.mp3"
        audio.write_bytes(b"\x00")
        present_paths.append(str(audio))
    missing = str(audio_dir / "gone.mp3")

    conn = state_db.open_rw(state_db_path)
    try:
        for index, path in enumerate(present_paths):
            _upsert_track(conn, _stable(f"p{index}"), path)
        _upsert_track(conn, _stable("m"), missing)
    finally:
        conn.close()

    app = create_app(EngineConfig(data_dir=data_dir))
    with TestClient(app) as client:
        _wait_status_complete(client, guard_s=THREAD_HANG_GUARD_S)

    conn = state_db.open_rw(state_db_path)
    try:
        view_count = conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0]
        expected = _independent_present_count(conn)
        assert view_count == expected == len(present_paths)
        missing_state = conn.execute(
            "SELECT state FROM track_availability WHERE stable_id = ?",
            (_stable("m"),),
        ).fetchone()[0]
        assert missing_state == "absent"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tracks_available WHERE stable_id = ?",
                (_stable("m"),),
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_worker_restart_before_completion_preserves_settled_batches(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    audio_dir = tmp_path / "resume"
    audio_dir.mkdir()
    stable_ids: list[str] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(8):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            sid = _stable(f"r{index}")
            stable_ids.append(sid)
            _upsert_track(conn, sid, str(audio))
        first_batch = stable_ids[:3]
        avail.write(
            conn,
            avail.probe_batch(conn, first_batch),
            apply_mass_missing_guard=False,
        )
        conn.commit()
        stamps_before = dict(
            conn.execute(
                "SELECT stable_id, checked_at FROM track_availability ORDER BY stable_id"
            )
        )
        assert len(stamps_before) == 3
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=3)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
        assert worker.status().processed_total >= 5
    finally:
        worker.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        for sid, checked_at in stamps_before.items():
            current = conn.execute(
                "SELECT checked_at FROM track_availability WHERE stable_id = ?",
                (sid,),
            ).fetchone()[0]
            assert current == checked_at
        assert conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 8
    finally:
        conn.close()

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=3)
    worker2.start()
    try:
        _wait_worker_complete(worker2, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        stamps_after = dict(
            conn.execute(
                "SELECT stable_id, checked_at FROM track_availability ORDER BY stable_id"
            )
        )
        assert len(stamps_after) == 8
        for sid, checked_at in stamps_before.items():
            assert stamps_after[sid] == checked_at
    finally:
        conn.close()


def test_updated_at_predicate_requeues_without_notification(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"\x00")
    second.write_bytes(b"\x00")
    sid = _stable("u")

    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, sid, str(first))
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, sid, str(second), title="moved")
    finally:
        conn.close()

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker2.start()
    try:
        _wait_worker_complete(worker2, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        state, checked = conn.execute(
            "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
            (sid,),
        ).fetchone()
        assert state == "present"
        assert checked == str(second)
    finally:
        conn.close()


def test_library_changed_notification_queues_priority_probe(
    data_dir: Path, state_db_path: Path, tmp_path: Path, engine_env: None
) -> None:
    audio = tmp_path / "notify.mp3"
    audio.write_bytes(b"\x00")
    sid = _stable("n")

    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, sid, str(audio))
    finally:
        conn.close()

    app = create_app(EngineConfig(data_dir=data_dir))
    with TestClient(app) as client:
        _wait_status_complete(client, guard_s=THREAD_HANG_GUARD_S)
        moved = tmp_path / "moved.mp3"
        moved.write_bytes(b"\x00")
        conn = state_db.open_rw(state_db_path)
        try:
            _upsert_track(conn, sid, str(moved), title="relocated")
        finally:
            conn.close()
        publish("library.changed", {"kind": "tracks", "ids": [sid]})
        _wait_status_complete(client, guard_s=THREAD_HANG_GUARD_S)
        conn = state_db.open_rw(state_db_path)
        try:
            state, checked = conn.execute(
                "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
                (sid,),
            ).fetchone()
            assert state == "present"
            assert checked == str(moved)
        finally:
            conn.close()


def test_awaiting_volume_stays_pending_until_reclassified(
    data_dir: Path, state_db_path: Path, engine_env: None
) -> None:
    sid = _stable("v")
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, sid, "/Volumes/SLATER/Music/a.mp3")
    finally:
        conn.close()

    app = create_app(EngineConfig(data_dir=data_dir))
    with TestClient(app) as client:
        started = time.monotonic()
        while time.monotonic() - started < THREAD_HANG_GUARD_S:
            status = client.get(STATUS_PATH).json()
            if status["awaiting_volume"] == 1:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("awaiting_volume row was never classified")
        assert status["pending"] >= 1
        assert status["complete"] is False


def test_awaiting_volume_reclassifies_when_volume_is_mounted(
    data_dir: Path, state_db_path: Path
) -> None:
    volume_name = "MDT2588VOL"
    volume_root = Path("/Volumes") / volume_name / "Music"
    sid = _stable("mount")
    volume_path = str(volume_root / "a.mp3")

    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, sid, volume_path)
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker.start()
    try:
        started = time.monotonic()
        while time.monotonic() - started < THREAD_HANG_GUARD_S:
            snapshot = worker.status()
            if snapshot.awaiting_volume == 1 and snapshot.pending >= 1:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("awaiting_volume row was never classified")
    finally:
        worker.stop()

    try:
        volume_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        pytest.skip(f"/Volumes is not writable in this environment: {exc}")
    (volume_root / "a.mp3").write_bytes(b"\x00")

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker2.start()
    try:
        _wait_worker_complete(worker2, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        state, checked = conn.execute(
            "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
            (sid,),
        ).fetchone()
        assert state == "present"
        assert checked == volume_path
        assert conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 1
    finally:
        conn.close()
    shutil.rmtree(volume_root.parent, ignore_errors=True)


def test_background_round_refuses_catastrophic_present_drop(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    audio_dir = tmp_path / "guard"
    audio_dir.mkdir()
    paths: list[Path] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(10):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            paths.append(audio)
            _upsert_track(conn, _stable(f"g{index}"), str(audio))
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker.stop()

    for audio in paths[:6]:
        audio.unlink()

    conn = state_db.open_rw(state_db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE deleted_at IS NULL",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker2.start()
    started = time.monotonic()
    try:
        while time.monotonic() - started < THREAD_HANG_GUARD_S:
            snapshot = worker2.status()
            if snapshot.phase == "refused":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("background round never refused the mass-missing drop")
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 10
    finally:
        conn.close()


def test_listing_does_not_wait_for_probe(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
    engine_env: None,
) -> None:
    audio_dir = tmp_path / "many"
    audio_dir.mkdir()
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(150):
            audio = audio_dir / f"{index:03d}.mp3"
            audio.write_bytes(b"\x00")
            _upsert_track(conn, _stable(f"l{index:03d}"), str(audio))
    finally:
        conn.close()

    app = create_app(EngineConfig(data_dir=data_dir))
    with TestClient(app) as client:
        _wait_probe_pending(client, guard_s=THREAD_HANG_GUARD_S)
        list_started = time.monotonic()
        response = client.get("/api/v1/tracks", params={"limit": 5})
        elapsed = time.monotonic() - list_started
        assert response.status_code == 200
        assert elapsed < 2.0
        status = client.get(STATUS_PATH).json()
        assert status["pending"] > 0


def test_http_probe_and_status_routes(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
    engine_env: None,
) -> None:
    audio = tmp_path / "one.mp3"
    audio.write_bytes(b"\x00")
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, _stable("h"), str(audio))
    finally:
        conn.close()

    app = create_app(EngineConfig(data_dir=data_dir))
    with TestClient(app) as client:
        probe = client.post(PROBE_PATH, json={})
        assert probe.status_code == 202
        body = probe.json()
        assert body["accepted"] is True

        status = _wait_status_complete(client, guard_s=THREAD_HANG_GUARD_S)
        assert status["present"] == 1
        assert PROBE_PATH in json.dumps(app.openapi())
        assert STATUS_PATH in json.dumps(app.openapi())

        conn = state_db.open_rw(state_db_path)
        try:
            assert (
                conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0]
                == _independent_present_count(conn)
            )
        finally:
            conn.close()
