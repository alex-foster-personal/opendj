"""Idle refusal-hold cases for the availability path-map resolver.

[if] a refused sweep meets an unchanged or empty path map [then] the worker stays refused, [else stop].

Split from ``test_library_availability_resolver.py``: a refused sweep that
stays refused on an unchanged map, and an empty-map refusal without a marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.engine_core.library_availability import _STALE_CHECKED_AT
from apps.shared.state import db as state_db
from tests.engine_core.availability_test_support import (
    _stable,
    _upsert_track,
)
from tests.engine_core.library_availability_resolver_support import (
    _resolver_markers,
    _run_resolver_subprocess,
    _seed_mapped_present_tracks,
)
from tests.waits import THREAD_HANG_GUARD_S

pytestmark = pytest.mark.requirement("LIBM-09")


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
    stored_root, present_root, missing_root, _restored_root = _seed_mapped_present_tracks(data_dir, state_db_path)
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
                "INSERT INTO track_availability (stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
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
