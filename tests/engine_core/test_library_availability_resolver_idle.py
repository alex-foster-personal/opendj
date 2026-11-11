"""Idle path-map reprobe cases for the availability worker.

[if] an idle worker sees a path-map edit or a refused sweep should wake [then] the worker re-probes, [else stop].

Split from ``test_library_availability_resolver.py``: an idle worker noticing
a path-map edit, and a refused sweep waking when the map changes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from tests.engine_core.availability_test_support import (
    _stable,
    _upsert_track,
)
from tests.engine_core.library_availability_resolver_support import (
    _run_resolver_subprocess,
    _seed_mapped_present_tracks,
)
from tests.waits import THREAD_HANG_GUARD_S

pytestmark = pytest.mark.requirement("LIBM-09")


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

    env = {key: value for key, value in os.environ.items() if key not in {"MDT_PATH_MAP", "MDT_DATA_DIR"}}
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(repo_root) + (os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else "")
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
    stored_root, present_root, missing_root, restored_root = _seed_mapped_present_tracks(data_dir, state_db_path)
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
