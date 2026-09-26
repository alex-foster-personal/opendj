"""Engine boot must migrate an existing state.db before serving (#762).

Live incident: shipping a new build over a library imported by an OLDER
build left state.db at schema_meta version 5 while the build wanted 7.
``SqliteBackend`` reads exclusively through ``open_ro`` (never migrates);
only ``open_rw``'s ``apply_migrations`` side effect does, and nothing on
the boot path called it for an EXISTING db -- so every ``/api/v1/health``
call raised ``sqlite3.OperationalError: no such column: deleted_at`` and
the engine never came up.

Run in a SUBPROCESS, matching test_contract_rev.py / test_data_dir_sandbox.py:
``create_app`` imports the legacy modules, which resolve their paths from
``MDT_DATA_DIR`` at import time.

Single-line intent:
  - if a v5-shaped state.db boots through create_app then /api/v1/health
    serves 200 with the v8 schema applied [broken if migration only runs on
    a write path, per the #762 incident]
  - if concurrent stale-db boots race on the same state.db then all reach the
    current schema without raw SQLite migration errors [broken if only one
    of N contenders survives, per issue #791]
  - if a peer holds the state.db file lock past the request-time busy wait
    then every boot-path open still waits it out and boots [broken if a boot
    pre-check gives up at 5 s, per the Sat 26 Sep 2026 trunk red]
  - if a lock is held past a handle's busy wait then it raises within that
    bound, and an unbounded wait is refused [broken if the fix waits forever]
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.webui.server import sqlite_backend
from tests.test_schema_time_travel import _verified_v5_sql
from tests.webui.pre_v7_state import build_pre_v7_state_db

pytestmark = pytest.mark.requirement("GUARD-09")

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import json
import os
from pathlib import Path

from starlette.testclient import TestClient

from apps.engine_core.app import HEALTH_PATH, create_app
from apps.engine_core.config import EngineConfig

cfg = EngineConfig(data_dir=Path(os.environ["MDT_DATA_DIR"]))
app = create_app(cfg)

# SEC-01 (#2689): this runs in a bare subprocess, so it never imports
# tests/conftest.py's TestClient default -- base_url must be explicit here
# or the daemon host allowlist 403s every request.
with TestClient(app, base_url="http://127.0.0.1") as client:
    health = client.get(HEALTH_PATH)

print(json.dumps({
    "health_status": health.status_code,
    "health_body": health.json(),
}))
"""

# AC4 end to end, with a REAL failure rather than fabricated migration SQL
# (repo policy, AGENTS.md: no monkeypatching or fabricated application
# state in tests). The fixture below makes the v5 db filesystem read-only --
# a genuine failure mode a stale install or a locked/read-only mount can
# actually hit -- so the real, unmodified next migration step's write is
# refused by the OS itself. ``apps.webui.server.app`` builds its module-scope
# ``app`` object at IMPORT time, and that build's own ``make_backend()`` call
# is no longer caught (issue #762 fixed the silent InMemoryBackend swallow
# there too), so the plain, unmodified ``_PROBE`` script's very first import
# line is enough to reproduce this: no custom failing probe script needed.


def _write_v5_state_db(db_path: Path) -> None:
    """Restore the pinned v5 dump (same fixture as test_schema_time_travel)
    and seed one row, so the engine boots against a real, non-empty,
    old-shape library rather than an edge-case-empty one."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_verified_v5_sql())
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path,"
            " created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?)",
            ("v5-boot-fixture", "Old Build Track", "/music/old.mp3", now, now),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    data_dir = tmp_path_factory.mktemp("engine-data")
    _write_v5_state_db(data_dir / "state" / "state.db")

    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "MDT_DATA_DIR": str(data_dir),
            "MDT_LIBRARY_MODE": "local",
        }
    )
    env.pop("WEB_CONCURRENCY", None)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"engine boot-migration probe failed (exit {result.returncode})\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-4000:]}"
        )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    payload["state_db"] = data_dir / "state" / "state.db"
    return payload


def test_engine_boots_and_serves_health_against_a_v5_db(probe: dict) -> None:
    assert probe["health_status"] == 200, (
        f"health call failed against an unmigrated db: {probe['health_body']}"
    )
    assert probe["health_body"]["status"] == "ok"


def test_v5_state_db_lands_on_the_ladder_top_after_boot(probe: dict) -> None:
    """[if] booting over a v5 library leaves schema_meta below the ladder's
    terminal version [then] the migration did not complete, [else stop].

    Pinned to ``state_schema.SCHEMA_VERSION`` rather than to the number that
    happened to be terminal when this was written: a literal here passes the
    day it is written and then silently stops checking that boot reaches the
    TOP the moment a rung is added, which is precisely when it matters.
    """
    conn = sqlite3.connect(f"file:{probe['state_db']}?mode=ro", uri=True)
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_meta"
        ).fetchone()[0]
        # v7 first added the ``deleted_at`` column that the live incident's
        # unmigrated db was missing; querying it proves the shape survives
        # every subsequent migration too, not just the version counter.
        row = conn.execute(
            "SELECT deleted_at FROM tracks WHERE stable_id = ?",
            ("v5-boot-fixture",),
        ).fetchone()
    finally:
        conn.close()
    assert version == state_schema.SCHEMA_VERSION
    assert row == (None,)


def test_boot_aborts_end_to_end_when_migration_cannot_write(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """AC4, partial -- proved through ``create_app`` itself, not just
    ``make_backend``, but see caveat below.

    ``apps.webui.server.app`` builds its own module-scope ``app`` object on
    import (a side effect of ``apps.engine_core.app`` importing names from
    it), and that build's ``make_backend()`` call is uncaught: issue #762
    also removed the swallow-into-InMemoryBackend that used to sit around it,
    so a real migration failure aborts right there, at import, rather than
    quietly serving an empty library. The real failure here is
    ``open_rw``'s own ``PRAGMA journal_mode = WAL`` being unable to create
    ``<db>-wal`` because that exact path is pre-occupied by a directory --
    a real failure (another process already holding that path, a leftover
    directory from a botched install) that, unlike a read-only chmod, no
    privileged (root) process can bypass either: no patched migration
    content, per repo policy.

    Caveat (same as ``tests/webui/test_sqlite_backend.py``'s equivalent):
    this fires before ``apply_migrations`` runs any statement, not inside a
    migration step. v6-v8 only add nullable columns and brand-new tables, so
    no real v5 data makes an actual migration step raise without fabricating
    broken SQL. This proves the boot path aborts on any real write failure
    during migrate-or-open; it does not exercise a step itself raising.
    """
    data_dir = tmp_path_factory.mktemp("engine-data-failing")
    db_path = data_dir / "state" / "state.db"
    _write_v5_state_db(db_path)

    pre_conn = sqlite3.connect(str(db_path))
    try:
        pre_version = pre_conn.execute(
            "SELECT MAX(version) FROM schema_meta"
        ).fetchone()[0]
    finally:
        pre_conn.close()
    assert pre_version == 5

    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "MDT_DATA_DIR": str(data_dir),
            "MDT_LIBRARY_MODE": "local",
        }
    )
    env.pop("WEB_CONCURRENCY", None)

    wal_path = db_path.parent / f"{db_path.name}-wal"
    wal_path.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=180,
        check=False,
    )

    assert result.returncode != 0, (
        f"boot must abort when migration cannot write, not exit 0\n"
        f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-4000:]}"
    )
    assert "unable to open database file" in result.stderr, (
        f"the migration error must surface, not be swallowed\n"
        f"stderr: {result.stderr[-4000:]}"
    )
    wal_path.rmdir()

    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_meta"
        ).fetchone()[0]
    finally:
        conn.close()
    assert version == pre_version, (
        "a real write failure during migration must not commit; the db "
        "should still be sitting at its pre-failure version"
    )


_CONCURRENT_BOOT_WORKER = """
import json
import sys
import time
from pathlib import Path

gate = Path(sys.argv[1])
db_path = Path(sys.argv[2])

while not gate.exists():
    time.sleep(0.005)

from apps.webui.server.sqlite_backend import SqliteBackend, make_backend

backend = make_backend(db_path)
assert isinstance(backend, SqliteBackend), (
    f"expected SqliteBackend, got {type(backend).__name__}"
)
print(json.dumps({"ok": True}))
"""


def _run_concurrent_make_backend(
    db_path: Path,
    *,
    worker_count: int = 3,
    timeout_s: float = 180,
) -> list[subprocess.CompletedProcess[str]]:
    gate = db_path.parent / "start-gate"
    if gate.exists():
        gate.unlink()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)

    processes: list[subprocess.Popen[str]] = [
        subprocess.Popen(
            [sys.executable, "-c", _CONCURRENT_BOOT_WORKER, str(gate), str(db_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
        )
        for _ in range(worker_count)
    ]
    try:
        time.sleep(0.2)
        gate.touch()
        results: list[subprocess.CompletedProcess[str]] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=timeout_s)
            results.append(
                subprocess.CompletedProcess(
                    process.args,
                    process.returncode,
                    stdout,
                    stderr,
                )
            )
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        if gate.exists():
            gate.unlink()


def _assert_concurrent_boot_results(
    results: list[subprocess.CompletedProcess[str]],
    *,
    label: str,
) -> None:
    for index, result in enumerate(results):
        assert result.returncode == 0, (
            f"{label} worker {index} failed (exit {result.returncode})\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-4000:]}"
        )
        combined = f"{result.stdout}\n{result.stderr}".lower()
        assert "duplicate column name" not in combined, (
            f"{label} worker {index} hit duplicate DDL\nstderr: {result.stderr[-4000:]}"
        )
        assert "database is locked" not in combined, (
            f"{label} worker {index} hit database is locked\nstderr: {result.stderr[-4000:]}"
        )


def _inspect_migrated_db(db_path: Path, *, expected_track_count: int) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        version_rows = conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0]
        track_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert version == state_schema.SCHEMA_VERSION
    assert version_rows == state_schema.SCHEMA_VERSION
    assert track_count == expected_track_count
    assert integrity == "ok"


def test_concurrent_make_backend_migrates_stale_db_without_sqlite_race(
    tmp_path: Path,
) -> None:
    """[if] concurrent stale-db boots race on the same state.db [then] all
    reach the current schema without raw SQLite migration errors, [else stop].
    """
    source = tmp_path / "source-v6.db"
    build_pre_v7_state_db(source)

    pre_conn = sqlite3.connect(str(source), isolation_level=None)
    try:
        # Every state.db the app has ever written is WAL: ``open_rw`` pins
        # ``journal_mode = WAL`` and SQLite persists that in the file header,
        # so a real stale db arrives at boot already in WAL. The raw fixture
        # is in rollback mode, and switching to WAL needs an EXCLUSIVE lock
        # that SQLite hands out without consulting the busy handler; N
        # processes doing that first-ever switch at once is a different
        # race (measured 10 of 64 boots on this fixture, none of them inside
        # apply_migrations) than the one this test pins. See
        # .planning/debt/3527.md.
        journal_mode = pre_conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        assert journal_mode == "wal"
        pre_version = pre_conn.execute(
            "SELECT MAX(version) FROM schema_meta"
        ).fetchone()[0]
        expected_track_count = pre_conn.execute(
            "SELECT COUNT(*) FROM tracks"
        ).fetchone()[0]
    finally:
        pre_conn.close()
    assert pre_version == 6
    assert expected_track_count == 1

    shared_db = tmp_path / "shared" / "state.db"
    shared_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, shared_db)

    stale_results = _run_concurrent_make_backend(shared_db)
    _assert_concurrent_boot_results(stale_results, label="stale-db")
    _inspect_migrated_db(shared_db, expected_track_count=expected_track_count)

    current_results = _run_concurrent_make_backend(shared_db)
    _assert_concurrent_boot_results(current_results, label="current-db")


def test_current_db_boot_takes_no_write_lock_behind_a_long_writer(
    tmp_path: Path,
) -> None:
    """[if] a db already at SCHEMA_VERSION is opened while another connection
    holds a long write transaction [then] apply_migrations returns without
    waiting for or failing on the writer, [else stop].

    Codex P2 on PR #3527: an unconditional ``BEGIN IMMEDIATE`` made every
    boot against a CURRENT db contend for the writer lock, so a writer held
    longer than ``busy_timeout`` turned ordinary boots into a raw
    ``database is locked``. The contender below sets ``busy_timeout = 0`` so
    any write-lock attempt fails instantly instead of hiding behind the
    five-second wait.
    """
    db_path = tmp_path / "state.db"
    build_pre_v7_state_db(db_path)
    state_db.open_rw(db_path).close()

    holder = state_db.open_rw(db_path)
    contender = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        contender.execute("PRAGMA busy_timeout = 0")
        assert state_schema.apply_migrations(contender) == state_schema.SCHEMA_VERSION
        assert not contender.in_transaction
    finally:
        contender.close()
        holder.execute("ROLLBACK")
        holder.close()


# A peer that holds the state.db FILE lock EXCLUSIVE, the lock a closing last
# connection holds while it checkpoints and deletes -wal/-shm. In WAL mode that
# (and wal-index recovery) is the only thing that blocks a reader, and it is
# what a concurrent boot met on the Sat 26 Sep 2026 trunk red. A real SQLite
# connection in EXCLUSIVE locking mode takes it; nothing is emulated.
_EXCLUSIVE_LOCK_HOLDER = """
import sqlite3
import sys
import time

conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA locking_mode = EXCLUSIVE")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE schema_meta SET applied_at = applied_at WHERE version = 1")
conn.execute("COMMIT")
print("held", flush=True)
time.sleep(float(sys.argv[2]))
conn.close()
"""


def _hold_exclusive_lock(db_path: Path, hold_s: float) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [sys.executable, "-c", _EXCLUSIVE_LOCK_HOLDER, str(db_path), str(hold_s)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    line = holder.stdout.readline().strip()
    if line != "held":
        holder.kill()
        raise AssertionError(f"lock holder never took the lock: {holder.communicate()}")
    return holder


def _current_wal_state_db(db_path: Path) -> Path:
    build_pre_v7_state_db(db_path)
    state_db.open_rw(db_path).close()
    return db_path


def _timed_under_peer_lock(
    db_path: Path, hold_s: float, call: Callable[[Path], object],
) -> float:
    holder = _hold_exclusive_lock(db_path, hold_s)
    try:
        started = time.monotonic()
        call(db_path)
        return time.monotonic() - started
    finally:
        holder.communicate(timeout=hold_s + 30)


def test_every_boot_open_waits_out_a_peer_lock_past_the_request_wait(
    tmp_path: Path,
) -> None:
    """[if] a peer holds the state.db file lock past the request-time busy
    wait while a boot opens the db [then] each boot-path open waits it out
    and the boot succeeds, [else stop].

    Before the fix each of these opens inherited Python's implicit 5 s and
    raised ``database is locked``. Every site runs against its own db and its
    own holder, concurrently, so the whole test costs one hold.
    """
    hold_s = state_db.DEFAULT_BUSY_TIMEOUT_S + 1.5
    assert hold_s < state_db.BOOT_BUSY_TIMEOUT_S, (
        "the hold must sit between the two bounds to tell them apart"
    )
    sites: dict[str, Callable[[Path], object]] = {
        "make_backend": sqlite_backend.make_backend,
        "missing-schema-meta pre-check": (
            sqlite_backend._tracks_table_missing_schema_meta
        ),
        "migrate-before-serving open_rw": sqlite_backend._migrate_before_serving,
        "SqliteBackend stale-schema pre-check": sqlite_backend.SqliteBackend,
    }
    dbs = {
        name: _current_wal_state_db(tmp_path / f"site-{index}" / "state.db")
        for index, name in enumerate(sites)
    }
    with ThreadPoolExecutor(max_workers=len(sites)) as pool:
        futures = {
            name: pool.submit(_timed_under_peer_lock, dbs[name], hold_s, call)
            for name, call in sites.items()
        }
        waited = {name: future.result() for name, future in futures.items()}

    for name, elapsed in waited.items():
        # Presence, not absence: the open really was blocked past the old
        # bound, so a pass here cannot come from a holder that never held.
        assert elapsed >= state_db.DEFAULT_BUSY_TIMEOUT_S, (
            f"{name} returned after {elapsed:.2f}s; the peer lock was not in "
            "force, so this run measured nothing"
        )


@pytest.mark.parametrize(
    "open_with_bound",
    [
        pytest.param(
            lambda path, bound: sqlite_backend.read_tracks_schema_version(
                path, busy_timeout_s=bound,
            ),
            id="read_tracks_schema_version",
        ),
        pytest.param(
            lambda path, bound: state_db.open_rw(path, busy_timeout_s=bound).close(),
            id="open_rw",
        ),
        pytest.param(
            lambda path, bound: state_db.open_ro(path, busy_timeout_s=bound)
            .execute("SELECT COUNT(*) FROM sqlite_master")
            .fetchone(),
            id="open_ro",
        ),
    ],
)
def test_a_peer_lock_held_past_the_bound_raises_within_it(
    tmp_path: Path, open_with_bound: Callable[[Path, float], object],
) -> None:
    """[if] a peer holds the lock past a handle's busy wait [then] the open
    raises ``database is locked`` no sooner than the bound and no later than
    the bound plus slack, [else stop]. Locked is an error, never "not stale".
    """
    bound_s, slack_s = 0.5, 2.0
    db_path = _current_wal_state_db(tmp_path / "state.db")
    holder = _hold_exclusive_lock(db_path, bound_s + slack_s + 3)
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            open_with_bound(db_path, bound_s)
        elapsed = time.monotonic() - started
    finally:
        holder.kill()
        holder.communicate(timeout=30)
    assert bound_s <= elapsed < bound_s + slack_s, (
        f"raised after {elapsed:.2f}s against a {bound_s}s bound"
    )


@pytest.mark.parametrize("bound", [math.inf, math.nan, 0.0, -1.0])
def test_an_unbounded_or_zero_busy_wait_is_refused(tmp_path: Path, bound: float) -> None:
    """[if] a handle is asked to wait forever, or not at all [then] it
    refuses before opening, [else stop]."""
    db_path = _current_wal_state_db(tmp_path / "state.db")
    for opener in (state_db.open_rw, state_db.open_ro):
        with pytest.raises(ValueError, match="busy_timeout_s"):
            opener(db_path, busy_timeout_s=bound)
    assert math.isfinite(state_db.BOOT_BUSY_TIMEOUT_S)
    assert state_db.BOOT_BUSY_TIMEOUT_S > state_db.DEFAULT_BUSY_TIMEOUT_S


@pytest.mark.parametrize(
    "bound", [0.5, state_db.DEFAULT_BUSY_TIMEOUT_S, state_db.BOOT_BUSY_TIMEOUT_S],
)
def test_each_handle_carries_the_bound_it_was_asked_for(
    tmp_path: Path, bound: float,
) -> None:
    """[if] a handle is opened with a busy wait [then] SQLite reports that
    exact wait for every later statement, [else stop]. The first statement
    alone cannot show this: a later PRAGMA could shorten the wait after it."""
    db_path = _current_wal_state_db(tmp_path / "state.db")
    for opener in (state_db.open_rw, state_db.open_ro):
        conn = opener(db_path, busy_timeout_s=bound)
        try:
            effective_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            conn.close()
        assert effective_ms == round(bound * 1000), opener.__name__
