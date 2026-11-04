"""The machine-local cloud offload ledger (OFFLOAD, spec e8da14b8 section 4).

``<data_dir>/state/offload.db`` records, per track, which offload state it is
in and, per file, where the bytes lived, their digest, and whether R2 was
verified to hold them. It is deliberately NOT a synced table: free disk is a
property of this disk, so "offloaded here" means nothing on another machine.
Other machines learn of the R2 copy through the synced ``track_locations``
``kind='remote'`` row, exactly as hydration writes it.

Every write commits immediately, so a crash at any step leaves a row that
names the step. Resume reads the rows back: a file with ``verified_at`` set
is not uploaded again, a file with ``deleted_at`` set is not deleted again.

-Claude
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

OffloadState = Literal["local", "uploading", "offloaded", "restoring", "temporary"]
RestoreMode = Literal["temporary", "permanent"]
FileRole = Literal["audio", "stem"]

OFFLOAD_STATES: tuple[str, ...] = get_args(OffloadState)
RESTORE_MODES: tuple[str, ...] = get_args(RestoreMode)
LEDGER_FILENAME: str = "offload.db"

_SCHEMA: str = f"""
CREATE TABLE IF NOT EXISTS offload_tracks (
    stable_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ({", ".join(repr(s) for s in OFFLOAD_STATES)})),
    restore_mode TEXT CHECK (restore_mode IS NULL OR restore_mode IN ('temporary', 'permanent')),
    offloaded_at TEXT,
    restored_at TEXT,
    expires_at TEXT,
    last_played_at TEXT,
    bytes_offloaded INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offload_files (
    stable_id TEXT NOT NULL REFERENCES offload_tracks(stable_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('audio', 'stem')),
    local_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    object_key TEXT NOT NULL,
    verified_by TEXT,
    verified_at TEXT,
    deleted_at TEXT,
    PRIMARY KEY (stable_id, local_path)
);
"""


class OffloadLedgerError(RuntimeError):
    """The ledger is missing, corrupt, or asked for an impossible transition."""


@dataclass(frozen=True)
class OffloadFile:
    stable_id: str
    role: FileRole
    local_path: Path
    sha256: str
    size_bytes: int
    object_key: str
    verified_by: str | None
    verified_at: str | None
    deleted_at: str | None


@dataclass(frozen=True)
class TrackOffload:
    stable_id: str
    state: OffloadState
    restore_mode: RestoreMode | None
    offloaded_at: str | None
    restored_at: str | None
    expires_at: str | None
    last_played_at: str | None
    bytes_offloaded: int
    error: str | None
    updated_at: str

    def wire(self) -> dict[str, object]:
        return {
            "state": self.state,
            "restore_mode": self.restore_mode,
            "offloaded_at": self.offloaded_at,
            "restored_at": self.restored_at,
            "expires_at": self.expires_at,
            "last_played_at": self.last_played_at,
            "bytes_offloaded": self.bytes_offloaded,
            "error": self.error,
        }


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def ledger_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / LEDGER_FILENAME


@contextmanager
def open_ledger(data_dir: Path) -> Iterator[sqlite3.Connection]:
    """Open (creating if absent) the ledger in autocommit mode with FKs on."""
    path = ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        yield conn
    finally:
        conn.close()


# --- reads ------------------------------------------------------------------

_TRACK_COLUMNS = (
    "stable_id, state, restore_mode, offloaded_at, restored_at, expires_at, "
    "last_played_at, bytes_offloaded, error, updated_at"
)
_FILE_COLUMNS = (
    "stable_id, role, local_path, sha256, size_bytes, object_key, verified_by, "
    "verified_at, deleted_at"
)


def _track(row: tuple) -> TrackOffload:
    return TrackOffload(*row)


def _file(row: tuple) -> OffloadFile:
    return OffloadFile(row[0], row[1], Path(row[2]), *row[3:])


def get_track(conn: sqlite3.Connection, stable_id: str) -> TrackOffload | None:
    row = conn.execute(
        f"SELECT {_TRACK_COLUMNS} FROM offload_tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    return _track(row) if row is not None else None


def get_tracks(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> dict[str, TrackOffload]:
    out: dict[str, TrackOffload] = {}
    ids = list(dict.fromkeys(stable_ids))
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        marks = ",".join("?" * len(batch))
        for row in conn.execute(
            f"SELECT {_TRACK_COLUMNS} FROM offload_tracks WHERE stable_id IN ({marks})", batch
        ):
            out[row[0]] = _track(row)
    return out


def tracks_in_state(conn: sqlite3.Connection, *states: OffloadState) -> list[TrackOffload]:
    marks = ",".join("?" * len(states))
    rows = conn.execute(
        f"SELECT {_TRACK_COLUMNS} FROM offload_tracks WHERE state IN ({marks}) ORDER BY stable_id",
        states,
    ).fetchall()
    return [_track(row) for row in rows]


def files_for(conn: sqlite3.Connection, stable_id: str) -> list[OffloadFile]:
    rows = conn.execute(
        f"SELECT {_FILE_COLUMNS} FROM offload_files WHERE stable_id = ? ORDER BY role, local_path",
        (stable_id,),
    ).fetchall()
    return [_file(row) for row in rows]


def summary(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {state: 0 for state in OFFLOAD_STATES}
    for state, count in conn.execute("SELECT state, COUNT(*) FROM offload_tracks GROUP BY state"):
        counts[str(state)] = int(count)
    freed = conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) FROM offload_files WHERE deleted_at IS NOT NULL"
    ).fetchone()[0]
    return {**counts, "bytes_freed": int(freed)}


# --- writes -----------------------------------------------------------------


def set_state(
    conn: sqlite3.Connection,
    stable_id: str,
    state: OffloadState,
    *,
    now: datetime,
    **fields: object,
) -> None:
    """Upsert the track row into ``state``; ``fields`` name other columns to set."""
    if state not in OFFLOAD_STATES:
        raise OffloadLedgerError(f"unknown offload state {state!r}")
    allowed = {"restore_mode", "offloaded_at", "restored_at", "expires_at",
               "last_played_at", "bytes_offloaded", "error"}
    unknown = set(fields) - allowed
    if unknown:
        raise OffloadLedgerError(f"unknown ledger columns {sorted(unknown)}")
    columns = ["state", "updated_at", *fields]
    values = [state, iso(now), *fields.values()]
    assignments = ", ".join(f"{column} = excluded.{column}" for column in columns)
    conn.execute(
        f"INSERT INTO offload_tracks (stable_id, {', '.join(columns)}) "
        f"VALUES (?, {', '.join('?' * len(columns))}) "
        f"ON CONFLICT(stable_id) DO UPDATE SET {assignments}",
        (stable_id, *values),
    )


def record_file(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    role: FileRole,
    local_path: Path,
    sha256: str,
    size_bytes: int,
    object_key: str,
) -> None:
    """Register (or re-register with new bytes) one file; a new digest clears its proofs."""
    conn.execute(
        "INSERT INTO offload_files (stable_id, role, local_path, sha256, size_bytes, object_key) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(stable_id, local_path) DO UPDATE SET "
        "role = excluded.role, size_bytes = excluded.size_bytes, object_key = excluded.object_key, "
        "verified_by = CASE WHEN sha256 = excluded.sha256 THEN verified_by END, "
        "verified_at = CASE WHEN sha256 = excluded.sha256 THEN verified_at END, "
        "sha256 = excluded.sha256, deleted_at = NULL",
        (stable_id, role, str(local_path), sha256, size_bytes, object_key),
    )


def mark_verified(conn: sqlite3.Connection, stable_id: str, local_path: Path, how: str, now: datetime) -> None:
    conn.execute(
        "UPDATE offload_files SET verified_by = ?, verified_at = ? WHERE stable_id = ? AND local_path = ?",
        (how, iso(now), stable_id, str(local_path)),
    )


def mark_deleted(conn: sqlite3.Connection, stable_id: str, local_path: Path, now: datetime) -> None:
    conn.execute(
        "UPDATE offload_files SET deleted_at = ? WHERE stable_id = ? AND local_path = ? "
        "AND verified_at IS NOT NULL",
        (iso(now), stable_id, str(local_path)),
    )


def mark_restored(conn: sqlite3.Connection, stable_id: str, local_path: Path) -> None:
    conn.execute(
        "UPDATE offload_files SET deleted_at = NULL WHERE stable_id = ? AND local_path = ?",
        (stable_id, str(local_path)),
    )


def forget_files(conn: sqlite3.Connection, stable_id: str) -> None:
    conn.execute("DELETE FROM offload_files WHERE stable_id = ?", (stable_id,))


__all__ = [
    "LEDGER_FILENAME",
    "OFFLOAD_STATES",
    "RESTORE_MODES",
    "FileRole",
    "OffloadFile",
    "OffloadLedgerError",
    "OffloadState",
    "RestoreMode",
    "TrackOffload",
    "files_for",
    "forget_files",
    "get_track",
    "get_tracks",
    "iso",
    "ledger_path",
    "mark_deleted",
    "mark_restored",
    "mark_verified",
    "open_ledger",
    "parse_iso",
    "record_file",
    "set_state",
    "summary",
    "tracks_in_state",
    "utc_now",
]
