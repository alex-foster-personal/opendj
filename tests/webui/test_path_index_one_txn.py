"""STATE-20 path index batch writes.

[if] upsert_rows writes a batch [then] it lands as ONE WAL commit, not one per row, [else stop].
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.server.rb_vendor_pkg import path_index

pytestmark = pytest.mark.requirement("STATE-20")

NS = "ns-txn"


def _wal_commit_frames(db_path: Path) -> int:
    """Commit frames in the WAL: frame header bytes 4..8 are non-zero on a commit."""
    wal = db_path.with_name(db_path.name + "-wal")
    data = wal.read_bytes() if wal.exists() else b""
    if len(data) < 32:
        return 0
    page_size = struct.unpack(">I", data[8:12])[0]
    frame = 24 + page_size
    return sum(
        1
        for off in range(32, len(data) - frame + 1, frame)
        if struct.unpack(">I", data[off + 4 : off + 8])[0] != 0
    )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    db_path = tmp_path / "state" / "state.db"
    conn = state_db.open_rw(db_path, apply_schema=True)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    try:
        yield db_path, conn
    finally:
        conn.close()


def _rows(n: int) -> list[tuple[str, int | None]]:
    return [(f"/Music/{i:05d}.mp3", None) for i in range(n)]


def test_batch_is_one_commit(db) -> None:
    """[if] 200 new paths are upserted [then] the WAL gains exactly one commit, [else stop]."""
    db_path, conn = db
    before = _wal_commit_frames(db_path)
    path_index.upsert_rows(conn, NS, _rows(200))
    assert _wal_commit_frames(db_path) - before == 1
    assert conn.execute("SELECT count(*) FROM path_availability").fetchone()[0] == 200


def test_caller_transaction_is_not_committed(db) -> None:
    """[if] the caller holds a transaction [then] upsert_rows neither commits nor ends it, [else stop]."""
    _db_path, conn = db
    conn.execute("BEGIN")
    path_index.upsert_rows(conn, NS, _rows(5))
    assert conn.in_transaction
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT count(*) FROM path_availability").fetchone()[0] == 0


def test_failed_batch_leaves_no_partial_rows(db) -> None:
    """[if] a row in the batch fails [then] no row of that batch is kept, [else stop]."""
    _db_path, conn = db
    rows = [*_rows(3), (None, None)]  # logical_path NOT NULL
    with pytest.raises(sqlite3.IntegrityError):
        path_index.upsert_rows(conn, NS, rows)  # type: ignore[arg-type]
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM path_availability").fetchone()[0] == 0
