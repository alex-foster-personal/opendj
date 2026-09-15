"""``track_availability`` upsert primitives (issue #2790 scoped lookup).

[if] availability rows are upserted for a track [then] scoped lookup returns the stored state, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.availability_write import (
    AvailabilityRow,
    upsert_availability_rows,
)
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("LIBM-09")


def _stable(prefix: str) -> str:
    return (prefix * 40)[:40]


def _upsert_track(conn: sqlite3.Connection, stable_id: str, file_path: str) -> None:
    with StateWriter(conn, actor="test-availability-write") as writer:
        writer.upsert_track(
            stable_id=stable_id,
            stable_id_tier="inferred",
            title=stable_id,
            artists=[],
            album=None,
            isrc=None,
            duration_ms=None,
            file_path=file_path,
        )
    conn.commit()


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    target = tmp_path / "state" / "state.db"
    target.parent.mkdir(parents=True)
    return target


def test_scoped_existing_lookup_matches_full_lookup(
    state_db_path: Path, tmp_path: Path
) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    paths = [str(audio_dir / f"{index}.mp3") for index in range(4)]
    for path in paths:
        Path(path).write_bytes(b"\x00")

    conn = state_db.open_rw(state_db_path)
    try:
        stable_ids = [_stable(f"s{index}") for index in range(4)]
        for stable_id, path in zip(stable_ids, paths, strict=True):
            _upsert_track(conn, stable_id, path)

        seed_rows = [
            AvailabilityRow(stable_id=stable_ids[0], state="present", checked_path=paths[0]),
            AvailabilityRow(stable_id=stable_ids[1], state="absent", checked_path=paths[1]),
        ]
        upsert_availability_rows(conn, seed_rows)
        conn.commit()

        batch = [
            AvailabilityRow(stable_id=stable_ids[0], state="present", checked_path=paths[0]),
            AvailabilityRow(stable_id=stable_ids[2], state="present", checked_path=paths[2]),
            AvailabilityRow(stable_id=stable_ids[3], state="absent", checked_path=paths[3]),
        ]
        conn.execute("SAVEPOINT scoped_lookup")
        full_report = upsert_availability_rows(conn, batch)
        conn.execute("ROLLBACK TO SAVEPOINT scoped_lookup")

        scoped_report = upsert_availability_rows(
            conn,
            batch,
            existing_scope_stable_ids=[row.stable_id for row in batch],
        )
        conn.execute("ROLLBACK TO SAVEPOINT scoped_lookup")

        assert scoped_report.changed == full_report.changed
        assert scoped_report.unchanged == full_report.unchanged
        assert scoped_report.total == full_report.total
        assert scoped_report.counts == full_report.counts
        assert scoped_report.changed_stable_ids == full_report.changed_stable_ids
    finally:
        conn.close()
