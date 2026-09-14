"""Engine-owned ``track_availability`` probing (issue #2588)."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from apps.engine_core.app import create_app
from apps.engine_core.availability_api import PROBE_PATH, STATUS_PATH
from apps.engine_core.config import EngineConfig
from apps.engine_core.library_availability import LibraryAvailabilityWorker
from apps.mik import availability as avail
from apps.shared.events import publish
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from tests.waits import THREAD_HANG_GUARD_S

# A listing that waited on the probe would block for the probe's whole run; this
# bound only catches that hang, while `pending > 0` proves the listing did not wait.
LISTING_HANG_BOUND_S: float = 10.0

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


def _get_with_deadline(
    client: TestClient, url: str, *, timeout: float, **kwargs: Any
) -> Response:
    """``client.get`` with a real enforced wall-clock deadline.

    httpx's own ``timeout=`` kwarg is a documented no-op against TestClient's
    in-process ASGI transport (empirically confirmed in
    tests/webui/test_audio_deck_load_contract.py): a handler that hangs just
    hangs the call. Running it on a daemon thread and bounding the wait with a
    real ``queue.Queue.get(timeout=...)`` is what actually enforces the
    deadline, which is the whole point of a hang-detection test like this one.
    """
    result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            result.put(("ok", client.get(url, **kwargs)))
        except Exception as exc:  # noqa: BLE001 - re-raised on the main thread below
            result.put(("error", exc))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        outcome, payload = result.get(timeout=timeout)
    except queue.Empty:
        pytest.fail(
            f"GET {url} did not respond within {timeout}s -- the listing "
            "route waited on the availability probe"
        )
    if outcome == "error":
        raise payload
    return payload


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


def test_worker_restart_mid_run_preserves_settled_rows_byte_identical(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    audio_dir = tmp_path / "mid-run"
    audio_dir.mkdir()
    track_count = 20
    batch_size = 4
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(track_count):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            _upsert_track(conn, _stable(f"m{index:02d}"), str(audio))
    finally:
        conn.close()

    interrupted = threading.Event()
    stop_requested = False
    worker_holder: list[LibraryAvailabilityWorker] = []

    def on_batch_committed(snapshot) -> None:
        nonlocal stop_requested
        if stop_requested or snapshot.processed_total < batch_size:
            return
        stop_requested = True
        worker_holder[0].stop_after_current_batch()
        interrupted.set()

    worker = LibraryAvailabilityWorker(
        data_dir,
        batch_size=batch_size,
        on_batch_committed=on_batch_committed,
    )
    worker_holder.append(worker)
    worker.start()
    try:
        assert interrupted.wait(timeout=THREAD_HANG_GUARD_S)
    finally:
        worker.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        settled = dict(
            conn.execute(
                "SELECT stable_id, checked_at FROM track_availability ORDER BY stable_id"
            )
        )
        settled_count = len(settled)
        assert 0 < settled_count < track_count
    finally:
        conn.close()

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=batch_size)
    worker2.start()
    try:
        _wait_worker_complete(worker2, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM track_availability").fetchone()[0]
            == track_count
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0]
            == track_count
        )
        for sid, checked_at in settled.items():
            current = conn.execute(
                "SELECT checked_at FROM track_availability WHERE stable_id = ?",
                (sid,),
            ).fetchone()[0]
            assert current == checked_at
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
    data_dir: Path, state_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # P0 (Sol review, PR #2619): this used to mkdir + shutil.rmtree a REAL
    # /Volumes/<name> path. On any machine that happens to have a real
    # volume mounted under that literal name, that deleted user data outside
    # the test sandbox. `apps.mik.availability.VOLUMES_ROOT` is a
    # module-level constant read by both `_mounted_volumes()` (`os.listdir`)
    # and `classify_path()` (`path.startswith(...)`) at call time, so
    # monkeypatching it confines every filesystem touch this test makes to a
    # fake "/Volumes" tree under `tmp_path`, auto-cleaned by pytest -- no
    # writes to the real `/Volumes` and no rmtree at all.
    fake_volumes_root = tmp_path / "Volumes"
    monkeypatch.setattr(avail, "VOLUMES_ROOT", str(fake_volumes_root))

    volume_name = "MDT2588VOL"
    volume_root = fake_volumes_root / volume_name / "Music"
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

    # Always writable: `fake_volumes_root` lives entirely under `tmp_path`,
    # so no OSError/skip branch is needed here any more either.
    volume_root.mkdir(parents=True, exist_ok=True)
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
    # No cleanup call here on purpose: `volume_root` lives entirely under
    # `tmp_path`, which pytest already removes -- see the P0 note above for
    # why this test must never itself rmtree a volume-shaped path.


def test_status_reports_present_count_when_boot_finds_nothing_pending(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    # P1 (Sol review, PR #2619): `status()` only refreshed `present` inside
    # `_refresh_status_counts`, which a round that finds nothing pending
    # (every row already settled -- e.g. a later engine boot) never calls, so
    # `present` stayed stuck at the `AvailabilityWorkerStatus` dataclass
    # default of 0 even though the database already held present rows. Seed
    # rows as already settled (not via the worker) so the worker's own first
    # round has nothing to do and never commits a batch, which is exactly
    # the path that skipped the refresh.
    audio_dir = tmp_path / "already-settled"
    audio_dir.mkdir()
    stable_ids: list[str] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(3):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            sid = _stable(f"s{index}")
            stable_ids.append(sid)
            _upsert_track(conn, sid, str(audio))
        avail.write(
            conn,
            avail.probe_batch(conn, stable_ids),
            apply_mass_missing_guard=False,
        )
        conn.commit()
        still_pending = conn.execute(
            "SELECT COUNT(*) FROM tracks t "
            "LEFT JOIN track_availability a ON a.stable_id = t.stable_id "
            "WHERE t.deleted_at IS NULL "
            "AND (a.stable_id IS NULL OR t.updated_at > a.checked_at)"
        ).fetchone()[0]
        assert still_pending == 0, "seed must already be fully settled"
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
        snapshot = worker.status()
        # Confirms this run really did take the boot-finds-nothing-pending
        # path (the one `_refresh_status_counts` never runs on), not some
        # other path that would make this assertion pass for the wrong
        # reason.
        assert snapshot.processed_total == 0
        assert snapshot.present == 3
    finally:
        worker.stop()


def test_status_present_count_does_not_double_count_after_batch_commit(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    # P1 opposite direction (Sol review, PR #2619): `present` must be a real
    # database COUNT recomputed on every `status()` call, not an accumulator
    # that grows every time a batch commits or every time `status()` is
    # polled. Read it several times after the same batch commit and confirm
    # it stays exactly the seeded count.
    audio_dir = tmp_path / "present-count"
    audio_dir.mkdir()
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(5):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            _upsert_track(conn, _stable(f"pc{index}"), str(audio))
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
        readings = [worker.status().present for _ in range(4)]
        assert readings == [5, 5, 5, 5]
    finally:
        worker.stop()


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


def _seed_mass_missing_refusal_fixture(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
) -> tuple[LibraryAvailabilityWorker, list[Path]]:
    audio_dir = tmp_path / "terminal-refused"
    audio_dir.mkdir()
    paths: list[Path] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(10):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            paths.append(audio)
            _upsert_track(conn, _stable(f"t{index}"), str(audio))
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
    while time.monotonic() - started < THREAD_HANG_GUARD_S:
        snapshot = worker2.status()
        if snapshot.phase == "refused":
            return worker2, paths
        time.sleep(0.05)
    worker2.stop()
    raise AssertionError("background round never refused the mass-missing drop")


def test_refused_phase_stays_terminal_without_auto_retry(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    worker2, _paths = _seed_mass_missing_refusal_fixture(
        data_dir, state_db_path, tmp_path
    )
    try:
        snapshot = worker2.status()
        assert snapshot.phase == "refused"
        assert snapshot.pending > 0
        refused_processed = snapshot.processed_total

        # Count real retry rounds directly rather than polling `status()` for a
        # transient "running" phase: a retry round flips phase to "running" and
        # back to "refused" fast enough (small in-memory batch, rolled-back
        # transaction) that a wall-clock poll can miss every occurrence even
        # when the worker is retrying every idle cycle. Wrapping the real
        # `_drain_once` still calls the real implementation; it only adds a
        # counter, so this is a spy, not a mock.
        drain_calls = {"n": 0}
        original_drain_once = worker2._drain_once

        def _counting_drain_once() -> None:
            drain_calls["n"] += 1
            return original_drain_once()

        worker2._drain_once = _counting_drain_once  # type: ignore[method-assign]

        # _POLL_IDLE_S is 0.25s; wait several multiples of it so a retrying
        # worker would have attempted multiple rounds.
        poll_deadline = time.monotonic() + 1.5
        phases_seen: set[str] = set()
        while time.monotonic() < poll_deadline:
            snapshot = worker2.status()
            phases_seen.add(snapshot.phase)
            assert snapshot.processed_total == refused_processed
            time.sleep(0.05)

        assert drain_calls["n"] == 0, (
            f"expected zero retry rounds while refused, saw {drain_calls['n']}"
        )
        assert phases_seen == {"refused"}
    finally:
        worker2.stop()


def test_explicit_probe_request_clears_refused_phase(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    worker2, _paths = _seed_mass_missing_refusal_fixture(
        data_dir, state_db_path, tmp_path
    )
    try:
        worker2.request_probe(full=True, allow_mass_missing=True)
        started = time.monotonic()
        while time.monotonic() - started < THREAD_HANG_GUARD_S:
            snapshot = worker2.status()
            if snapshot.phase not in {"refused"}:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("explicit probe never cleared refused phase")
        assert snapshot.phase in {"queued", "running", "complete"}
    finally:
        worker2.stop()


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
    # A minimal, explicit test seam (LibraryAvailabilityWorker.set_on_batch_committed):
    # create_app() already owns this worker instance by the time it is reachable
    # via app.state.availability_worker, so the constructor's on_batch_committed
    # kwarg cannot be used here -- the hook must be installed on the real
    # instance before start() (triggered by entering the TestClient lifespan
    # below).
    worker: LibraryAvailabilityWorker = app.state.availability_worker

    first_batch_committed = threading.Event()
    release_probe = threading.Event()

    def _hold_after_first_batch(status: object) -> None:
        if first_batch_committed.is_set():
            return
        first_batch_committed.set()
        release_probe.wait(timeout=THREAD_HANG_GUARD_S)

    worker.set_on_batch_committed(_hold_after_first_batch)

    with TestClient(app) as client:
        try:
            # Warm the listing route first so the timed call measures waiting
            # on the probe, not first-request route initialization (which
            # flaked this test before it held the probe deterministically).
            warm = _get_with_deadline(
                client,
                "/api/v1/tracks",
                timeout=LISTING_HANG_BOUND_S,
                params={"limit": 1},
            )
            assert warm.status_code == 200

            assert first_batch_committed.wait(timeout=THREAD_HANG_GUARD_S), (
                "probe never committed its first batch"
            )
            # Provably still running, asserted BEFORE the timed listing call:
            # 150 seeded tracks at the worker's default batch size of 100
            # leaves a second batch pending while the worker thread sits
            # inside the hook above.
            status = client.get(STATUS_PATH).json()
            assert status["pending"] > 0

            list_started = time.monotonic()
            response = _get_with_deadline(
                client,
                "/api/v1/tracks",
                timeout=LISTING_HANG_BOUND_S,
                params={"limit": 5},
            )
            elapsed = time.monotonic() - list_started
            assert response.status_code == 200
            # `pending > 0` above is the structural proof the probe was still
            # running; this bound only catches a genuine hang (a fixed 2.0s
            # wall-clock bound failed 6 of 16 runs under ordinary machine
            # load before the probe was held deterministically).
            assert elapsed < LISTING_HANG_BOUND_S
        finally:
            # Always release, even on assertion failure above, so the worker
            # thread is not left blocked past this test (lifespan shutdown
            # would otherwise wait out stop()'s own 30s join).
            release_probe.set()

        final = _wait_status_complete(client, guard_s=THREAD_HANG_GUARD_S)
        assert final["pending"] == 0


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
