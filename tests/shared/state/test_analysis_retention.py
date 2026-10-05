"""STATE-17: superseded own-analysis versions are pruned; the current one always survives."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.analysis import retention

pytestmark = pytest.mark.requirement("STATE-17")

Key = tuple[str, str, str]

_ROWS: list[Key] = [
    ("t1", "own_waveform.backfill", "1.0.0"),
    ("t1", "own_waveform.backfill", "1.4.0"),
    ("t2", "own_waveform.backfill", "1.0.0"),  # only version: current
    ("t3", "own_waveform.backfill", "1.4.0"),
    ("t3", "own_waveform.backfill", "1.10.0"),  # numeric, not lexicographic
    ("t4", "own_key.backfill", "1.0.0"),  # canonical pointer: kept
    ("t4", "own_key.backfill", "1.1.0"),
    ("t5", "librosa", "a"),
    ("t5", "librosa", "b"),  # not own_: never touched
    ("t6", "own_key.backfill", "dev"),  # not dotted ints: never touched
    ("t6", "own_key.backfill", "1.1.0"),
]


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE analysis (stable_id TEXT, backend TEXT, backend_version TEXT, "
        "record_json TEXT, PRIMARY KEY (stable_id, backend, backend_version))"
    )
    conn.execute(
        "CREATE TABLE analysis_canonical (stable_id TEXT, lane TEXT, backend TEXT, "
        "backend_version TEXT, PRIMARY KEY (stable_id, lane))"
    )
    conn.execute(
        "CREATE TABLE analysis_stale (stable_id TEXT, lane TEXT, backend TEXT, "
        "backend_version TEXT, PRIMARY KEY (stable_id, lane, backend, backend_version))"
    )
    conn.executemany("INSERT INTO analysis VALUES (?, ?, ?, ?)", [(*k, "x" * 1000) for k in _ROWS])
    conn.execute("INSERT INTO analysis_canonical VALUES ('t4', 'key', 'own_key.backfill', '1.0.0')")
    conn.close()
    return db


def _keys(db: Path) -> set[Key]:
    conn = sqlite3.connect(db)
    try:
        return {tuple(r) for r in conn.execute("SELECT stable_id, backend, backend_version FROM analysis")}
    finally:
        conn.close()


def _assert_current_survives(before: set[Key], after: set[Key]) -> None:
    """The invariant: the highest dotted-int version of every (track, backend) survives."""
    newest: dict[tuple[str, str], Key] = {}
    for key in before:
        version = retention.parse_version(key[2])
        if version is None:
            continue
        best = newest.get(key[:2])
        if best is None or version > retention.parse_version(best[2]):
            newest[key[:2]] = key
    missing = [key for key in newest.values() if key not in after]
    if missing:
        raise AssertionError(f"current version pruned: {missing}")


def test_prune_removes_only_superseded_rows(tmp_path: Path) -> None:
    """[if] own_* rows have a newer sibling [then] only those uncanonical ones go, [else stop]."""
    db = _make_db(tmp_path)
    before = _keys(db)
    report = retention.prune_superseded(db)
    after = _keys(db)
    assert before - after == {
        ("t1", "own_waveform.backfill", "1.0.0"),
        ("t3", "own_waveform.backfill", "1.4.0"),
    }
    assert report.rows == 2 and report.bytes == 2000
    _assert_current_survives(before, after)


def test_invariant_check_bites_when_a_current_row_is_deleted(tmp_path: Path) -> None:
    """[if] a prune deleted a current row [then] the invariant check fails, [else stop]."""
    db = _make_db(tmp_path)
    before = _keys(db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM analysis WHERE stable_id='t3' AND backend_version='1.10.0'")
    conn.commit()
    conn.close()
    with pytest.raises(AssertionError, match="current version pruned"):
        _assert_current_survives(before, _keys(db))


def test_dry_run_counts_and_writes_nothing(tmp_path: Path) -> None:
    """[if] dry_run is set [then] rows and bytes are counted and no row is deleted, [else stop]."""
    db = _make_db(tmp_path)
    before = _keys(db)
    report = retention.prune_superseded(db, dry_run=True)
    assert report.dry_run is True and report.rows == 2 and report.bytes == 2000
    assert _keys(db) == before


def test_stale_marker_protects_a_row(tmp_path: Path) -> None:
    """[if] analysis_stale names an old version [then] that row is kept, [else stop]."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO analysis_stale VALUES ('t1', 'waveform', 'own_waveform.backfill', '1.0.0')")
    conn.commit()
    conn.close()
    retention.prune_superseded(db)
    assert ("t1", "own_waveform.backfill", "1.0.0") in _keys(db)


def test_version_order_is_numeric() -> None:
    """[if] versions are 1.4.0 and 1.10.0 [then] 1.10.0 is newer; a non-numeric one is None, [else stop]."""
    assert retention.parse_version("1.10.0") > retention.parse_version("1.4.0")
    assert retention.parse_version("dev") is None


def test_missing_analysis_table_is_a_no_op(tmp_path: Path) -> None:
    """[if] the database has no analysis table [then] prune reports zero rows, [else stop]."""
    db = tmp_path / "state.db"
    sqlite3.connect(db).close()
    assert retention.prune_superseded(db).rows == 0
