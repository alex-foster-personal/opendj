"""Durable pairings on SqliteBackend.

A pairing saved from the Create pairing sheet used to live only in the
daemon's process memory: it vanished on restart and never reached the
``pairings`` table the CLI and the suggester's stage-2 rerank read. These
tests pin the rows to that table.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.pairings import PairingsRepo
from apps.shared.state import db as state_db
from apps.webui.server.backend import ConflictError, NotFoundError, Pairing
from apps.webui.server.etag import compute_etag
from apps.webui.server.sqlite_backend import SqliteBackend

SNAPSHOT = {
    "version": 1,
    "beat_sync_max": False,
    "decks": [
        {"deck_id": 1, "stable_id": "a", "title": "A", "position_ms": 1000.0,
         "timestamp": {"unit": "time", "value": 1000.0},
         "eq_adjusts": [{"band": "low", "value": 0.2}]},
        {"deck_id": 2, "stable_id": "b", "title": "B", "position_ms": 2000.0,
         "timestamp": {"unit": "time", "value": 2000.0}, "eq_adjusts": []},
    ],
}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _pairing(
    from_id: str = "a", to_id: str = "b", *, direction: str = "->",
    notes: str | None = None, snapshot: dict | None = None,
    pairing_id: str = "client-side-id",
) -> Pairing:
    now = _now()
    return Pairing(
        pairing_id=pairing_id, from_stable_id=from_id,
        to_stable_id=to_id, direction=direction, source="manual",  # type: ignore[arg-type]
        notes=notes, snapshot=snapshot, created_at=now, updated_at=now,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    return path


def test_snapshot_and_notes_survive_a_restart(db_path: Path) -> None:
    created = SqliteBackend(db_path).create_pairing(
        _pairing(notes="drop on the 2nd chorus", snapshot=SNAPSHOT),
    )
    [listed] = SqliteBackend(db_path).list_pairings()
    assert listed == created
    assert listed.snapshot == SNAPSHOT
    assert listed.notes == "drop on the 2nd chorus"
    assert listed.direction == "->"


def test_ui_pairing_is_visible_to_the_cli_repo(db_path: Path) -> None:
    SqliteBackend(db_path).create_pairing(_pairing(direction="<->"))
    conn = sqlite3.connect(db_path)
    try:
        [edge] = list(PairingsRepo(conn, ensure_schema=False).list_all())
    finally:
        conn.close()
    assert (edge.from_stable_id, edge.to_stable_id) == ("a", "b")
    assert edge.direction == "either"
    assert edge.source == "manual"


def test_cli_pairing_is_visible_to_the_ui(db_path: Path) -> None:
    conn = state_db.open_rw(db_path)
    try:
        repo = PairingsRepo(conn)
        repo.add("a", "b", direction="into", notes="cli")
        repo.add("c", "d", direction="out_of")
    finally:
        conn.close()
    listed = {
        (p.from_stable_id, p.to_stable_id, p.direction, p.notes)
        for p in SqliteBackend(db_path).list_pairings()
    }
    # out_of reads as its reverse so the arrow always points the way the mix runs.
    assert listed == {("a", "b", "->", "cli"), ("d", "c", "->", None)}


def test_capturing_a_cli_reverse_edge_merges_into_it(db_path: Path) -> None:
    """If the CLI stored the visible edge reversed then a capture merges into it, else stop."""
    conn = state_db.open_rw(db_path)
    try:
        repo = PairingsRepo(conn)
        repo.add("b", "a", direction="out_of", notes="cli")  # reads as a -> b
        repo.add("d", "c", direction="either")  # reads as d <-> c
    finally:
        conn.close()
    backend = SqliteBackend(db_path)
    [before_ab] = [p for p in backend.list_pairings() if p.from_stable_id == "a"]

    merged = backend.create_pairing(_pairing("a", "b", notes="ui", snapshot=SNAPSHOT))
    backend.create_pairing(_pairing("c", "d", direction="<->", notes="ui"))

    assert merged.pairing_id == before_ab.pairing_id
    assert merged.notes == "cli\nui"
    assert merged.snapshot == SNAPSHOT
    listed = sorted(
        (p.from_stable_id, p.to_stable_id, p.direction, p.notes)
        for p in backend.list_pairings()
    )
    assert listed == [("a", "b", "->", "cli\nui"), ("d", "c", "<->", "ui")]


def test_repeat_merges_notes_and_keeps_first_snapshot(db_path: Path) -> None:
    backend = SqliteBackend(db_path)
    first = backend.create_pairing(_pairing(notes="one", snapshot=SNAPSHOT))
    other = {**SNAPSHOT, "beat_sync_max": True}
    second = backend.create_pairing(_pairing(notes="two", snapshot=other))
    assert second.pairing_id == first.pairing_id
    assert second.notes == "one\ntwo"
    assert second.snapshot == SNAPSHOT
    assert len(backend.list_pairings()) == 1


def test_repeat_without_change_returns_existing_unchanged(db_path: Path) -> None:
    backend = SqliteBackend(db_path)
    first = backend.create_pairing(_pairing(notes="one"))
    again = backend.create_pairing(_pairing(notes="one"))
    assert again == first


def test_snapshot_fills_an_edge_that_had_none(db_path: Path) -> None:
    backend = SqliteBackend(db_path)
    backend.create_pairing(_pairing())
    filled = backend.create_pairing(_pairing(snapshot=SNAPSHOT))
    assert filled.snapshot == SNAPSHOT


def test_filters(db_path: Path) -> None:
    backend = SqliteBackend(db_path)
    backend.create_pairing(_pairing("a", "b"))
    backend.create_pairing(_pairing("b", "c", pairing_id="client-side-id-2"))
    assert [p.to_stable_id for p in backend.list_pairings(from_stable_id="b")] == ["c"]
    assert [p.from_stable_id for p in backend.list_pairings(to_stable_id="b")] == ["a"]
    assert backend.list_pairings(source="ai") == []


def test_delete_checks_etag_and_existence(db_path: Path) -> None:
    backend = SqliteBackend(db_path)
    p = backend.create_pairing(_pairing())
    with pytest.raises(ConflictError):
        backend.delete_pairing(p.pairing_id, expected_etag='"stale"')
    assert len(backend.list_pairings()) == 1
    with pytest.raises(NotFoundError):
        backend.delete_pairing("pair-missing", expected_etag='"x"')
    backend.delete_pairing(
        p.pairing_id, expected_etag=compute_etag(p.pairing_id, p.updated_at),
    )
    assert backend.list_pairings() == []


def test_list_on_a_db_without_the_table_is_empty(db_path: Path) -> None:
    assert SqliteBackend(db_path).list_pairings() == []
    assert SqliteBackend(db_path).stats()["pairings"] == 0


def test_stats_counts_stored_pairings(db_path: Path) -> None:
    backend = SqliteBackend(db_path)
    backend.create_pairing(_pairing("a", "b"))
    backend.create_pairing(_pairing("b", "c", pairing_id="client-side-id-2"))
    assert backend.stats()["pairings"] == 2


def test_old_pairings_table_gains_snapshot_column(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE pairings (from_stable_id TEXT NOT NULL, "
        "to_stable_id TEXT NOT NULL, direction TEXT NOT NULL, "
        "source TEXT NOT NULL, notes TEXT, confidence REAL, "
        "created_at TEXT NOT NULL, modified_at TEXT NOT NULL, "
        "PRIMARY KEY (from_stable_id, to_stable_id, direction))"
    )
    conn.execute(
        "INSERT INTO pairings VALUES ('x', 'y', 'into', 'manual', 'old', "
        "NULL, '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    backend = SqliteBackend(db_path)
    # Reads tolerate the pre-column table; the first write upgrades it.
    [old] = backend.list_pairings()
    assert (old.from_stable_id, old.notes, old.snapshot) == ("x", "old", None)
    backend.create_pairing(_pairing(snapshot=SNAPSHOT))
    by_from = {p.from_stable_id: p for p in backend.list_pairings()}
    assert by_from["x"].notes == "old"
    assert by_from["a"].snapshot == SNAPSHOT


def test_snapshot_column_add_survives_losing_the_race(tmp_path: Path) -> None:
    """If a rival connection adds snapshot_json first then the lost ALTER is benign, else stop."""
    from apps.shared.pairings import schema_sql

    path = tmp_path / "race.db"
    setup = sqlite3.connect(path)
    setup.execute(schema_sql._PAIRINGS_DDL[0].replace("        snapshot_json  TEXT,\n", ""))
    setup.commit()
    setup.close()
    ours = sqlite3.connect(path, isolation_level=None)
    rival = sqlite3.connect(path, isolation_level=None)
    rival_added: list[bool] = []

    def _rival_wins(action: int, *_: object) -> int:
        # SQLite asks this as it prepares our ALTER, after our check saw the
        # column absent: the rival's real ALTER commits in that window.
        if action == sqlite3.SQLITE_ALTER_TABLE and not rival_added:
            rival_added.append(True)
            rival.execute("ALTER TABLE pairings ADD COLUMN snapshot_json TEXT")
        return sqlite3.SQLITE_OK

    ours.set_authorizer(_rival_wins)
    schema_sql.migrate_pairings_snapshot_json(ours)  # must not raise
    ours.set_authorizer(None)

    assert rival_added == [True]
    columns = [row[1] for row in ours.execute("PRAGMA table_info(pairings)")]
    assert columns.count("snapshot_json") == 1
    ours.close()
    rival.close()


def test_ensure_tables_waits_out_a_rival_wal_writer_adding_the_column(
    tmp_path: Path,
) -> None:
    """If a rival WAL writer adds snapshot_json mid-open then ensure waits and succeeds, else stop."""
    import threading

    from apps.shared.pairings import schema_sql

    path = tmp_path / "wal_race.db"
    setup = sqlite3.connect(path, isolation_level=None)
    setup.execute("PRAGMA journal_mode = WAL")
    setup.execute(schema_sql._PAIRINGS_DDL[0].replace("        snapshot_json  TEXT,\n", ""))
    setup.close()

    rival = sqlite3.connect(path, isolation_level=None)
    rival.execute("BEGIN IMMEDIATE")
    rival.execute("ALTER TABLE pairings ADD COLUMN snapshot_json TEXT")

    ours = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    ours.execute("PRAGMA busy_timeout = 5000")
    errors: list[sqlite3.Error] = []

    def _open() -> None:
        try:
            schema_sql.ensure_phase08_tables(ours)
        except sqlite3.Error as exc:  # surfaced below, not swallowed
            errors.append(exc)

    worker = threading.Thread(target=_open)
    worker.start()
    worker.join(0.3)  # ours is now waiting on (or about to need) the write lock
    rival.execute("COMMIT")
    worker.join(10)

    assert not worker.is_alive()
    assert errors == []
    columns = [row[1] for row in ours.execute("PRAGMA table_info(pairings)")]
    assert columns.count("snapshot_json") == 1
    ours.close()
    rival.close()


def test_snapshot_column_add_still_raises_a_real_failure(tmp_path: Path) -> None:
    """If the ALTER fails and the column is still absent then it raises, else stop."""
    from apps.shared.pairings import schema_sql

    path = tmp_path / "ro.db"
    conn = sqlite3.connect(path)
    conn.execute(schema_sql._PAIRINGS_DDL[0].replace("        snapshot_json  TEXT,\n", ""))
    conn.commit()
    conn.close()
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        schema_sql.migrate_pairings_snapshot_json(ro)
    ro.close()
