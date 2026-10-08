"""Path-map resolver cases for the availability worker.

[if] a path-map classification, idle reprobe, resolver-stamp invalidation, or retryable unreadable map applies [then] the worker classifies, re-probes, or stays queued to retry, [else stop].

Split from ``test_library_availability.py``: these pin path-map.json
classification, the idle reprobe, and resolver-stamp invalidation.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from apps.engine_core.library_availability import (
    _RESOLVER_MARKER_PREFIX,
    _STALE_CHECKED_AT,
    LibraryAvailabilityWorker,
    _resolver_fingerprint,
)
from apps.shared.state import db as state_db
from tests.engine_core.availability_test_support import (
    _hold_immediate_until,
    _stable,
    _upsert_track,
    _wait_worker_complete,
)
from tests.engine_core.availability_test_support import (
    data_dir as data_dir,
)
from tests.engine_core.availability_test_support import (
    state_db_path as state_db_path,
)
from tests.waits import THREAD_HANG_GUARD_S

pytestmark = pytest.mark.requirement("LIBM-09")


_WORKER_PATH_MAP_PROBE = """
import sys
import time
from pathlib import Path

from apps.engine_core.library_availability import LibraryAvailabilityWorker
from apps.shared.state import db as state_db

data_dir = Path(sys.argv[1])
stable_id = sys.argv[2]
mapped_audio = sys.argv[3]
guard_s = float(sys.argv[4])
worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
worker.start()
try:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.complete and snapshot.pending == 0:
            break
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"worker still pending={status.pending} phase={status.phase} "
            f"error={status.last_error}"
        )
    status = worker.status()
    if status.last_error is not None or status.present != 1:
        raise SystemExit(f"present={status.present} error={status.last_error}")
finally:
    worker.stop()

conn = state_db.open_rw(data_dir / "state" / "state.db")
try:
    row = conn.execute(
        "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
        (stable_id,),
    ).fetchone()
finally:
    conn.close()
if row != ("present", mapped_audio):
    raise SystemExit(f"row={row!r} expected={mapped_audio!r}")
print("ok")
"""


def test_worker_classifies_mapped_only_track_present_from_its_data_dir(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """The worker must load path-map.json from the data dir it was built with.

    [if] the stored path is missing and only that data dir's map names an
    existing file [then] the production worker records the track present.
    The worker runs in a fresh process whose environment does not carry
    MDT_PATH_MAP or MDT_DATA_DIR.
    """
    stored_root = data_dir / "stored-missing"
    mapped_root = data_dir / "mapped-present"
    mapped_root.mkdir()
    mapped_audio = mapped_root / "track.mp3"
    mapped_audio.write_bytes(b"mapped-audio")
    stored = stored_root / "track.mp3"
    assert not stored.exists()
    map_payload = {
        "entries": [{"from": str(stored_root), "to": str(mapped_root)}],
    }
    (data_dir / "path-map.json").write_text(json.dumps(map_payload), encoding="utf-8")
    stable_id = _stable("mapped")
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(stored))
    finally:
        conn.close()

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MDT_PATH_MAP", "MDT_DATA_DIR"}
    }
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _WORKER_PATH_MAP_PROBE,
            str(data_dir),
            stable_id,
            str(mapped_audio),
            str(THREAD_HANG_GUARD_S),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == "ok"


def test_changed_path_map_reprobes_a_persisted_absent_row(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """A fresh absent row is invisible to the incremental scan.

    [if] no resolver stamp exists and the path map names an existing file
    [then] the worker marks the persisted absent track present.
    [if] the stamp already matches that map [then] the same absent row stays
    absent, including its checked_at.
    """
    stored_root = data_dir / "stored-missing"
    mapped_root = data_dir / "mapped-present"
    mapped_root.mkdir()
    mapped_audio = mapped_root / "track.mp3"
    mapped_audio.write_bytes(b"mapped-audio")
    stored = stored_root / "track.mp3"
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": str(stored_root), "to": str(mapped_root)}]}),
        encoding="utf-8",
    )
    stable_id = _stable("stale-absent")
    fresh = "9999-01-01T00:00:00+00:00"
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(stored))
        conn.execute(
            "INSERT INTO track_availability "
            "(stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
            (stable_id, "absent", str(stored), fresh),
        )
        conn.commit()
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
        row = conn.execute(
            "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        assert row == ("present", str(mapped_audio))
        conn.execute(
            "UPDATE track_availability SET state = 'absent', checked_path = ?, "
            "checked_at = ? WHERE stable_id = ?",
            (str(stored), fresh, stable_id),
        )
        conn.commit()
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
        held = conn.execute(
            "SELECT state, checked_path, checked_at FROM track_availability "
            "WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
    finally:
        conn.close()
    assert held == ("absent", str(stored), fresh)


_WORKER_IDLE_PATH_MAP_PROBE = """
import json
import sys
import time
from pathlib import Path

from apps.engine_core.library_availability import LibraryAvailabilityWorker
from apps.shared.state import db as state_db

data_dir = Path(sys.argv[1])
stable_id = sys.argv[2]
stored = sys.argv[3]
mapped_audio = sys.argv[4]
stored_root = sys.argv[5]
mapped_root = sys.argv[6]
guard_s = float(sys.argv[7])
db_path = data_dir / "state" / "state.db"


def _row() -> tuple[object, ...] | None:
    conn = state_db.open_rw(db_path)
    try:
        return conn.execute(
            "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
    finally:
        conn.close()


worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
worker.start()
try:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        if worker.status().complete and _row() == ("absent", stored):
            break
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"worker never settled absent: phase={status.phase} "
            f"error={status.last_error} row={_row()!r}"
        )
    (data_dir / "path-map.json").write_text("{", encoding="utf-8")
    time.sleep(0.6)
    thread = worker._thread
    if thread is None or not thread.is_alive():
        raise SystemExit("malformed path-map.json killed the idle worker")
    if _row() != ("absent", stored):
        raise SystemExit(f"malformed map changed the row: {_row()!r}")
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": stored_root, "to": mapped_root}]}),
        encoding="utf-8",
    )
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        if _row() == ("present", mapped_audio):
            break
        status = worker.status()
        if status.phase == "failed":
            raise SystemExit(f"idle worker failed after the valid map: {status.last_error}")
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"idle worker left the absent row untouched: phase={status.phase} "
            f"error={status.last_error} row={_row()!r}"
        )
finally:
    worker.stop()
print("ok")
"""


def test_idle_worker_notices_path_map_edit_after_complete(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """A completed worker must notice path-map.json without a new request.

    [if] the worker has reached complete with a track stored absent whose
    file is missing, and that data dir's path-map.json is then written to
    map the stored path onto an existing file [then] the same worker marks
    the row present after an idle poll. A malformed map in between must not
    kill the worker. The worker runs in a fresh process whose environment
    does not carry MDT_PATH_MAP or MDT_DATA_DIR.
    """
    stored_root = data_dir / "stored-missing"
    mapped_root = data_dir / "mapped-present"
    mapped_root.mkdir()
    mapped_audio = mapped_root / "track.mp3"
    mapped_audio.write_bytes(b"mapped-audio")
    stored = stored_root / "track.mp3"
    assert not stored.exists()
    assert not (data_dir / "path-map.json").exists()
    stable_id = _stable("idle-map")
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(stored))
    finally:
        conn.close()

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MDT_PATH_MAP", "MDT_DATA_DIR"}
    }
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(repo_root) + (
        os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else ""
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _WORKER_IDLE_PATH_MAP_PROBE,
            str(data_dir),
            stable_id,
            str(stored),
            str(mapped_audio),
            str(stored_root),
            str(mapped_root),
            str(THREAD_HANG_GUARD_S),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == "ok"


_WORKER_STARTUP_MALFORMED = """
import sys
import time
from pathlib import Path

from apps.engine_core.library_availability import LibraryAvailabilityWorker

data_dir = Path(sys.argv[1])
guard_s = float(sys.argv[2])
worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
worker.start()
try:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        thread = worker._thread
        if snapshot.phase == "failed":
            raise SystemExit(
                f"malformed path map parked the worker: {snapshot.last_error}"
            )
        if (
            snapshot.phase == "queued"
            and snapshot.last_error
            and "malformed" in snapshot.last_error
            and thread is not None
            and thread.is_alive()
        ):
            break
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"drain never queued the malformed map: phase={status.phase} "
            f"error={status.last_error}"
        )
    (data_dir / "path-map.json").write_text('{"entries": []}', encoding="utf-8")
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.phase == "failed":
            raise SystemExit(f"failed after repair: {snapshot.last_error}")
        if snapshot.complete and snapshot.phase == "complete" and snapshot.present == 1:
            break
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"repaired map was not probed: phase={status.phase} "
            f"present={status.present} error={status.last_error}"
        )
finally:
    worker.stop()
print("ok")
"""


_WORKER_OTHER_MACHINE = """
import sys
import time
from pathlib import Path

from apps.engine_core.library_availability import (
    LibraryAvailabilityWorker,
    _RESOLVER_MARKER_PREFIX,
    _resolver_fingerprint,
)
from apps.shared.state import db as state_db

data_dir = Path(sys.argv[1])
state_db_path = Path(sys.argv[2])
stable_id = sys.argv[3]
audio = sys.argv[4]
guard_s = float(sys.argv[5])
original = _resolver_fingerprint(data_dir)
(data_dir / "machine-id").write_text("b" * 32, encoding="utf-8")
if _resolver_fingerprint(data_dir) == original:
    raise SystemExit("fingerprint ignored the machine id")
fresh = "9999-01-01T00:00:00+00:00"
conn = state_db.open_rw(state_db_path)
try:
    conn.execute(
        "INSERT INTO schema_meta_markers(marker, applied_at) VALUES (?, ?)",
        (_RESOLVER_MARKER_PREFIX + original, fresh),
    )
    conn.commit()
finally:
    conn.close()

worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
worker.start()
try:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        verify = state_db.open_ro(state_db_path)
        try:
            row = verify.execute(
                "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
        finally:
            verify.close()
        if row == ("present", audio):
            break
        status = worker.status()
        if status.phase == "failed":
            raise SystemExit(f"worker failed instead of reprobeing: {status.last_error}")
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"other machine's absent verdict was left in place: "
            f"phase={status.phase} error={status.last_error} row={row!r}"
        )
finally:
    worker.stop()
print("ok")
"""


def _resolver_subprocess_env() -> dict[str, str]:
    """Child env with path-map overrides removed, matching the idle probe."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MDT_PATH_MAP", "MDT_DATA_DIR"}
    }
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(repo_root) + (
        os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else ""
    )
    return env


def _run_resolver_subprocess(script: str, args: list[str]) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=repo_root,
        env=_resolver_subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert proc.stdout.strip() == "ok"


def test_startup_drain_retries_a_malformed_path_map(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """A wake already queued must not park the worker when the map is unreadable.

    [if] path-map.json is malformed at startup, so the first drain reads it
    [then] the worker stays queued with the read error and the thread stays alive.
    [if] the file is then replaced with a valid empty map and the audio exists
    [then] the same worker reaches complete with the row present, not failed.
    The worker runs in a fresh process whose environment does not carry
    MDT_PATH_MAP or MDT_DATA_DIR.
    """
    audio = data_dir / "track.mp3"
    audio.write_bytes(b"audio")
    (data_dir / "path-map.json").write_text("{", encoding="utf-8")
    stable_id = _stable("drain-malformed")
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
    finally:
        conn.close()
    _run_resolver_subprocess(
        _WORKER_STARTUP_MALFORMED,
        [str(data_dir), str(THREAD_HANG_GUARD_S)],
    )


def test_restored_db_reprobes_when_the_machine_id_differs(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """A copied state.db must not keep another machine's disk verdict.

    [if] the row is a fresh absent and the stored stamp matches this path
    map, but the machine-id file is a different machine [then] the worker
    reprobes the file that exists here to present.
    [if] the machine id is unchanged [then] the fingerprint stays put, so
    this test fails closed when the stamp ignores machine id. A present-to-
    absent drop is the wrong fixture: one row is a mass-missing refusal.
    The worker runs in a fresh process whose environment does not carry
    MDT_PATH_MAP or MDT_DATA_DIR.
    """
    audio = data_dir / "here.mp3"
    audio.write_bytes(b"audio")
    stable_id = _stable("other-machine")
    fresh = "9999-01-01T00:00:00+00:00"
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
        conn.execute(
            "INSERT INTO track_availability "
            "(stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
            (stable_id, "absent", str(audio), fresh),
        )
        conn.commit()
    finally:
        conn.close()
    _run_resolver_subprocess(
        _WORKER_OTHER_MACHINE,
        [
            str(data_dir),
            str(state_db_path),
            stable_id,
            str(audio),
            str(THREAD_HANG_GUARD_S),
        ],
    )


def _resolver_markers(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT marker FROM schema_meta_markers WHERE marker LIKE ?",
            (_RESOLVER_MARKER_PREFIX + "%",),
        )
    ]


def test_sidecar_resolver_stamp_does_not_bind_a_restored_state_db(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """Restore workflows replace state.db and leave the sidecar behind.

    [if] availability-resolver.json matches the current path map but
    state.db has no resolver marker and a fresh absent row [then] the worker
    reprobes that row to present.
    [if] the in-db marker then matches [then] rewriting the row to a fresh
    absent leaves it absent.
    """
    stored_root = data_dir / "stored-missing"
    mapped_root = data_dir / "mapped-present"
    mapped_root.mkdir()
    mapped_audio = mapped_root / "track.mp3"
    mapped_audio.write_bytes(b"mapped-audio")
    stored = stored_root / "track.mp3"
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": str(stored_root), "to": str(mapped_root)}]}),
        encoding="utf-8",
    )
    stamp_path = data_dir / "state" / "availability-resolver.json"
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(
        json.dumps({"fingerprint": _resolver_fingerprint(data_dir)}) + "\n",
        encoding="utf-8",
    )
    stable_id = _stable("restored-absent")
    fresh = "9999-01-01T00:00:00+00:00"
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(stored))
        conn.execute(
            "INSERT INTO track_availability "
            "(stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
            (stable_id, "absent", str(stored), fresh),
        )
        conn.commit()
        assert _resolver_markers(conn) == []
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
        row = conn.execute(
            "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        assert row == ("present", str(mapped_audio))
        markers = _resolver_markers(conn)
        assert markers == [_RESOLVER_MARKER_PREFIX + _resolver_fingerprint(data_dir)]
        conn.execute(
            "UPDATE track_availability SET state = 'absent', checked_path = ?, "
            "checked_at = ? WHERE stable_id = ?",
            (str(stored), fresh, stable_id),
        )
        conn.commit()
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
        held = conn.execute(
            "SELECT state, checked_path, checked_at FROM track_availability "
            "WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
    finally:
        conn.close()
    assert held == ("absent", str(stored), fresh)
    assert stamp_path.is_file()


def test_resolver_invalidation_stays_queued_under_a_write_lock(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """A busy database must not fail the worker on the stamp reset.

    [if] no resolver stamp exists and another connection holds BEGIN IMMEDIATE
    [then] the worker stays queued, and neither checked_at nor the stamp changes.
    [if] that lock is then released [then] the persisted absent track becomes
    present through the mapped file, without an external request_probe.
    """
    stored_root = data_dir / "stored-missing"
    mapped_root = data_dir / "mapped-present"
    mapped_root.mkdir()
    mapped_audio = mapped_root / "track.mp3"
    mapped_audio.write_bytes(b"mapped-audio")
    stored = stored_root / "track.mp3"
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": str(stored_root), "to": str(mapped_root)}]}),
        encoding="utf-8",
    )
    stable_id = _stable("lock-stamp")
    fresh = "9999-01-01T00:00:00+00:00"
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(stored))
        conn.execute(
            "INSERT INTO track_availability "
            "(stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
            (stable_id, "absent", str(stored), fresh),
        )
        conn.commit()
    finally:
        conn.close()

    stamp_path = data_dir / "state" / "availability-resolver.json"
    reader = state_db.open_ro(state_db_path)
    release = threading.Event()
    holder = _hold_immediate_until(state_db_path, release)
    worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
    try:
        worker.start()
        started = time.monotonic()
        while time.monotonic() - started < 40.0:
            snapshot = worker.status()
            if snapshot.phase == "failed":
                pytest.fail(
                    "resolver invalidation parked the worker in failed: "
                    f"{snapshot.last_error}"
                )
            if (
                snapshot.phase == "queued"
                and snapshot.last_error
                and "lock retries" in snapshot.last_error
            ):
                break
            time.sleep(0.05)
        else:
            pytest.fail(
                "resolver invalidation never exhausted lock retries: "
                f"phase={worker.status().phase} error={worker.status().last_error}"
            )
        assert not stamp_path.exists()
        assert _resolver_markers(reader) == []
        held = reader.execute(
            "SELECT state, checked_at FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        assert held == ("absent", fresh)
    finally:
        release.set()
        holder.join(timeout=2.0)
        reader.close()

    try:
        started = time.monotonic()
        while time.monotonic() - started < 40.0:
            verify = state_db.open_rw(state_db_path)
            try:
                row = verify.execute(
                    "SELECT state, checked_path FROM track_availability "
                    "WHERE stable_id = ?",
                    (stable_id,),
                ).fetchone()
            finally:
                verify.close()
            if row == ("present", str(mapped_audio)):
                break
            if worker.status().phase == "failed":
                pytest.fail(
                    "worker failed after the write lock was released: "
                    f"{worker.status().last_error}"
                )
            time.sleep(0.05)
        else:
            pytest.fail(
                "persisted absent row was not reprobed after the lock cleared: "
                f"phase={worker.status().phase} error={worker.status().last_error} "
                f"row={row!r}"
            )
        assert not stamp_path.exists()
        stamped = state_db.open_ro(state_db_path)
        try:
            assert _resolver_markers(stamped) == [
                _RESOLVER_MARKER_PREFIX + _resolver_fingerprint(data_dir)
            ]
        finally:
            stamped.close()
    finally:
        worker.stop()


_UNREADABLE_MAP_LOG = "could not read path-map.json during drain"


_WORKER_CLASSIFY_MALFORMED = """
import sys
import time
from pathlib import Path

from apps.engine_core.library_availability import LibraryAvailabilityWorker
from apps.shared.state import db as state_db

data_dir = Path(sys.argv[1])
stable_id = sys.argv[2]
audio = sys.argv[3]
flag = Path(sys.argv[4])
guard_s = float(sys.argv[5])
worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
worker.request_probe([stable_id])
worker.start()
try:
    started = time.monotonic()
    while not flag.is_file():
        if time.monotonic() - started > guard_s:
            raise SystemExit("parent never opened the classification window")
        time.sleep(0.01)
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.phase == "failed":
            raise SystemExit(
                f"classification path-map read parked the worker failed: "
                f"{snapshot.last_error}"
            )
        verify = state_db.open_ro(data_dir / "state" / "state.db")
        try:
            row = verify.execute(
                "SELECT state, checked_path FROM track_availability "
                "WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
        finally:
            verify.close()
        if row == ("present", audio):
            break
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"repaired path-map was not classified: phase={status.phase} "
            f"error={status.last_error}"
        )
finally:
    worker.stop()
print("ok")
"""


def _fingerprint_in_env(data_dir: Path, extra: dict[str, str]) -> str:
    """Fingerprint from a fresh interpreter with ``extra`` resolver env."""
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from apps.engine_core.library_availability import _resolver_fingerprint\n"
        "print(_resolver_fingerprint(Path(sys.argv[1])))\n"
    )
    env = _resolver_subprocess_env()
    env.pop("MDT_LIBRARY_MODE", None)
    env.pop("MDT_CRATE_ROOT", None)
    env.update(extra)
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", script, str(data_dir)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout.strip()


def _process_has_file_open(pid: int, path: Path) -> bool:
    """True when ``pid`` has ``path`` open.

    psutil reads the fd table on Linux, macOS, and Windows. ``/proc/<pid>/fd``
    exists only on Linux, so a direct read always missed the open elsewhere
    and the classification retry timed out.
    """
    try:
        opened = psutil.Process(pid).open_files()
    except (psutil.Error, OSError):
        return False
    target_key = os.path.normcase(str(path.resolve()))
    for item in opened:
        raw = item.path.removesuffix(" (deleted)")
        if os.path.normcase(raw) == target_key:
            return True
        try:
            if os.path.normcase(str(Path(raw).resolve())) == target_key:
                return True
        except OSError:
            continue
    return False


def test_fingerprint_includes_library_mode_and_crate_root(data_dir: Path) -> None:
    """Mode and crate root change the stamp without a path-map edit.

    [if] MDT_LIBRARY_MODE or MDT_CRATE_ROOT differs and path-map.json does
    not [then] the fingerprint differs, [else stop].
    [if] neither variable is set [then] a second process with the same
    environment repeats the fingerprint.
    """
    (data_dir / "path-map.json").write_text('{"entries": []}', encoding="utf-8")
    base = _fingerprint_in_env(data_dir, {})
    assert base == _fingerprint_in_env(data_dir, {})
    remote_a = _fingerprint_in_env(
        data_dir,
        {"MDT_LIBRARY_MODE": "remote", "MDT_CRATE_ROOT": "/crates/a"},
    )
    remote_b = _fingerprint_in_env(
        data_dir,
        {"MDT_LIBRARY_MODE": "remote", "MDT_CRATE_ROOT": "/crates/b"},
    )
    assert base != remote_a
    assert remote_a != remote_b


def test_first_empty_map_generation_reprobes_a_fresh_absent_row(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """The first empty-map stamp must not freeze a settled absent row.

    [if] state.db has no resolver marker, path-map.json has no entries, and
    a row is a fresh absent [then] the worker stales that row and rechecks
    it instead of stamping the generation over the old checked_at.
    """
    audio = data_dir / "missing.mp3"
    assert not audio.exists()
    (data_dir / "path-map.json").write_text('{"entries": []}', encoding="utf-8")
    stable_id = _stable("empty-map-first")
    fresh = "9999-01-01T00:00:00+00:00"
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
        conn.execute(
            "INSERT INTO track_availability "
            "(stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
            (stable_id, "absent", str(audio), fresh),
        )
        conn.commit()
        assert _resolver_markers(conn) == []
    finally:
        conn.close()

    worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
    worker.start()
    try:
        started = time.monotonic()
        row: tuple[object, ...] | None = None
        markers: list[str] = []
        while time.monotonic() - started < THREAD_HANG_GUARD_S:
            verify = state_db.open_ro(state_db_path)
            try:
                row = verify.execute(
                    "SELECT state, checked_at FROM track_availability "
                    "WHERE stable_id = ?",
                    (stable_id,),
                ).fetchone()
                markers = _resolver_markers(verify)
            finally:
                verify.close()
            if (
                row is not None
                and row[0] == "absent"
                and row[1] != fresh
                and markers
                == [_RESOLVER_MARKER_PREFIX + _resolver_fingerprint(data_dir)]
            ):
                break
            snapshot = worker.status()
            if snapshot.phase == "failed":
                pytest.fail(snapshot.last_error or "worker failed")
            time.sleep(0.05)
        else:
            pytest.fail(
                "fresh absent row was not rechecked under the first empty map: "
                f"row={row!r} markers={markers!r} phase={worker.status().phase} "
                f"error={worker.status().last_error}"
            )
    finally:
        worker.stop()


def test_classification_path_map_read_stays_queued_and_retries(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """A malformed map at classification must not park the worker failed.

    The matching stamp is seeded. The parent holds the writer lock, waits
    until the worker has opened state.db (the first path-map read already
    returned), truncates the real path-map.json, then releases the lock so
    the classification read inside ``_run_capped_round`` sees the truncated
    file. No production function is replaced.

    [if] that classification read sees a malformed path-map.json [then] the
    worker logs the read and stays alive, not failed, [else stop].
    [if] the file is repaired to the same stamped map [then] a later idle
    wake classifies the pending track without another request_probe.
    """
    audio = data_dir / "track.mp3"
    audio.write_bytes(b"audio")
    map_payload = '{"entries": []}'
    map_path = data_dir / "path-map.json"
    map_path.write_text(map_payload, encoding="utf-8")
    stable_id = _stable("classify-malformed")
    conn = state_db.open_rw(state_db_path)
    try:
        _upsert_track(conn, stable_id, str(audio))
        conn.execute(
            "INSERT INTO schema_meta_markers(marker, applied_at) VALUES (?, ?)",
            (
                _RESOLVER_MARKER_PREFIX + _resolver_fingerprint(data_dir),
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    flag = data_dir / "classification-window.flag"
    repo_root = Path(__file__).resolve().parents[2]
    release = threading.Event()
    holder = _hold_immediate_until(state_db_path, release)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            _WORKER_CLASSIFY_MALFORMED,
            str(data_dir),
            stable_id,
            str(audio),
            str(flag),
            str(THREAD_HANG_GUARD_S),
        ],
        cwd=repo_root,
        env=_resolver_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    err_lines: list[str] = []

    def _read_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            err_lines.append(line)

    reader = threading.Thread(target=_read_err, daemon=True)
    reader.start()
    try:
        opened = time.monotonic()
        while time.monotonic() - opened < THREAD_HANG_GUARD_S:
            if proc.poll() is not None:
                pytest.fail(
                    "worker exited before opening state.db: "
                    + "".join(err_lines)
                )
            if any(_UNREADABLE_MAP_LOG in line for line in err_lines):
                pytest.fail(
                    "path-map read failed before the worker opened state.db: "
                    + "".join(err_lines)
                )
            if _process_has_file_open(proc.pid, state_db_path):
                break
            time.sleep(0.01)
        else:
            pytest.fail(
                "worker never opened state.db after the first path-map read: "
                + "".join(err_lines)
            )
        map_path.write_text("{", encoding="utf-8")
        release.set()
        seen = time.monotonic()
        while time.monotonic() - seen < THREAD_HANG_GUARD_S:
            if any(_UNREADABLE_MAP_LOG in line for line in err_lines):
                break
            if proc.poll() is not None:
                pytest.fail(
                    "worker exited before the classification read: "
                    + "".join(err_lines)
                )
            time.sleep(0.01)
        else:
            pytest.fail(
                "classification read never reported the malformed path map: "
                + "".join(err_lines)
            )
        map_path.write_text(map_payload, encoding="utf-8")
        flag.write_text("open", encoding="utf-8")
        try:
            returncode = proc.wait(timeout=THREAD_HANG_GUARD_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail(
                "worker did not classify the repaired map: " + "".join(err_lines)
            )
        assert proc.stdout is not None
        stdout = proc.stdout.read()
        assert returncode == 0, "".join(err_lines) or stdout
        assert stdout.strip() == "ok"
    finally:
        release.set()
        holder.join(timeout=2.0)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=2.0)


_MAPPED_TRACK_COUNT = 4


def _seed_mapped_present_tracks(
    data_dir: Path, state_db_path: Path
) -> tuple[Path, Path, Path, Path]:
    """Four stored-missing tracks whose first sweep is present via path-map."""
    stored_root = data_dir / "stored-missing"
    present_root = data_dir / "mapped-present"
    missing_root = data_dir / "mapped-missing"
    restored_root = data_dir / "mapped-restored"
    present_root.mkdir()
    missing_root.mkdir()
    restored_root.mkdir()
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(_MAPPED_TRACK_COUNT):
            name = f"{index}.mp3"
            (present_root / name).write_bytes(b"audio")
            (restored_root / name).write_bytes(b"restored-audio")
            stored = stored_root / name
            assert not stored.exists()
            _upsert_track(conn, _stable(f"m{index}"), str(stored))
    finally:
        conn.close()
    (data_dir / "path-map.json").write_text(
        json.dumps({"entries": [{"from": str(stored_root), "to": str(present_root)}]}),
        encoding="utf-8",
    )
    return stored_root, present_root, missing_root, restored_root


_WORKER_REFUSED_MAP_WAKES = """
import json
import sys
import time
from pathlib import Path

from apps.engine_core.library_availability import (
    LibraryAvailabilityWorker,
    _RESOLVER_MARKER_PREFIX,
    _resolver_fingerprint,
)
from apps.shared.state import db as state_db

data_dir = Path(sys.argv[1])
stored_root = Path(sys.argv[2])
present_root = Path(sys.argv[3])
missing_root = Path(sys.argv[4])
restored_root = Path(sys.argv[5])
guard_s = float(sys.argv[6])
map_path = data_dir / "path-map.json"
db_path = data_dir / "state" / "state.db"
track_count = 4


def _write_map(to_root: Path) -> None:
    map_path.write_text(
        json.dumps({"entries": [{"from": str(stored_root), "to": str(to_root)}]}),
        encoding="utf-8",
    )


def _markers() -> list[str]:
    conn = state_db.open_rw(db_path)
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT marker FROM schema_meta_markers WHERE marker LIKE ?",
                (_RESOLVER_MARKER_PREFIX + "%",),
            )
        ]
    finally:
        conn.close()


def _rows() -> list[tuple[object, ...]]:
    conn = state_db.open_rw(db_path)
    try:
        return list(
            conn.execute(
                "SELECT stable_id, state, checked_path FROM track_availability "
                "ORDER BY stable_id"
            )
        )
    finally:
        conn.close()


worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
worker.start()
try:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if (
            snapshot.complete
            and snapshot.pending == 0
            and snapshot.phase == "complete"
            and snapshot.present == track_count
            and snapshot.last_error is None
        ):
            break
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"first sweep never completed present: phase={status.phase} "
            f"pending={status.pending} present={status.present} "
            f"error={status.last_error}"
        )
    rows = _rows()
    if len(rows) != track_count or any(row[1] != "present" for row in rows):
        raise SystemExit(f"first sweep rows were not all present: {rows!r}")
    _write_map(missing_root)
    bad_fingerprint = _resolver_fingerprint(data_dir)
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.phase == "refused":
            break
        if snapshot.phase == "failed":
            raise SystemExit(f"worker failed before refusal: {snapshot.last_error}")
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"bad map never refused: phase={status.phase} "
            f"error={status.last_error} pending={status.pending}"
        )
    expected_marker = _RESOLVER_MARKER_PREFIX + bad_fingerprint
    if _markers() != [expected_marker]:
        raise SystemExit(
            f"refused without committing the bad resolver marker: "
            f"markers={_markers()!r} expected={expected_marker!r}"
        )
    thread = worker._thread
    if thread is None or not thread.is_alive():
        raise SystemExit("worker thread died at refusal")
    _write_map(restored_root)
    restored_fingerprint = _resolver_fingerprint(data_dir)
    if restored_fingerprint == bad_fingerprint:
        raise SystemExit("restored path map did not change the fingerprint")
    restored_paths = {str(restored_root / f"{index}.mp3") for index in range(track_count)}
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.phase == "failed":
            raise SystemExit(f"idle worker failed after the restored map: {snapshot.last_error}")
        rows = _rows()
        if (
            snapshot.phase == "complete"
            and snapshot.pending == 0
            and snapshot.last_error is None
            and len(rows) == track_count
            and all(row[1] == "present" for row in rows)
            and {str(row[2]) for row in rows} == restored_paths
        ):
            break
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"corrected map left the refused sweep stuck: phase={status.phase} "
            f"pending={status.pending} error={status.last_error} rows={_rows()!r}"
        )
    thread = worker._thread
    if thread is None or not thread.is_alive():
        raise SystemExit("worker thread died after the restored map")
finally:
    worker.stop()
print("ok")
"""


_WORKER_REFUSED_MAP_STAYS = """
import json
import sys
import time
from pathlib import Path

from apps.engine_core.library_availability import (
    LibraryAvailabilityWorker,
    _RESOLVER_MARKER_PREFIX,
    _resolver_fingerprint,
)
from apps.shared.state import db as state_db

data_dir = Path(sys.argv[1])
stored_root = Path(sys.argv[2])
present_root = Path(sys.argv[3])
missing_root = Path(sys.argv[4])
guard_s = float(sys.argv[5])
map_path = data_dir / "path-map.json"
db_path = data_dir / "state" / "state.db"
track_count = 4


def _write_map(to_root: Path) -> None:
    map_path.write_text(
        json.dumps({"entries": [{"from": str(stored_root), "to": str(to_root)}]}),
        encoding="utf-8",
    )


def _markers() -> list[str]:
    conn = state_db.open_rw(db_path)
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT marker FROM schema_meta_markers WHERE marker LIKE ?",
                (_RESOLVER_MARKER_PREFIX + "%",),
            )
        ]
    finally:
        conn.close()


def _rows() -> list[tuple[object, ...]]:
    conn = state_db.open_rw(db_path)
    try:
        return list(
            conn.execute(
                "SELECT stable_id, state, checked_path, checked_at "
                "FROM track_availability ORDER BY stable_id"
            )
        )
    finally:
        conn.close()


worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
worker.start()
try:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if (
            snapshot.complete
            and snapshot.pending == 0
            and snapshot.phase == "complete"
            and snapshot.present == track_count
            and snapshot.last_error is None
        ):
            break
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"first sweep never completed present: phase={status.phase} "
            f"pending={status.pending} present={status.present} "
            f"error={status.last_error}"
        )
    _write_map(missing_root)
    bad_fingerprint = _resolver_fingerprint(data_dir)
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.phase == "refused":
            break
        if snapshot.phase == "failed":
            raise SystemExit(f"worker failed before refusal: {snapshot.last_error}")
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"bad map never refused: phase={status.phase} "
            f"error={status.last_error} pending={status.pending}"
        )
    refused = worker.status()
    if not refused.last_error:
        raise SystemExit("refused without last_error")
    markers_before = _markers()
    expected_marker = _RESOLVER_MARKER_PREFIX + bad_fingerprint
    if markers_before != [expected_marker]:
        raise SystemExit(
            f"refused without committing the bad resolver marker: "
            f"markers={markers_before!r} expected={expected_marker!r}"
        )
    rows_before = _rows()
    held_error = refused.last_error
    started = time.monotonic()
    while time.monotonic() - started < 1.25:
        snapshot = worker.status()
        if snapshot.phase != "refused" or snapshot.last_error != held_error:
            raise SystemExit(
                f"unchanged map woke the refused worker: phase={snapshot.phase} "
                f"error={snapshot.last_error!r} held={held_error!r}"
            )
        time.sleep(0.005)
    if _markers() != markers_before:
        raise SystemExit(
            f"unchanged map mutated the resolver marker: "
            f"before={markers_before!r} after={_markers()!r}"
        )
    if _rows() != rows_before:
        raise SystemExit(
            f"unchanged map mutated row state: "
            f"before={rows_before!r} after={_rows()!r}"
        )
finally:
    worker.stop()
print("ok")
"""


def test_idle_worker_wakes_a_refused_sweep_when_the_path_map_changes(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """A refused sweep must wake when path-map.json is corrected.

    [if] the worker committed a path-map change and then refused because
    more than half of the previously present mapped tracks disappeared, and
    path-map.json is then edited to a different valid map that restores
    those files [then] the same idle worker leaves refused and marks every
    seeded row present. The worker runs in a fresh process whose
    environment does not carry MDT_PATH_MAP or MDT_DATA_DIR.
    """
    stored_root, present_root, missing_root, restored_root = _seed_mapped_present_tracks(
        data_dir, state_db_path
    )
    _run_resolver_subprocess(
        _WORKER_REFUSED_MAP_WAKES,
        [
            str(data_dir),
            str(stored_root),
            str(present_root),
            str(missing_root),
            str(restored_root),
            str(THREAD_HANG_GUARD_S),
        ],
    )


def test_idle_worker_stays_refused_when_the_path_map_is_unchanged(
    data_dir: Path,
    state_db_path: Path,
) -> None:
    """An unchanged map after a resolver-induced refusal must stay refused.

    [if] that same refusal is followed by an idle period with path-map.json
    unchanged [then] every status sample stays refused with last_error set
    and the resolver marker and row states do not change. The worker runs
    in a fresh process whose environment does not carry MDT_PATH_MAP or
    MDT_DATA_DIR.
    """
    stored_root, present_root, missing_root, _restored_root = _seed_mapped_present_tracks(
        data_dir, state_db_path
    )
    _run_resolver_subprocess(
        _WORKER_REFUSED_MAP_STAYS,
        [
            str(data_dir),
            str(stored_root),
            str(present_root),
            str(missing_root),
            str(THREAD_HANG_GUARD_S),
        ],
    )


_WORKER_EMPTY_MAP_REFUSAL_HOLDS = """
import os
import sys
import time
from pathlib import Path

import psutil

from apps.engine_core.library_availability import (
    LibraryAvailabilityWorker,
    _RESOLVER_MARKER_PREFIX,
    _STALE_CHECKED_AT,
)
from apps.shared.state import db as state_db

data_dir = Path(sys.argv[1])
db_path = Path(sys.argv[2])
guard_s = float(sys.argv[3])


def _markers() -> list[str]:
    conn = state_db.open_rw(db_path)
    try:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT marker FROM schema_meta_markers WHERE marker LIKE ?",
                (_RESOLVER_MARKER_PREFIX + "%",),
            )
        ]
    finally:
        conn.close()


def _rows() -> list[tuple[object, ...]]:
    conn = state_db.open_rw(db_path)
    try:
        return list(
            conn.execute(
                "SELECT stable_id, state, checked_path, checked_at "
                "FROM track_availability ORDER BY stable_id"
            )
        )
    finally:
        conn.close()


def _db_open(pid: int) -> bool:
    try:
        opened = psutil.Process(pid).open_files()
    except (psutil.Error, OSError):
        return False
    target_key = os.path.normcase(str(db_path.resolve()))
    for item in opened:
        raw = item.path.removesuffix(" (deleted)")
        if os.path.normcase(raw) == target_key:
            return True
        try:
            if os.path.normcase(str(Path(raw).resolve())) == target_key:
                return True
        except OSError:
            continue
    return False


worker = LibraryAvailabilityWorker(data_dir, batch_size=10)
worker.start()
pid = os.getpid()
try:
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        snapshot = worker.status()
        if snapshot.phase == "refused":
            break
        if snapshot.phase == "failed":
            raise SystemExit(f"worker failed before refusal: {snapshot.last_error}")
        time.sleep(0.05)
    else:
        status = worker.status()
        raise SystemExit(
            f"empty-map first drain never refused: phase={status.phase} "
            f"error={status.last_error} pending={status.pending}"
        )
    refused = worker.status()
    if not refused.last_error:
        raise SystemExit("refused without last_error")
    if _markers():
        raise SystemExit(f"empty-map refusal stamped a marker: {_markers()!r}")
    started = time.monotonic()
    while time.monotonic() - started < guard_s:
        if not _db_open(pid):
            break
        time.sleep(0.005)
    else:
        raise SystemExit("state.db stayed open after the refused drain closed")
    rows_before = _rows()
    if not rows_before:
        raise SystemExit("seeded availability rows disappeared at refusal")
    if any(row[3] != _STALE_CHECKED_AT for row in rows_before):
        raise SystemExit(f"refusal rewrote checked_at: {rows_before!r}")
    held_error = refused.last_error
    started = time.monotonic()
    while time.monotonic() - started < 1.25:
        snapshot = worker.status()
        if snapshot.phase != "refused" or snapshot.last_error != held_error:
            raise SystemExit(
                f"unchanged empty map woke the refused worker: phase={snapshot.phase} "
                f"error={snapshot.last_error!r} held={held_error!r}"
            )
        if _db_open(pid):
            raise SystemExit("refused empty-map worker reopened state.db while idle")
        time.sleep(0.005)
    if _markers():
        raise SystemExit(
            f"unchanged empty map stamped a deferred marker: {_markers()!r}"
        )
    if _rows() != rows_before:
        raise SystemExit(
            f"unchanged empty map mutated row bytes: "
            f"before={rows_before!r} after={_rows()!r}"
        )
finally:
    worker.stop()
print("ok")
"""


def test_idle_worker_holds_an_empty_map_refusal_without_a_marker(
    data_dir: Path,
    state_db_path: Path,
    tmp_path: Path,
) -> None:
    """An initial empty-map mass-missing refusal must stay refused.

    [if] state.db has no resolver marker, the path map has no entries, and
    pending present rows trip the mass-missing guard on the first drain
    [then] that sweep is refused, the marker stays absent, row bytes stay
    put, and the same worker does not open state.db again while the map is
    unchanged.
    """
    audio_dir = tmp_path / "empty-map-refuse"
    audio_dir.mkdir()
    (data_dir / "path-map.json").write_text('{"entries": []}', encoding="utf-8")
    paths: list[Path] = []
    conn = state_db.open_rw(state_db_path)
    try:
        for index in range(10):
            audio = audio_dir / f"{index}.mp3"
            audio.write_bytes(b"\\x00")
            paths.append(audio)
            stable_id = _stable(f"e{index}")
            _upsert_track(conn, stable_id, str(audio))
            conn.execute(
                "INSERT INTO track_availability "
                "(stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
                (stable_id, "present", str(audio), _STALE_CHECKED_AT),
            )
        conn.commit()
        assert _resolver_markers(conn) == []
    finally:
        conn.close()
    for audio in paths[:6]:
        audio.unlink()
    _run_resolver_subprocess(
        _WORKER_EMPTY_MAP_REFUSAL_HOLDS,
        [str(data_dir), str(state_db_path), str(THREAD_HANG_GUARD_S)],
    )
