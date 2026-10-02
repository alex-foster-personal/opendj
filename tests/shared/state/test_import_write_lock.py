"""STATE-15: an import takes the state.db writer lock up front, so a concurrent commit cannot fail it.

The engine keeps writing state.db while a setup import worker runs (agents-md
marker, analysis queue batches, availability rows). A transaction opened with
a bare ``SAVEPOINT`` is DEFERRED: its first read pins a WAL snapshot and its
first write must upgrade that snapshot to the writer lock. If any other
connection committed in between, SQLite answers SQLITE_BUSY_SNAPSHOT at once.
The busy handler is never consulted for that code, so ``busy_timeout`` cannot
ride it out, and the import dies with ``database is locked``. That is the
trunk flake of Thu 1 Oct 2026 (run 36902234879: ``register_machine`` inside
the folder worker's first ``upsert_track``).

Each test injects one real commit from a second connection at the exact
point the race needs: after the importer's first read, before its first
write (``ensure_local_machine`` sits between the two). With the lock taken up
front the second connection is the one that waits, and its write lands once
the import commits.

Single-line intent:
  - [if] a commit between an import's first read and first write fails the import [then] fail, [else stop].
  - [if] the import does not hold the writer lock while it runs [then] fail, [else stop].
  - [if] the blocked concurrent write is lost rather than landing after the import [then] fail, [else stop].
  - [if] a caller-owned transaction around the import is committed or nested-BEGIN'd [then] fail, [else stop].
"""

from __future__ import annotations

import sqlite3
import struct
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.shared.state.ingest import folder, folder_rescan
from apps.shared.state.ingest import rekordbox as rb_ingest
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("STATE-15")

TRACKS = 3
#: Short on purpose: the injected writer runs on the importer's own thread,
#: so with the fix in place it must give up rather than wait out the import.
CONCURRENT_BUSY_TIMEOUT_S = 0.05
MARKER = "test-concurrent-engine-write"


@dataclass
class ConcurrentCommit:
    """One write from a second connection, injected mid-import."""

    state_path: Path
    attempts: int = 0
    blocked: list[str] = field(default_factory=list)

    def write(self, busy_timeout_s: float = CONCURRENT_BUSY_TIMEOUT_S) -> None:
        conn = state_db.open_rw(self.state_path, apply_schema=False, busy_timeout_s=busy_timeout_s)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta_markers(marker, applied_at) VALUES (?, ?)",
                (MARKER, "2026-10-01T00:00:00+00:00"),
            )
        finally:
            conn.close()

    def inject_once(self) -> None:
        self.attempts += 1
        if self.attempts > 1:
            return
        try:
            self.write()
        except sqlite3.OperationalError as exc:
            if not state_db.is_sqlite_busy(exc):
                raise
            self.blocked.append(getattr(exc, "sqlite_errorname", str(exc)))


# ----- helpers ----------------------------------------------------------------
def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = 2205
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))


def _music(tmp_path: Path) -> Path:
    root = tmp_path / "music"
    for index in range(TRACKS):
        _write_wav(root / f"track-{index}.wav")
    return root


def _state_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "state" / "state.db"
    path.parent.mkdir(parents=True)
    state_db.open_rw(path).close()
    return path


def _inject_before_first_write(
    monkeypatch: pytest.MonkeyPatch, commit: ConcurrentCommit
) -> None:
    """Commit from a second connection between the importer's first read and first write."""
    original: Callable[[sqlite3.Connection], str] = sync_stamp.ensure_local_machine

    def ensure_after_concurrent_commit(conn: sqlite3.Connection) -> str:
        commit.inject_once()
        return original(conn)

    monkeypatch.setattr(sync_stamp, "ensure_local_machine", ensure_after_concurrent_commit)


def _scalar(state_path: Path, sql: str) -> int:
    conn = state_db.open_rw(state_path, apply_schema=False)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def _assert_import_won_and_engine_write_landed(
    state_path: Path, commit: ConcurrentCommit, tracks: int = TRACKS
) -> None:
    assert commit.attempts >= 1, "the injection point was never reached, so nothing was tested"
    assert commit.blocked, (
        "the concurrent commit went through mid-import: the importer was not holding "
        "the writer lock, so it was exposed to SQLITE_BUSY_SNAPSHOT"
    )
    assert tracks > 0
    assert _scalar(state_path, "SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL") == tracks
    commit.write(busy_timeout_s=state_db.DEFAULT_BUSY_TIMEOUT_S)
    assert _scalar(state_path, f"SELECT COUNT(*) FROM schema_meta_markers WHERE marker = '{MARKER}'") == 1


# ----- the race, per import path ---------------------------------------------
def test_a_folder_import_survives_a_commit_between_its_first_read_and_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state_path = _music(tmp_path), _state_path(tmp_path)
    commit = ConcurrentCommit(state_path)
    _inject_before_first_write(monkeypatch, commit)

    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="test-write-lock")
    try:
        report = folder.ingest_folder(writer, [root], dry_run=False)
    finally:
        writer.close()
        conn.close()

    assert report.tracks_inserted == TRACKS, report
    _assert_import_won_and_engine_write_landed(state_path, commit)


def test_a_folder_rescan_survives_a_commit_between_its_first_read_and_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state_path = _music(tmp_path), _state_path(tmp_path)
    commit = ConcurrentCommit(state_path)
    _inject_before_first_write(monkeypatch, commit)

    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="test-write-lock")
    try:
        report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    finally:
        writer.close()
        conn.close()

    assert report.tracks_added == TRACKS, report
    _assert_import_won_and_engine_write_landed(state_path, commit)


def test_a_rekordbox_import_survives_a_commit_between_its_first_read_and_first_write(
    tmp_path: Path, tmp_rb_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = _state_path(tmp_path)
    commit = ConcurrentCommit(state_path)
    _inject_before_first_write(monkeypatch, commit)

    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="test-write-lock")
    try:
        report = rb_ingest.ingest_rb(writer, tmp_rb_db, dry_run=False)
    finally:
        writer.close()
        conn.close()

    _assert_import_won_and_engine_write_landed(state_path, commit, tracks=report.tracks_inserted)


def test_a_standalone_writer_call_survives_a_commit_between_its_first_read_and_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state_path = _music(tmp_path), _state_path(tmp_path)
    commit = ConcurrentCommit(state_path)
    _inject_before_first_write(monkeypatch, commit)

    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="test-write-lock")
    try:
        for index, wav in enumerate(sorted(root.iterdir())):
            writer.upsert_track(
                stable_id=f"{index:040d}",
                stable_id_tier="inferred",
                title=wav.stem,
                artists=[],
                album=None,
                isrc=None,
                duration_ms=50,
                file_path=str(wav),
                content_hash=None,
            )
    finally:
        writer.close()
        conn.close()

    _assert_import_won_and_engine_write_landed(state_path, commit)


# ----- the overshoot control ---------------------------------------------------
def test_an_import_inside_a_caller_owned_transaction_neither_nests_begin_nor_commits_it(
    tmp_path: Path,
) -> None:
    """Taking the lock up front must not hijack a transaction the caller already opened."""
    root, state_path = _music(tmp_path), _state_path(tmp_path)

    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, actor="test-write-lock")
    try:
        conn.execute("BEGIN IMMEDIATE")
        report = folder.ingest_folder(writer, [root], dry_run=False)
        assert conn.in_transaction, "the import committed a transaction it did not open"
        conn.execute("ROLLBACK")
    finally:
        writer.close()
        conn.close()

    assert report.tracks_inserted == TRACKS, report
    assert _scalar(state_path, "SELECT COUNT(*) FROM tracks") == 0
