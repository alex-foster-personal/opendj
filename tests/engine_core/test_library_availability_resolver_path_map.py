"""Path-map classification cases for the availability resolver.

[if] a path-map classification or unreadable map applies [then] the worker classifies or stays queued to retry, [else stop].

Split from ``test_library_availability_resolver.py``: path-map.json
classification, startup drain of a malformed map, and retryable unreadable-map reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from apps.engine_core.library_availability import (
    _RESOLVER_MARKER_PREFIX,
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
from tests.engine_core.library_availability_resolver_support import (
    _resolver_subprocess_env,
    _run_resolver_subprocess,
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

    env = {key: value for key, value in os.environ.items() if key not in {"MDT_PATH_MAP", "MDT_DATA_DIR"}}
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
            "INSERT INTO track_availability (stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
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
            "UPDATE track_availability SET state = 'absent', checked_path = ?, checked_at = ? WHERE stable_id = ?",
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
            "SELECT state, checked_path, checked_at FROM track_availability WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
    finally:
        conn.close()
    assert held == ("absent", str(stored), fresh)


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
                pytest.fail("worker exited before opening state.db: " + "".join(err_lines))
            if any(_UNREADABLE_MAP_LOG in line for line in err_lines):
                pytest.fail("path-map read failed before the worker opened state.db: " + "".join(err_lines))
            if _process_has_file_open(proc.pid, state_db_path):
                break
            time.sleep(0.01)
        else:
            pytest.fail("worker never opened state.db after the first path-map read: " + "".join(err_lines))
        map_path.write_text("{", encoding="utf-8")
        release.set()
        seen = time.monotonic()
        while time.monotonic() - seen < THREAD_HANG_GUARD_S:
            if any(_UNREADABLE_MAP_LOG in line for line in err_lines):
                break
            if proc.poll() is not None:
                pytest.fail("worker exited before the classification read: " + "".join(err_lines))
            time.sleep(0.01)
        else:
            pytest.fail("classification read never reported the malformed path map: " + "".join(err_lines))
        map_path.write_text(map_payload, encoding="utf-8")
        flag.write_text("open", encoding="utf-8")
        try:
            returncode = proc.wait(timeout=THREAD_HANG_GUARD_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("worker did not classify the repaired map: " + "".join(err_lines))
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
