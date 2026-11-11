"""Resolver-stamp and restored-database cases for the availability worker.

[if] a resolver-stamp invalidation or restored database applies [then] the worker re-probes or stays queued, [else stop].

Split from ``test_library_availability_resolver.py``: resolver-stamp
invalidation, a restored state.db, fingerprint env, and first empty-map generation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

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
    _resolver_markers,
    _resolver_subprocess_env,
    _run_resolver_subprocess,
)
from tests.waits import THREAD_HANG_GUARD_S

pytestmark = pytest.mark.requirement("LIBM-09")


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
            "INSERT INTO track_availability (stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
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
            "INSERT INTO track_availability (stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
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
            "INSERT INTO track_availability (stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
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
                pytest.fail(f"resolver invalidation parked the worker in failed: {snapshot.last_error}")
            if snapshot.phase == "queued" and snapshot.last_error and "lock retries" in snapshot.last_error:
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
                    "SELECT state, checked_path FROM track_availability WHERE stable_id = ?",
                    (stable_id,),
                ).fetchone()
            finally:
                verify.close()
            if row == ("present", str(mapped_audio)):
                break
            if worker.status().phase == "failed":
                pytest.fail(f"worker failed after the write lock was released: {worker.status().last_error}")
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
            assert _resolver_markers(stamped) == [_RESOLVER_MARKER_PREFIX + _resolver_fingerprint(data_dir)]
        finally:
            stamped.close()
    finally:
        worker.stop()


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
            "INSERT INTO track_availability (stable_id, state, checked_path, checked_at) VALUES (?, ?, ?, ?)",
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
                    "SELECT state, checked_at FROM track_availability WHERE stable_id = ?",
                    (stable_id,),
                ).fetchone()
                markers = _resolver_markers(verify)
            finally:
                verify.close()
            if (
                row is not None
                and row[0] == "absent"
                and row[1] != fresh
                and markers == [_RESOLVER_MARKER_PREFIX + _resolver_fingerprint(data_dir)]
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
