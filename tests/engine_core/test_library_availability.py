"""Engine-owned ``track_availability`` probing (issue #2588).

[if] a probe round would drop the present count catastrophically [then] it is refused, [else stop].
"""
from __future__ import annotations

import json
import os
import queue
import re
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
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    # P0 (Sol review, PR #2619 round 1): this used to mkdir + shutil.rmtree
    # a REAL /Volumes/<name> path. On any machine that happens to have a
    # real volume mounted under that literal name, that deleted user data
    # outside the test sandbox.
    #
    # P1 (Sol review, PR #2619 round 6): the fix for that used
    # `monkeypatch.setattr(avail, "VOLUMES_ROOT", ...)`, which swaps out a
    # real production input (the classifier's own contract for what
    # "/Volumes" means) for a fake one -- a mocked production input that
    # cannot establish real `/Volumes` paths classify correctly. The
    # worker now takes `volumes_root` as a constructor parameter (default:
    # the real `avail.VOLUMES_ROOT`, see
    # `test_worker_default_volumes_root_is_the_real_production_contract`),
    # threaded through to `probe`/`probe_batch`/`classify_path` exactly
    # like the existing `mounted` DI seam and like
    # `apps.webui.server.routes.usb_volumes`'s own `volumes_root`
    # parameter -- this test passes a real temp directory tree as that
    # parameter's VALUE, through the worker's own public constructor, not
    # by reaching into module internals. Every filesystem touch this test
    # makes is confined to `fake_volumes_root` under `tmp_path`,
    # auto-cleaned by pytest -- no writes to the real `/Volumes` and no
    # rmtree at all.
    fake_volumes_root = tmp_path / "Volumes"

    volume_name = "MDT2588VOL"
    volume_root = fake_volumes_root / volume_name / "Music"
    sid = _stable("mount")
    volume_path = str(volume_root / "a.mp3")

    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, sid, volume_path)
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(
        data_dir, batch_size=10, volumes_root=str(fake_volumes_root)
    )
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

    worker2 = LibraryAvailabilityWorker(
        data_dir, batch_size=10, volumes_root=str(fake_volumes_root)
    )
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


def test_background_round_guard_refuses_whole_round_not_partial_batches(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """P1 (Sol review, PR #2619, thread 4004468648): the mass-missing guard
    was evaluated batch by batch, AFTER each batch's classification, against
    the database's CURRENT present count -- which already reflects earlier
    batches in the SAME round that already committed. On a round split
    across multiple batches, an early batch's own drop can look safe in
    isolation and commit, and only a LATER batch trips the guard -- by which
    point the earlier batch's false absences are already durably persisted.
    The round as a whole must be refused before writing anything, not
    partway through it.
    """
    audio_dir = tmp_path / "guard-round"
    audio_dir.mkdir()
    paths: list[Path] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(12):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            paths.append(audio)
            _upsert_track(conn, _stable(f"gr{index:02d}"), str(audio))
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker.stop()

    # The whole volume goes missing: all 12 files gone.
    for audio in paths:
        audio.unlink()
    conn = state_db.open_rw(state_db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE deleted_at IS NULL", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    # batch_size=4 across 12 rows means 3 batches per round: the first
    # batch's own drop (4 of 12, 33%) looks safe in isolation and would
    # commit before the second batch's CUMULATIVE drop (8 of 12, 67%) trips
    # the guard -- exactly the batch-by-batch bug.
    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker2.start()
    started = time.monotonic()
    try:
        while time.monotonic() - started < THREAD_HANG_GUARD_S:
            if worker2.status().phase == "refused":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("background round never refused the mass-missing drop")
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        present_after = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state = 'present'"
        ).fetchone()[0]
        absent_after = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state = 'absent'"
        ).fetchone()[0]
        # A refused round must not have downgraded ANY row: the decision has
        # to be made before the first write, not partway through the round.
        assert absent_after == 0, (
            f"{absent_after} row(s) were downgraded to absent before the "
            "round was refused -- the guard ran too late"
        )
        assert present_after == 12
        assert conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 12
    finally:
        conn.close()


def test_background_round_commits_a_legitimate_small_drop(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Opposite-direction control for the round-level mass-missing guard: a
    genuinely small drop (well under the 50% threshold) must still commit
    normally rather than the round-level pre-check over-correcting into
    refusing safe rounds too.
    """
    audio_dir = tmp_path / "guard-round-safe"
    audio_dir.mkdir()
    paths: list[Path] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(12):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            paths.append(audio)
            _upsert_track(conn, _stable(f"gs{index:02d}"), str(audio))
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker.stop()

    # Only 2 of 12 go missing (17%), safely under the 50% guard threshold.
    # Bump updated_at ONLY for the two tracks whose files were actually
    # deleted: bumping the other 10 (whose classification will not change)
    # would trip the separate stale-checked_at bug and make this test hang,
    # rather than exercising the round-level guard control it is meant to be.
    for audio in paths[:2]:
        audio.unlink()
    conn = state_db.open_rw(state_db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE stable_id IN (?, ?)",
            (now, _stable("gs00"), _stable("gs01")),
        )
        conn.commit()
    finally:
        conn.close()

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker2.start()
    try:
        _wait_worker_complete(worker2, guard_s=THREAD_HANG_GUARD_S)
        assert worker2.status().phase == "complete"
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM track_availability WHERE state = 'absent'"
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT COUNT(*) FROM tracks_available").fetchone()[0] == 10
    finally:
        conn.close()


def test_background_round_settles_metadata_only_reprobe(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Repro for the checked_at-staleness bug: a track re-probed because
    ``tracks.updated_at`` changed, whose classification (state + checked_path)
    comes back IDENTICAL to what is already stored, must still settle --
    not sit stale forever and get re-probed on every single pass.

    The worker polls every ``_POLL_IDLE_S`` (0.25s); on the buggy code this
    row is re-selected, re-written as a content no-op that skips the
    checked_at stamp, and immediately re-selected again next poll, so a 5s
    bound (roughly 20 rounds) is ample to distinguish "never settles" from
    "briefly slow" -- real settling completes well within a single round.
    """
    audio_dir = tmp_path / "checked-at-settle"
    audio_dir.mkdir()
    audio = audio_dir / "track.mp3"
    audio.write_bytes(b"\x00")
    stable_id = _stable("ck01")
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        state_before, checked_at_before = conn.execute(
            "SELECT state, checked_at FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        assert state_before == "present"
    finally:
        conn.close()

    # Metadata-only change: the title differs (a genuine field change that
    # bumps tracks.updated_at) but the file path -- and hence the
    # classification -- is unchanged, so the round's write is a content
    # no-op for this row.
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio), title="Retitled Track")
    finally:
        conn.close()

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker2.start()
    try:
        _wait_worker_complete(worker2, guard_s=5.0)
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        checked_at_after = conn.execute(
            "SELECT checked_at FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()[0]
        assert checked_at_after > checked_at_before, (
            "checked_at never advanced after a re-probe whose state/path "
            "came back unchanged -- the row is stale forever"
        )
    finally:
        conn.close()


def test_background_round_settles_a_genuine_reclassification(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Opposite-direction control for the checked_at-staleness fix: a row
    whose re-probe DOES change classification (present -> absent) must
    still settle in exactly one extra pass and advance checked_at --
    proving the fix does not starve real reclassifications while it stops
    chasing no-op ones.

    Seeded with 6 tracks (only 1 goes missing = 17%) so the reclassification
    itself stays safely under the unrelated mass-missing guard's threshold.
    """
    audio_dir = tmp_path / "checked-at-genuine"
    audio_dir.mkdir()
    stable_id = _stable("ck02")
    target_audio: Path | None = None
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(6):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            sid = stable_id if index == 0 else _stable(f"ck02filler{index}")
            _upsert_track(conn, sid, str(audio))
            if index == 0:
                target_audio = audio
    finally:
        conn.close()
    assert target_audio is not None

    worker = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        checked_at_before = conn.execute(
            "SELECT checked_at FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    target_audio.unlink()
    conn = state_db.open_rw(state_db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE stable_id = ?",
            (now, stable_id),
        )
        conn.commit()
    finally:
        conn.close()

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=4)
    worker2.start()
    try:
        _wait_worker_complete(worker2, guard_s=THREAD_HANG_GUARD_S)
        status_after = worker2.status()
    finally:
        worker2.stop()

    assert status_after.processed_total == 1, (
        "a genuine reclassification must settle in exactly one pass, not loop -- "
        f"got processed_total={status_after.processed_total}"
    )
    conn = state_db.open_rw(state_db_path)
    try:
        state_after, checked_at_after = conn.execute(
            "SELECT state, checked_at FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        assert state_after == "absent"
        assert checked_at_after > checked_at_before
    finally:
        conn.close()


def test_probe_batch_chunks_the_in_clause_at_id_bind_batch(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """A large round must never build one unbounded ``stable_id IN (...)``.

    ``_collect_round_stable_ids`` (round-2's LIBM-41 fix) gathers the WHOLE
    round's candidate ids up front and hands them to ``avail.probe_batch``
    in one call. If that call binds one SQL placeholder per id in a SINGLE
    query, a library bigger than SQLite's compiled
    ``SQLITE_MAX_VARIABLE_NUMBER`` -- 999 on many builds, including the
    packaged desktop build (see ``apps/shared/state/locations.py``'s own
    ``ID_BIND_BATCH`` comment), 32766 on others -- raises "too many SQL
    variables" and the worker fails instead of classifying anything.

    Sized at the codebase's own established bound for this exact class of
    query (``ID_BIND_BATCH``, already used by ``list_location_paths`` and
    ``sids_with_remote_copy``) plus a margin, and verified via SQLite's
    trace callback rather than by trying to exceed the REAL compiled limit
    (which ranges from 999 to 32766 depending on the machine running the
    test) -- so this stays fast and portable instead of needing tens of
    thousands of seeded rows on a high-limit build.
    """
    from apps.shared.state.locations import ID_BIND_BATCH

    audio_dir = tmp_path / "bind-count"
    audio_dir.mkdir()
    track_count = ID_BIND_BATCH + 100
    conn = state_db.open_rw(state_db_path)
    try:
        with StateWriter(conn, actor="test-availability") as writer:
            for index in range(track_count):
                writer.upsert_track(
                    stable_id=_stable(f"bc{index:06d}"),
                    stable_id_tier="inferred",
                    title=f"bc{index}",
                    artists=[],
                    album=None,
                    isrc=None,
                    duration_ms=None,
                    file_path=str(audio_dir / f"{index}.mp3"),
                )
        conn.commit()

        traced_sql: list[str] = []
        conn.set_trace_callback(traced_sql.append)
        try:
            candidate_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT stable_id FROM tracks ORDER BY stable_id"
                )
            ]
            assert len(candidate_ids) == track_count
            rows = avail.probe_batch(conn, candidate_ids)
            assert len(rows) == track_count
        finally:
            conn.set_trace_callback(None)
    finally:
        conn.close()

    max_bound = 0
    for sql in traced_sql:
        match = re.search(r"stable_id IN \(([^)]*)\)", sql)
        if match:
            max_bound = max(max_bound, match.group(1).count(",") + 1)
    assert max_bound <= ID_BIND_BATCH, (
        f"a single query bound {max_bound} stable_id values into one "
        f"IN (...) clause -- probe_batch must chunk at ID_BIND_BATCH "
        f"({ID_BIND_BATCH}) like every other bulk id lookup, or a library "
        "over SQLite's compiled variable limit fails outright"
    )


def test_collect_round_stable_ids_caps_each_round_at_max_round_ids(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """A library bigger than MAX_ROUND_IDS must be gathered across several
    rounds, each one capped -- not one unbounded round that stats (and
    holds in memory) the whole library before its first write commits.
    """
    from apps.engine_core.library_availability import MAX_ROUND_IDS

    audio_dir = tmp_path / "round-cap"
    audio_dir.mkdir()
    track_count = MAX_ROUND_IDS + 400
    all_ids = {_stable(f"rc{index:06d}") for index in range(track_count)}
    conn = state_db.open_rw(state_db_path)
    try:
        with StateWriter(conn, actor="test-availability") as writer:
            for index in range(track_count):
                writer.upsert_track(
                    stable_id=_stable(f"rc{index:06d}"),
                    stable_id_tier="inferred",
                    title=f"rc{index}",
                    artists=[],
                    album=None,
                    isrc=None,
                    duration_ms=None,
                    file_path=str(audio_dir / f"{index}.mp3"),
                )
        conn.commit()

        worker = LibraryAvailabilityWorker(data_dir, batch_size=500)
        round1, round1_cursor = worker._collect_round_stable_ids(conn)
        # `_collect_round_stable_ids` no longer commits the cursor itself
        # (round-6 Sol finding: only after a round's guard passes and its
        # writes commit) -- do that explicitly here so round 2 resumes
        # from round 1's true tail, exactly as `_run_capped_round` would.
        worker._commit_round_cursor(round1_cursor)
        round2, _round2_cursor = worker._collect_round_stable_ids(conn)
    finally:
        conn.close()

    assert len(round1) == MAX_ROUND_IDS, (
        f"first round collected {len(round1)} ids, expected exactly the "
        f"cap ({MAX_ROUND_IDS}) when the library has more incomplete rows "
        "than that"
    )
    assert len(round2) == track_count - MAX_ROUND_IDS
    assert set(round1).isdisjoint(round2), "the two rounds must not overlap"
    assert set(round1) | set(round2) == all_ids, (
        "the two capped rounds together must still cover every row"
    )


def test_priority_id_does_not_corrupt_the_keyset_cursor_across_rounds(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Round-5 Sol finding (BLOCKING P1): a priority id supplied mid-scan
    must not advance ``_keyset_cursor`` past rows the keyset scan itself
    never visited.

    Library: 2 * MAX_ROUND_IDS + 400 rows, so settling needs 3 rounds.
    Round 1 is a pure keyset scan (no priority ids yet) and correctly
    leaves the cursor at its own true tail. Between round 1 and round 2, a
    priority id is enqueued -- the row that sorts HIGHEST in the whole
    library, i.e. one the keyset scan itself would only reach in some
    later round, exactly like a ``library.changed`` notification arriving
    mid-scan for an arbitrary row. ``batch_size`` is set larger than one
    round's candidate count so round 2 commits as a single chunk, making
    the corruption (if present) land in the FINAL value of
    ``_keyset_cursor`` for that round, not just a value transiently
    overwritten and then correctly overwritten again by a later chunk.

    Pre-fix, ``_commit_batch`` sets ``_keyset_cursor = max(stable_ids)``
    over that single chunk, which includes the priority id -- the highest
    id in the entire library -- so the cursor jumps to the library's end.
    Round 3's incomplete scan then runs ``WHERE stable_id > <library end>``,
    finds nothing, and incorrectly concludes the incomplete scan is
    exhausted, permanently skipping the real unclassified rows between the
    round 2 keyset tail and the priority id.
    """
    from apps.engine_core.library_availability import MAX_ROUND_IDS

    audio_dir = tmp_path / "priority-cursor"
    audio_dir.mkdir()
    track_count = 2 * MAX_ROUND_IDS + 400
    all_ids = [_stable(f"pc{index:06d}") for index in range(track_count)]
    conn = state_db.open_rw(state_db_path)
    try:
        with StateWriter(conn, actor="test-availability") as writer:
            for index, stable_id in enumerate(all_ids):
                writer.upsert_track(
                    stable_id=stable_id,
                    stable_id_tier="inferred",
                    title=f"pc{index}",
                    artists=[],
                    album=None,
                    isrc=None,
                    duration_ms=None,
                    # Never created: classifies to `absent` without touching
                    # the filesystem for thousands of files.
                    file_path=str(audio_dir / f"{index}.mp3"),
                )
        conn.commit()
    finally:
        conn.close()

    # Bigger than one round's candidate count (MAX_ROUND_IDS), so each
    # round's writes commit as a single `_commit_batch` chunk.
    worker = LibraryAvailabilityWorker(data_dir, batch_size=MAX_ROUND_IDS + 500)

    # Round 1: pure keyset scan, indices 0..MAX_ROUND_IDS-1. No priority
    # ids involved -- both pre- and post-fix this leaves the cursor at the
    # scan's own true tail.
    worker._drain_once()
    expected_round1_cursor = all_ids[MAX_ROUND_IDS - 1]
    assert worker._keyset_cursor == expected_round1_cursor, (
        f"round 1 cursor = {worker._keyset_cursor!r}, expected the "
        f"keyset scan's own tail {expected_round1_cursor!r}"
    )

    # A `library.changed`-style priority request for the row that sorts
    # LAST in the whole library, arriving between round 1 and round 2.
    priority_id = all_ids[-1]
    worker.request_probe([priority_id])

    # Round 2: MAX_ROUND_IDS candidates = [priority_id] + the next
    # MAX_ROUND_IDS-1 keyset-scanned rows (indices MAX_ROUND_IDS..
    # 2*MAX_ROUND_IDS-2). `_collect_round_stable_ids` itself must set the
    # cursor to that keyset scan's own tail, NOT to the priority id.
    worker._drain_once()
    expected_round2_cursor = all_ids[2 * MAX_ROUND_IDS - 2]
    assert worker._keyset_cursor == expected_round2_cursor, (
        f"round 2 cursor = {worker._keyset_cursor!r}, expected the keyset "
        f"scan's own tail {expected_round2_cursor!r} -- a chunk containing "
        "the priority id must not be allowed to set the cursor"
    )

    # Round 3 (and a generous few more, in case the real per-round cap
    # ever changes): must resume exactly where round 2's keyset scan left
    # off and finish classifying every remaining row.
    for _ in range(4):
        if worker.status().pending == 0:
            break
        worker._drain_once()

    status = worker.status()
    assert status.pending == 0, (
        f"{status.pending} row(s) never settled -- a corrupted cursor "
        "made a later round believe the incomplete scan was exhausted"
    )
    conn = state_db.open_rw(state_db_path)
    try:
        classified = conn.execute(
            "SELECT COUNT(*) FROM track_availability"
        ).fetchone()[0]
    finally:
        conn.close()
    assert classified == track_count, (
        f"only {classified} of {track_count} rows were ever classified -- "
        "the rows between the round 2 keyset tail and the priority id "
        "were skipped"
    )


def test_priority_id_is_still_probed_ahead_of_its_natural_scan_position(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Opposite-direction control for the cursor fix above: a priority id
    must still be classified in the very next round, well ahead of where
    the keyset scan would naturally reach it on its own -- the fix must
    only stop the priority id from corrupting the cursor, not remove the
    priority queue's fast-track purpose.
    """
    from apps.engine_core.library_availability import MAX_ROUND_IDS

    audio_dir = tmp_path / "priority-fast-track"
    audio_dir.mkdir()
    track_count = MAX_ROUND_IDS + 200
    all_ids = [_stable(f"pf{index:06d}") for index in range(track_count)]
    conn = state_db.open_rw(state_db_path)
    try:
        with StateWriter(conn, actor="test-availability") as writer:
            for index, stable_id in enumerate(all_ids):
                writer.upsert_track(
                    stable_id=stable_id,
                    stable_id_tier="inferred",
                    title=f"pf{index}",
                    artists=[],
                    album=None,
                    isrc=None,
                    duration_ms=None,
                    file_path=str(audio_dir / f"{index}.mp3"),
                )
        conn.commit()
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=MAX_ROUND_IDS + 500)
    # Index MAX_ROUND_IDS + 100: the keyset scan alone would not reach
    # this row until round 2 (round 1's cap is MAX_ROUND_IDS rows,
    # indices 0..MAX_ROUND_IDS-1).
    priority_id = all_ids[MAX_ROUND_IDS + 100]
    worker.request_probe([priority_id])

    worker._drain_once()

    conn = state_db.open_rw(state_db_path)
    try:
        row = conn.execute(
            "SELECT state FROM track_availability WHERE stable_id = ?",
            (priority_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, (
        "the priority id was not classified in round 1, even though it "
        "was requested ahead of the keyset scan reaching it naturally"
    )
    assert row[0] == "absent"

    # And the fix above still holds: the round's cursor reflects only the
    # keyset scan's own tail, not the priority id mixed into the chunk.
    expected_cursor = all_ids[MAX_ROUND_IDS - 2]
    assert worker._keyset_cursor == expected_cursor, (
        f"round 1 cursor = {worker._keyset_cursor!r}, expected the keyset "
        f"scan's own tail {expected_cursor!r}"
    )


def test_status_pending_does_not_double_count_a_stale_awaiting_volume_row(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Round-5 Sol finding (NON-BLOCKING P2): a row that is BOTH stale
    (``tracks.updated_at > track_availability.checked_at``) AND
    ``awaiting_volume`` must be counted once in ``pending``, not twice.
    """
    audio = tmp_path / "overlap.mp3"
    audio.write_bytes(b"\x00")
    stable_id = _stable("ov")

    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
        # A checked_at far in the past guarantees `tracks.updated_at >
        # track_availability.checked_at` (stale), while the row's own
        # state is independently `awaiting_volume`.
        conn.execute(
            "INSERT INTO track_availability "
            "(stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
            (stable_id, "awaiting_volume", str(audio), "2000-01-01T00:00:00+00:00"),
        )
        conn.commit()

        worker = LibraryAvailabilityWorker(data_dir)
        counts = worker._status_counts_from_conn(conn)
    finally:
        conn.close()

    assert counts["stale"] == 1
    assert counts["awaiting_volume"] == 1
    assert counts["pending"] == 1, (
        f"pending={counts['pending']}, expected 1 -- a row that is both "
        "stale and awaiting_volume must be counted once, not summed "
        "across overlapping categories"
    )


def _seed_present_then_mass_drop(
    conn: sqlite3.Connection, audio_dir: Path, prefix: str, track_count: int
) -> list[Path]:
    """``track_count`` tracks, all with real audio (so a first round
    classifies them all ``present``). Returns their paths, unlinked.
    """
    paths: list[Path] = []
    for index in range(track_count):
        audio = audio_dir / f"{index}.mp3"
        audio.write_bytes(b"\x00")
        paths.append(audio)
        _upsert_track(conn, _stable(f"{prefix}{index:06d}"), str(audio))
    return paths


def test_refused_round_does_not_advance_the_keyset_cursor(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Round-6 Sol finding (BLOCKING P1): a round whose guard refuses must
    leave ``_keyset_cursor``/``_scan_awaiting_volume`` (and any priority
    ids it took) exactly as ``_collect_round_stable_ids`` found them, so a
    retry re-sees the same candidate rows instead of silently resuming
    past them.
    """
    audio_dir = tmp_path / "refused-cursor"
    audio_dir.mkdir()
    conn = state_db.open_rw(state_db_path)
    try:
        paths = _seed_present_then_mass_drop(conn, audio_dir, "rc", 10)
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker._drain_once()  # round 1: all 10 present, sweep completes cleanly.
    conn = state_db.open_rw(state_db_path)
    try:
        present = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state='present'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert present == 10

    # 6 of 10 files disappear (60% drop, past LIBM-41's 50% threshold);
    # bump updated_at so the incomplete scan re-selects every row.
    for audio in paths[:6]:
        audio.unlink()
    conn = state_db.open_rw(state_db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE deleted_at IS NULL", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    cursor_before = worker._keyset_cursor
    scan_awaiting_before = worker._scan_awaiting_volume
    priority_id = _stable("rc000003")  # one of the about-to-be-refused rows
    worker.request_probe([priority_id])
    priority_before = list(worker._priority_ids)

    worker._drain_once()  # round 2: guard must refuse (60% > 50%).
    status = worker.status()
    assert status.phase == "refused", f"expected refusal, got {status.phase!r}"

    assert worker._keyset_cursor == cursor_before, (
        f"cursor moved to {worker._keyset_cursor!r} from {cursor_before!r} "
        "even though the round never wrote anything"
    )
    assert worker._scan_awaiting_volume == scan_awaiting_before, (
        "scan_awaiting flipped even though the refused round never wrote "
        "anything -- a later ordinary round would jump straight past the "
        "incomplete scan and never revisit these rows"
    )
    assert list(worker._priority_ids) == priority_before, (
        "the priority id was popped and lost even though the round that "
        "took it was refused"
    )

    # Consequence: force the refusal through, and confirm every row --
    # including the priority id -- still gets classified. Nothing was
    # silently skipped by a cursor that had already run past them.
    worker.request_probe(full=True, allow_mass_missing=True)
    worker._drain_once()
    conn = state_db.open_rw(state_db_path)
    try:
        classified = conn.execute(
            "SELECT COUNT(*) FROM track_availability"
        ).fetchone()[0]
        absent = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state='absent'"
        ).fetchone()[0]
        priority_state = conn.execute(
            "SELECT state FROM track_availability WHERE stable_id = ?",
            (priority_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert classified == 10
    assert absent == 6
    assert priority_state == "absent"


def test_successful_round_still_advances_the_keyset_cursor(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Opposite-direction control for the fix above: a round whose guard
    PASSES must still advance the cursor once its writes commit -- the fix
    only defers the commit past a successful write, it must not stop the
    cursor from ever advancing.
    """
    from apps.engine_core.library_availability import MAX_ROUND_IDS

    audio_dir = tmp_path / "successful-cursor"
    audio_dir.mkdir()
    track_count = MAX_ROUND_IDS + 400
    all_ids = [_stable(f"sc{index:06d}") for index in range(track_count)]
    conn = state_db.open_rw(state_db_path)
    try:
        with StateWriter(conn, actor="test-availability") as writer:
            for index, stable_id in enumerate(all_ids):
                writer.upsert_track(
                    stable_id=stable_id,
                    stable_id_tier="inferred",
                    title=f"sc{index}",
                    artists=[],
                    album=None,
                    isrc=None,
                    duration_ms=None,
                    file_path=str(audio_dir / f"{index}.mp3"),
                )
        conn.commit()
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=500)
    assert worker._keyset_cursor == ""
    worker._drain_once()
    expected_cursor = all_ids[MAX_ROUND_IDS - 1]
    assert worker._keyset_cursor == expected_cursor, (
        f"a successful round left the cursor at {worker._keyset_cursor!r}, "
        f"expected the scan's own tail {expected_cursor!r}"
    )
    assert worker.status().phase in {"queued", "idle", "running"}


def test_cumulative_present_loss_is_guarded_across_capped_rounds(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Round-6 Sol finding (BLOCKING P1): a mass-missing drop spread
    across several capped rounds, each individually UNDER the 50% LIBM-41
    threshold relative to the already-reduced present count, must still
    be refused once the CUMULATIVE loss since the sweep began exceeds it.
    """
    from apps.engine_core.library_availability import MAX_ROUND_IDS

    track_count = 3 * MAX_ROUND_IDS  # 6000: exactly 3 capped rounds.
    audio_dir = tmp_path / "cumulative-guard"
    audio_dir.mkdir()
    conn = state_db.open_rw(state_db_path)
    try:
        paths = _seed_present_then_mass_drop(conn, audio_dir, "cg", track_count)
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=MAX_ROUND_IDS)
    # First sweep: classify everything present (3 capped rounds; nothing
    # to guard against yet, round_start_present starts at 0).
    for _ in range(6):
        if worker.status().pending == 0:
            break
        worker._drain_once()
    conn = state_db.open_rw(state_db_path)
    try:
        present = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state='present'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert present == track_count

    # The whole volume disconnects: every file gone, every row marked
    # incomplete again so the next sweep re-scans all of them.
    for audio in paths:
        audio.unlink()
    conn = state_db.open_rw(state_db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE deleted_at IS NULL", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    # Round 1 of the new sweep: 2000/6000 = 33% cumulative drop, under the
    # threshold either way -- must commit.
    worker._drain_once()
    status = worker.status()
    assert status.phase != "refused", f"round 1 refused unexpectedly: {status.last_error}"
    conn = state_db.open_rw(state_db_path)
    try:
        present_after_round1 = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state='present'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert present_after_round1 == track_count - MAX_ROUND_IDS

    # Round 2: relative to round 1's ALREADY-REDUCED present count
    # (4000), this round's own drop is exactly 50% -- a round-local
    # baseline would let it pass. Relative to the SWEEP's original
    # baseline (6000), the cumulative drop is 4000/6000 = 66.7% -- must
    # be refused.
    worker._drain_once()
    status = worker.status()
    assert status.phase == "refused", (
        f"expected the cumulative drop (4000 of 6000, 66.7%) to be "
        f"refused, got phase={status.phase!r} pending={status.pending}"
    )
    conn = state_db.open_rw(state_db_path)
    try:
        present_after_round2 = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state='present'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert present_after_round2 == track_count - MAX_ROUND_IDS, (
        "round 2 must not have committed any downgrades once refused"
    )


def test_small_cumulative_drop_still_commits_across_capped_rounds(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Opposite-direction control: a genuinely small drop (well under the
    LIBM-41 threshold even against the sweep's ORIGINAL baseline), spread
    across several capped rounds, must still commit -- the cumulative
    guard must not block a legitimate sweep just because settling took
    more than one round.
    """
    from apps.engine_core.library_availability import MAX_ROUND_IDS

    track_count = 3 * MAX_ROUND_IDS  # 6000, needs 3 capped rounds either way.
    drop_count = 300  # 5% of 6000, nowhere near the 50% threshold.
    audio_dir = tmp_path / "small-cumulative-drop"
    audio_dir.mkdir()
    conn = state_db.open_rw(state_db_path)
    try:
        paths = _seed_present_then_mass_drop(conn, audio_dir, "sd", track_count)
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=MAX_ROUND_IDS)
    for _ in range(6):
        if worker.status().pending == 0:
            break
        worker._drain_once()
    conn = state_db.open_rw(state_db_path)
    try:
        present = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state='present'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert present == track_count

    for audio in paths[:drop_count]:
        audio.unlink()
    conn = state_db.open_rw(state_db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE deleted_at IS NULL", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    for _ in range(8):
        if worker.status().pending == 0:
            break
        worker._drain_once()
    status = worker.status()
    assert status.phase != "refused", (
        f"a genuinely small ({drop_count} of {track_count}) drop spread "
        f"across several capped rounds was refused: {status.last_error}"
    )
    assert status.pending == 0
    conn = state_db.open_rw(state_db_path)
    try:
        present_final = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state='present'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert present_final == track_count - drop_count


def test_worker_default_volumes_root_is_the_real_production_contract(
    data_dir: Path,
) -> None:
    """Round-6 Sol finding (BLOCKING P1, test quality): the worker's
    default ``volumes_root`` must be the REAL production ``/Volumes``
    contract (:data:`apps.mik.availability.VOLUMES_ROOT`), never silently
    something else -- a test that needs isolation passes an explicit
    override (see the DI seam threaded through `probe`/`probe_batch`/
    `classify_path`), it never monkeypatches the module constant.
    """
    assert avail.VOLUMES_ROOT == "/Volumes"
    worker = LibraryAvailabilityWorker(data_dir)
    assert worker._volumes_root == "/Volumes"
    assert worker._volumes_root == avail.VOLUMES_ROOT


def test_background_round_settles_a_library_larger_than_the_round_cap(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Integration-level repro: a library bigger than MAX_ROUND_IDS must
    still fully settle, via the real background thread looping across
    several rounds (each capped) rather than needing one unbounded round.
    """
    from apps.engine_core.library_availability import MAX_ROUND_IDS

    audio_dir = tmp_path / "round-cap-e2e"
    audio_dir.mkdir()
    track_count = MAX_ROUND_IDS + 400
    conn = state_db.open_rw(state_db_path)
    try:
        with StateWriter(conn, actor="test-availability") as writer:
            for index in range(track_count):
                writer.upsert_track(
                    stable_id=_stable(f"re{index:06d}"),
                    stable_id_tier="inferred",
                    title=f"re{index}",
                    artists=[],
                    album=None,
                    isrc=None,
                    duration_ms=None,
                    # Never created: classifies to `absent` without touching
                    # the filesystem for thousands of files.
                    file_path=str(audio_dir / f"{index}.mp3"),
                )
        conn.commit()
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=500)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
        status = worker.status()
    finally:
        worker.stop()

    assert status.phase == "complete"
    assert status.pending == 0
    assert status.processed_total == track_count, (
        f"expected every one of {track_count} rows classified exactly "
        f"once across however many capped rounds it took, got "
        f"processed_total={status.processed_total}"
    )

    conn = state_db.open_rw(state_db_path)
    try:
        absent_count = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state = 'absent'"
        ).fetchone()[0]
        assert absent_count == track_count
    finally:
        conn.close()


def test_background_round_guard_refuses_a_mass_drop_within_one_bounded_round(
    data_dir: Path, state_db_path: Path, tmp_path: Path
) -> None:
    """Opposite-direction control for the MAX_ROUND_IDS cap: a round well
    under the cap (but big enough to span several ID_BIND_BATCH-sized
    probe_batch chunks) must still have a catastrophic drop refused as ONE
    whole-round decision -- capping a round must not weaken or bypass the
    LIBM-41 guard for rounds that fit comfortably inside it.
    """
    from apps.shared.state.locations import ID_BIND_BATCH

    audio_dir = tmp_path / "bounded-round-guard"
    audio_dir.mkdir()
    track_count = ID_BIND_BATCH + 100  # 600: several probe_batch chunks, << MAX_ROUND_IDS
    paths: list[Path] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(track_count):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            paths.append(audio)
            _upsert_track(conn, _stable(f"bg{index:06d}"), str(audio))
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=200)
    worker.start()
    try:
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    finally:
        worker.stop()

    for audio in paths:
        audio.unlink()
    conn = state_db.open_rw(state_db_path)
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tracks SET updated_at = ? WHERE deleted_at IS NULL", (now,)
        )
        conn.commit()
    finally:
        conn.close()

    worker2 = LibraryAvailabilityWorker(data_dir, batch_size=200)
    worker2.start()
    try:
        started = time.monotonic()
        while time.monotonic() - started < THREAD_HANG_GUARD_S:
            if worker2.status().phase == "refused":
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "background round never refused the mass-missing drop"
            )
    finally:
        worker2.stop()

    conn = state_db.open_rw(state_db_path)
    try:
        absent_after = conn.execute(
            "SELECT COUNT(*) FROM track_availability WHERE state = 'absent'"
        ).fetchone()[0]
        assert absent_after == 0, (
            f"{absent_after} row(s) were downgraded before the round was "
            f"refused, even though the whole round ({track_count} rows, "
            "several probe_batch chunks) fit well inside MAX_ROUND_IDS"
        )
    finally:
        conn.close()


def test_status_reads_every_counter_from_one_consistent_snapshot(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status() must never report counters describing different moments.

    Pre-fix, ``status()`` computed ``pending`` and ``present`` via TWO
    separate connections (``_count_pending``/``_count_present``), and never
    refreshed ``unknown``/``stale``/``awaiting_volume`` at all outside a
    completed batch. A write landing between those two connection-opens is
    visible to the later read but not the earlier one, so the response's
    own invariant -- ``unknown + stale + awaiting_volume == pending`` --
    can break.

    Simulated deterministically rather than as a real thread race (which
    this project's own conventions treat as unreliable/non-deterministic):
    the SECOND time this worker opens a connection during one ``status()``
    call, a write lands via a separate connection just before it, standing
    in for a background round committing mid-status()-call. Post-fix,
    ``status()`` opens exactly ONE connection per call, so this injection
    point is never reached at all.
    """
    audio_dir = tmp_path / "status-snapshot"
    audio_dir.mkdir()
    audio = audio_dir / "a.mp3"
    audio.write_bytes(b"\x00")
    stable_id = _stable("ss01")
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir)
    open_count = 0
    original_open_conn = worker._open_conn

    def counting_open_conn() -> sqlite3.Connection:
        nonlocal open_count
        open_count += 1
        if open_count == 2:
            side_conn = state_db.open_rw(state_db_path)
            try:
                rows = avail.probe_batch(side_conn, [stable_id])
                avail.write(side_conn, rows, apply_mass_missing_guard=False)
                side_conn.commit()
            finally:
                side_conn.close()
        return original_open_conn()

    monkeypatch.setattr(worker, "_open_conn", counting_open_conn)

    snapshot = worker.status()

    assert (
        snapshot.unknown + snapshot.stale + snapshot.awaiting_volume
        == snapshot.pending
    ), (
        "status() counters describe different moments: "
        f"unknown={snapshot.unknown} stale={snapshot.stale} "
        f"awaiting_volume={snapshot.awaiting_volume} do not sum to "
        f"pending={snapshot.pending} -- a write landed between separate "
        "counter reads"
    )
    assert open_count <= 1, (
        f"status() opened {open_count} connections for one call -- every "
        "counter must come from a single connection/snapshot"
    )


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


def test_availability_commit_waits_under_immediate_lock(
    state_db_path: Path, tmp_path: Path
) -> None:
    audio_dir = tmp_path / "lock-wait"
    audio_dir.mkdir()
    audio = audio_dir / "one.mp3"
    audio.write_bytes(b"\x00")
    stable_id = _stable("lock")

    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
    finally:
        conn.close()

    row = avail.AvailabilityRow(stable_id=stable_id, state="present", checked_path=str(audio))
    errors: queue.Queue[Exception] = queue.Queue()
    written = threading.Event()

    def _commit_while_locked() -> None:
        try:
            writer = state_db.open_rw(state_db_path)
            try:
                avail.commit_availability_batch(
                    writer,
                    [row],
                    apply_mass_missing_guard=False,
                    always_refresh_checked_at=True,
                    existing_scope_stable_ids=[stable_id],
                )
                written.set()
            finally:
                writer.close()
        except Exception as exc:  # noqa: BLE001
            errors.put(exc)

    def _hold_immediate_lock() -> None:
        holder = state_db.open_rw(state_db_path)
        try:
            holder.execute("BEGIN IMMEDIATE")
            time.sleep(0.75)
            holder.execute("COMMIT")
        finally:
            holder.close()

    holder = threading.Thread(target=_hold_immediate_lock, daemon=True)
    holder.start()
    time.sleep(0.05)
    committer = threading.Thread(target=_commit_while_locked, daemon=True)
    committer.start()
    committer.join(timeout=THREAD_HANG_GUARD_S)
    holder.join(timeout=THREAD_HANG_GUARD_S)

    if not errors.empty():
        pytest.fail(f"commit failed: {errors.get_nowait()}")
    assert written.is_set()

    verify = state_db.open_rw(state_db_path)
    try:
        state = verify.execute(
            "SELECT state FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        assert state is not None and state[0] == "present"
    finally:
        verify.close()


def test_availability_commit_fails_fast_without_busy_timeout(
    state_db_path: Path, tmp_path: Path
) -> None:
    audio_dir = tmp_path / "lock-fail"
    audio_dir.mkdir()
    audio = audio_dir / "one.mp3"
    audio.write_bytes(b"\x00")
    stable_id = _stable("fast")

    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
    finally:
        conn.close()

    row = avail.AvailabilityRow(stable_id=stable_id, state="present", checked_path=str(audio))
    holder = state_db.open_rw(state_db_path)
    holder.execute("PRAGMA busy_timeout = 0")
    holder.execute("BEGIN IMMEDIATE")
    contender = state_db.open_rw(state_db_path)
    contender.execute("PRAGMA busy_timeout = 0")
    try:
        with pytest.raises(state_db.StateStoreBusyError):
            avail.commit_availability_batch(
                contender,
                [row],
                apply_mass_missing_guard=False,
                always_refresh_checked_at=True,
            )
    finally:
        holder.execute("COMMIT")
        holder.close()
        contender.close()


def _seed_worker_tracks(
    state_db_path: Path,
    tmp_path: Path,
    *,
    count: int,
    prefix: str,
) -> list[str]:
    audio_dir = tmp_path / prefix
    audio_dir.mkdir()
    stable_ids: list[str] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(count):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\x00")
            stable_id = _stable(f"{prefix}{index}")
            stable_ids.append(stable_id)
            _upsert_track(conn, stable_id, str(audio))
    finally:
        conn.close()
    return stable_ids


def test_worker_retries_busy_lock_instead_of_failed_phase(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
) -> None:
    _seed_worker_tracks(state_db_path, tmp_path, count=12, prefix="busy-retry-")
    worker = LibraryAvailabilityWorker(data_dir, batch_size=4)
    drain_calls = {"n": 0}
    original_drain_once = worker._drain_once

    def _counting_drain_once() -> None:
        drain_calls["n"] += 1
        return original_drain_once()

    worker._drain_once = _counting_drain_once  # type: ignore[method-assign]

    release_lock = threading.Event()
    stop_contender = threading.Event()

    def _pulse_write_lock() -> None:
        while not stop_contender.is_set():
            holder = state_db.open_rw(state_db_path)
            try:
                holder.execute("BEGIN IMMEDIATE")
                release_lock.wait(timeout=0.15)
                holder.execute("COMMIT")
            finally:
                holder.close()
            time.sleep(0.02)

    contender = threading.Thread(target=_pulse_write_lock, daemon=True)
    contender.start()
    worker.start()
    time.sleep(0.2)
    release_lock.set()
    _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
    stop_contender.set()
    contender.join(timeout=2.0)

    snapshot = worker.status()
    assert snapshot.phase != "failed"
    assert snapshot.processed_total > 0
    assert drain_calls["n"] >= 1
    worker.stop()


def test_lock_error_on_first_batch_still_checks_every_row(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A database-locked first batch must not leave the worker idle.

    The first commit attempts raise ``database is locked`` until the retry
    budget is spent. Every seeded row still gets a ``track_availability``
    row, and the give-up is logged at error.
    """
    stable_ids = _seed_worker_tracks(
        state_db_path, tmp_path, count=6, prefix="lock-drain-"
    )
    monkeypatch.setattr(
        "apps.engine_core.library_availability.AVAILABILITY_LOCK_RETRIES",
        2,
    )
    monkeypatch.setattr(
        "apps.engine_core.library_availability._AVAILABILITY_LOCK_BACKOFF_BASE_S",
        0.01,
    )
    monkeypatch.setattr(
        "apps.engine_core.library_availability._AVAILABILITY_LOCK_BACKOFF_MAX_S",
        0.02,
    )
    calls = {"n": 0}
    original = avail.commit_availability_batch

    def _locked_first_batch(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    monkeypatch.setattr(avail, "commit_availability_batch", _locked_first_batch)

    worker = LibraryAvailabilityWorker(data_dir, batch_size=3)
    with caplog.at_level("ERROR"):
        worker.start()
        _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)
        worker.stop()

    assert any(
        "gave up this round" in record.message for record in caplog.records
    )
    conn = state_db.open_rw(state_db_path)
    try:
        rows = conn.execute(
            "SELECT stable_id FROM track_availability WHERE stable_id IN "
            f"({','.join('?' for _ in stable_ids)})",
            stable_ids,
        ).fetchall()
    finally:
        conn.close()
    assert {row[0] for row in rows} == set(stable_ids)


def test_mid_round_busy_requeues_priority_ids(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
) -> None:
    stable_ids = _seed_worker_tracks(
        state_db_path, tmp_path, count=8, prefix="prio-requeue-"
    )
    priority_id = stable_ids[0]
    worker = LibraryAvailabilityWorker(data_dir, batch_size=2)
    worker.request_probe([priority_id])

    requeued: list[list[str]] = []
    original_requeue = worker._requeue_priority_ids

    def _spy_requeue(ids: list[str]) -> None:
        requeued.append(list(ids))
        original_requeue(ids)

    worker._requeue_priority_ids = _spy_requeue  # type: ignore[method-assign]

    stop_holder = threading.Event()
    holder_ready = threading.Event()

    def _hold_write_lock_until_stopped() -> None:
        holder = state_db.open_rw(state_db_path)
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder_ready.set()
            while not stop_holder.wait(timeout=0.05):
                pass
            holder.execute("COMMIT")
        finally:
            holder.close()

    holder = threading.Thread(target=_hold_write_lock_until_stopped, daemon=True)
    holder.start()
    assert holder_ready.wait(timeout=2.0), "contending write lock never acquired"
    worker.start()

    started = time.monotonic()
    while time.monotonic() - started < THREAD_HANG_GUARD_S:
        snapshot = worker.status()
        if snapshot.phase == "queued" and snapshot.last_error and "locked" in snapshot.last_error:
            break
        time.sleep(0.02)
    else:
        stop_holder.set()
        holder.join(timeout=2.0)
        worker.stop()
        pytest.fail("worker never entered queued lock-retry state")

    stop_holder.set()
    holder.join(timeout=2.0)
    _wait_worker_complete(worker, guard_s=THREAD_HANG_GUARD_S)

    assert any(priority_id in batch for batch in requeued)
    conn = state_db.open_rw(state_db_path)
    try:
        row = conn.execute(
            "SELECT state FROM track_availability WHERE stable_id = ?",
            (priority_id,),
        ).fetchone()
        assert row is not None
    finally:
        conn.close()
    worker.stop()
