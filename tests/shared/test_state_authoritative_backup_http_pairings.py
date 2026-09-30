"""Selective restore of the pairings group covers http_pairings (PAIR-04).

Review thread PRRT_kwDOSEvNd86mix-h: /api/v1/pairings reads http_pairings, so a
``restore --table pairings`` that only rewrote the CAT-03 ``pairings`` graph
reported success while the API kept serving the damaged rows. Every case uses a
real migrated state.db and the real webui table helper; no mocks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared import state_authoritative_backup as sab
from apps.shared.state_authoritative_backup import backup_state_db, restore_tables
from apps.webui.server.pairings_sqlite import ensure_http_pairings_table
from tests.shared.test_state_authoritative_backup import NOW, _seed_authoritative_data

HTTP_ROW = ("pair-1", "track-a", "track-b", "<->", "manual", "keep", None, NOW, NOW)


def _connect(data_dir: Path) -> sqlite3.Connection:
    return sqlite3.connect(sab.state_db_path(data_dir))


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _insert_http_pairing(conn: sqlite3.Connection, row: tuple[object, ...]) -> None:
    conn.execute(
        "INSERT INTO http_pairings(pairing_id, from_stable_id, to_stable_id, direction, "
        "source, notes, snapshot_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )


def _pairings(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT from_stable_id, to_stable_id, direction FROM pairings ORDER BY 1, 2"
    ).fetchall()


def _http_pairings(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT pairing_id, from_stable_id, to_stable_id, direction, notes "
        "FROM http_pairings ORDER BY pairing_id"
    ).fetchall()


@pytest.fixture
def old_data_dir(tmp_path: Path) -> Path:
    """A machine whose state.db has never had a webui pairing write (no http_pairings)."""
    data_dir = tmp_path / "old"
    _seed_authoritative_data(data_dir)
    return data_dir


@pytest.fixture
def new_data_dir(tmp_path: Path) -> Path:
    """A machine with rows in both the pairings graph and http_pairings."""
    data_dir = tmp_path / "new"
    _seed_authoritative_data(data_dir)
    conn = _connect(data_dir)
    try:
        ensure_http_pairings_table(conn)
        _insert_http_pairing(conn, HTTP_ROW)
        conn.commit()
    finally:
        conn.close()
    return data_dir


@pytest.mark.requirement("PAIR-04")
@pytest.mark.requirement("LIBM-113")
def test_restore_pairings_round_trips_both_tables(new_data_dir: Path, tmp_path: Path) -> None:
    """[if] pairings is restored [then] http_pairings and the pairings graph both return to the backup, [else stop]."""
    backup = backup_state_db(new_data_dir, tmp_path / "backups", keep=3)
    assert backup.row_counts["http_pairings"] == 1
    conn = _connect(new_data_dir)
    try:
        want_graph, want_http = _pairings(conn), _http_pairings(conn)
        conn.execute("DELETE FROM pairings")
        conn.execute("UPDATE http_pairings SET notes = 'damaged'")
        _insert_http_pairing(conn, ("pair-stray", "track-b", "track-a", "->", "ai", None, None, NOW, NOW))
        conn.commit()
    finally:
        conn.close()

    assert restore_tables(backup.path, new_data_dir, ["pairings"]) == ["pairings"]

    conn = _connect(new_data_dir)
    try:
        assert _pairings(conn) == want_graph
        assert _http_pairings(conn) == want_http
        assert [row[0] for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
    finally:
        conn.close()


@pytest.mark.requirement("LIBM-113")
def test_backup_without_http_pairings_restores_the_graph_and_leaves_http_rows(
    old_data_dir: Path, tmp_path: Path
) -> None:
    """[if] the backup predates http_pairings [then] pairings restores and live http_pairings is untouched, [else stop]."""
    backup = backup_state_db(old_data_dir, tmp_path / "backups", keep=3)
    assert "http_pairings" not in backup.row_counts
    conn = _connect(old_data_dir)
    try:
        want_graph = _pairings(conn)
        conn.execute("DELETE FROM pairings")
        ensure_http_pairings_table(conn)
        _insert_http_pairing(conn, HTTP_ROW)
        conn.commit()
        want_http = _http_pairings(conn)
    finally:
        conn.close()

    assert restore_tables(backup.path, old_data_dir, ["pairings"]) == ["pairings"]

    conn = _connect(old_data_dir)
    try:
        assert _pairings(conn) == want_graph
        assert _http_pairings(conn) == want_http
    finally:
        conn.close()


@pytest.mark.requirement("LIBM-113")
def test_live_db_without_http_pairings_gets_it_created_then_restored(
    new_data_dir: Path, tmp_path: Path
) -> None:
    """[if] the live state.db has no http_pairings yet [then] restore creates it and fills it from the backup, [else stop]."""
    backup = backup_state_db(new_data_dir, tmp_path / "backups", keep=3)
    conn = _connect(new_data_dir)
    try:
        want_http = _http_pairings(conn)
        conn.execute("DROP TABLE http_pairings")
        conn.commit()
        assert not _has_table(conn, "http_pairings")
    finally:
        conn.close()

    restore_tables(backup.path, new_data_dir, ["pairings"])

    conn = _connect(new_data_dir)
    try:
        assert _http_pairings(conn) == want_http
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='http_pairings'"
            )
        }
    finally:
        conn.close()
    # Created by the webui helper, so its indexes come with it.
    assert {"idx_http_pairings_from", "idx_http_pairings_to", "idx_http_pairings_source"} <= indexes


@pytest.mark.requirement("LIBM-113")
def test_control_pairings_only_restore_is_unchanged(old_data_dir: Path, tmp_path: Path) -> None:
    """Control for the pre-fix behavior.

    [if] neither side has http_pairings [then] pairings restores as before and no table is created, [else stop].
    """
    backup = backup_state_db(old_data_dir, tmp_path / "backups", keep=3)
    conn = _connect(old_data_dir)
    try:
        want_graph = _pairings(conn)
        conn.execute("DELETE FROM pairings")
        conn.commit()
    finally:
        conn.close()

    assert restore_tables(backup.path, old_data_dir, ["pairings"]) == ["pairings"]

    conn = _connect(old_data_dir)
    try:
        assert _pairings(conn) == want_graph
        # Nothing in the backup asked for it, so the live db gains no new table.
        assert not _has_table(conn, "http_pairings")
    finally:
        conn.close()


def test_backup_missing_the_required_graph_table_still_refuses(
    old_data_dir: Path, tmp_path: Path
) -> None:
    """Control: the optional table does not relax the required one."""
    backup = backup_state_db(old_data_dir, tmp_path / "backups", keep=3)
    raw = sqlite3.connect(backup.path)
    try:
        raw.execute("DROP TABLE pairings")
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(sab.StateAuthoritativeBackupError, match="no table 'pairings'"):
        restore_tables(backup.path, old_data_dir, ["pairings"])
