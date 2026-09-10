"""The one-shot repair for stored timestamps the protocol cannot order.

Contract under test: ``apps/shared/state/normalize_stamps.py``, answering
round 2 finding N3b's second half. ``sync_stamp.coalesce_stored_stamp`` stops
one bad row bricking the machine; it does not make the row right -- a row
stuck at EPOCH loses every conflict it takes part in, silently. This module
is how an operator puts such rows back on the UTC line.

Acceptance criteria, one assertion block each:
- if the scan does not find a naive stored stamp, the row that bricks
  spoke_push is invisible to the repair -- broken.
- if the scan rewrites an orderable but non-canonical spelling, the pass
  churns rows the protocol already handles -- broken.
- if --dry-run writes anything, the flag is a lie -- broken.
- if --live leaves any stored stamp unorderable, the repair did not repair --
  broken.
- if the swept column list drifts from the protocol's digest set, a table can
  hold an unorderable stamp the repair never visits -- broken.
- if neither or both of --dry-run/--live is accepted, the CLI has a hidden
  default about writing to a database -- broken.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import normalize_stamps
from apps.shared.state import sync_stamp
from apps.sync_hub import protocol as hub_protocol

pytestmark = pytest.mark.requirement("INFRA-01")

SID = "c" * 40
CANONICAL = "2026-08-30T09:00:00.000000+00:00"


def _seed_track(conn: sqlite3.Connection, stable_id: str, *, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, "
        "updated_at) VALUES (?, 'inferred', 'legacy', ?, ?)",
        (stable_id, CANONICAL, updated_at),
    )


# ----- (1) what the scan finds, and what it leaves alone -------------------


def test_a_naive_stamp_is_repaired_as_that_wall_time_in_utc(
    state_conn: sqlite3.Connection,
) -> None:
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    repairs = normalize_stamps.scan(state_conn)
    assert len(repairs) == 1
    repair = repairs[0]
    assert (repair.table, repair.column) == ("tracks", "updated_at")
    assert repair.reason == "naive-assumed-utc"
    assert repair.replacement == "2024-11-01T12:00:00.000000+00:00"


def test_sqlite_s_own_current_timestamp_spelling_is_repaired(
    state_conn: sqlite3.Connection,
) -> None:
    """``'2024-11-01 12:00:00'`` trips the rejector identically, and it is
    the spelling a hand-written ``CURRENT_TIMESTAMP`` produces."""
    _seed_track(state_conn, SID, updated_at="2024-11-01 12:00:00")
    repairs = normalize_stamps.scan(state_conn)
    assert [r.reason for r in repairs] == ["naive-assumed-utc"]
    assert repairs[0].replacement == "2024-11-01T12:00:00.000000+00:00"


def test_an_unrecoverable_value_becomes_the_lowest_storable_stamp(
    state_conn: sqlite3.Connection,
) -> None:
    _seed_track(state_conn, SID, updated_at="whenever")
    repairs = normalize_stamps.scan(state_conn)
    assert [r.reason for r in repairs] == ["unparseable-to-floor"]
    assert repairs[0].replacement == normalize_stamps.FLOOR_STAMP


def test_the_floor_is_storable_and_the_epoch_sentinel_is_not() -> None:
    """The distinction is load-bearing, so it gets its own assertion.

    ``sync_stamp.EPOCH`` is year zero: it sorts below everything under string
    comparison, which is all a SENTINEL has to do, but ``datetime`` cannot
    represent it. A repair that wrote it into a row would hand
    ``protocol.canonical_timestamp`` a value it refuses on every read -- the
    exact brick this pass exists to clear.
    """
    with pytest.raises(sync_stamp.SyncStampError):
        sync_stamp.parse_canonical(sync_stamp.EPOCH)
    assert (
        sync_stamp.to_canonical(normalize_stamps.FLOOR_STAMP)
        == normalize_stamps.FLOOR_STAMP
    )
    assert sync_stamp.EPOCH < normalize_stamps.FLOOR_STAMP < CANONICAL


@pytest.mark.parametrize(
    "orderable",
    [
        CANONICAL,
        "2026-08-30T09:00:00Z",        # Z suffix
        "2026-08-30T09:00:00+00:00",   # second precision
        "2026-08-30T10:00:00+01:00",   # non-UTC offset
    ],
)
def test_an_orderable_stamp_is_never_rewritten(
    state_conn: sqlite3.Connection, orderable: str,
) -> None:
    """The protocol boundary re-emits these canonically on every read, so
    rewriting them would churn rows for no correctness gain."""
    _seed_track(state_conn, SID, updated_at=orderable)
    assert normalize_stamps.scan(state_conn) == []


def test_deleted_at_is_swept_too(state_conn: sqlite3.Connection) -> None:
    """``deleted_at`` is in the digest and is hashed verbatim (round 2 N8),
    so an unorderable tombstone stamp is a divergence waiting to happen."""
    _seed_track(state_conn, SID, updated_at=CANONICAL)
    state_conn.execute(
        "UPDATE tracks SET deleted_at = '2024-11-01 12:00:00' WHERE stable_id = ?",
        (SID,),
    )
    repairs = normalize_stamps.scan(state_conn)
    assert [(r.table, r.column) for r in repairs] == [("tracks", "deleted_at")]


# ----- (2) applying the repair ---------------------------------------------


def test_applying_the_repair_makes_every_stored_stamp_orderable(
    state_conn: sqlite3.Connection,
) -> None:
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    _seed_track(state_conn, "d" * 40, updated_at="not a timestamp")
    _seed_track(state_conn, "e" * 40, updated_at=CANONICAL)

    applied = normalize_stamps.apply_repairs(
        state_conn, normalize_stamps.scan(state_conn)
    )
    assert applied == 2
    assert normalize_stamps.scan(state_conn) == [], "the repair is idempotent"

    for (stored,) in state_conn.execute("SELECT updated_at FROM tracks"):
        # The whole point: sync_hub.protocol.canonical_timestamp no longer
        # raises on any of them, so spoke_push can offer the library again.
        assert sync_stamp.to_canonical(stored) == stored


def test_the_repair_is_one_transaction(state_conn: sqlite3.Connection) -> None:
    """A half-applied repair would leave the operator guessing which rows
    were touched, which is the N3a failure wearing a different hat.

    The failure is injected as a repair naming a column that does not exist,
    so the last UPDATE raises after the earlier ones have run.
    """
    _seed_track(state_conn, SID, updated_at="2024-11-01T12:00:00")
    _seed_track(state_conn, "d" * 40, updated_at="2024-11-02T12:00:00")
    repairs = normalize_stamps.scan(state_conn)
    assert len(repairs) == 2

    doomed = normalize_stamps.Repair(
        table="tracks", column="no_such_column", rowid=1,
        stored="whatever", replacement=CANONICAL,
        reason="unparseable-to-floor",
    )
    with pytest.raises(sqlite3.OperationalError):
        normalize_stamps.apply_repairs(state_conn, [*repairs, doomed])

    assert len(normalize_stamps.scan(state_conn)) == 2, (
        "a failed repair must leave every row exactly as it was"
    )


# ----- (3) the swept list cannot silently shrink ---------------------------


def test_the_swept_columns_cover_the_whole_digest_set() -> None:
    """A digest table missing from the sweep can hold an unorderable stamp
    the repair never visits, which is a machine nothing can unbrick."""
    swept = {table for table, _column in normalize_stamps.STAMP_COLUMNS}
    assert set(hub_protocol.DIGEST_TABLES) <= swept
    for table in hub_protocol.DIGEST_TABLES:
        columns = {
            column
            for swept_table, column in normalize_stamps.STAMP_COLUMNS
            if swept_table == table
        }
        assert {"updated_at", "deleted_at"} <= columns, table
    assert sync_stamp.LOCAL_CHANGELOG_TABLE in swept, (
        "the spoke changelog carries stamps the push fence reads"
    )


def test_the_digest_half_of_the_swept_list_stays_alphabetical() -> None:
    """The convention the tuple's own layout asserts, made checkable.

    ``STAMP_COLUMNS`` is a hand-maintained mirror of ``DIGEST_TABLES`` x
    {updated_at, deleted_at}, ordered alphabetically by table so a reader can
    find an entry and an author can see where a new one goes. Nothing checked
    that, so the ordering was one careless append away from being decorative
    -- and a list nobody can scan is a list a table goes missing from. The
    ``local_changelog`` pair is deliberately excluded: it is appended after
    the digest set rather than sorted into it.
    """
    digest = set(hub_protocol.DIGEST_TABLES)
    tables = [table for table, _column in normalize_stamps.STAMP_COLUMNS
              if table in digest]
    assert tables == sorted(tables), (
        f"STAMP_COLUMNS' digest half is out of alphabetical order: {tables}"
    )
    assert "lyric_verdict" in digest, (
        "schema v9 put lyric_verdict in the sync set; if it is not in the "
        "digest set here, the rest of this module's coverage is a lie"
    )


# ----- (4) the CLI ---------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A data dir in the canonical ``<data-dir>/state/state.db`` layout."""
    target = normalize_stamps.state_db_path(tmp_path)
    conn = state_db.open_rw(target)
    try:
        _seed_track(conn, SID, updated_at="2024-11-01T12:00:00")
    finally:
        conn.close()
    return tmp_path


def _stored_stamp(data_dir: Path) -> str:
    conn = sqlite3.connect(str(normalize_stamps.state_db_path(data_dir)))
    try:
        return str(conn.execute("SELECT updated_at FROM tracks").fetchone()[0])
    finally:
        conn.close()


def test_dry_run_reports_every_repair_and_writes_nothing(
    data_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert normalize_stamps.main(["--data-dir", str(data_dir), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "tracks.updated_at" in out
    assert "naive-assumed-utc" in out
    assert "[DRY-RUN]" in out
    assert _stored_stamp(data_dir) == "2024-11-01T12:00:00", (
        "--dry-run must not touch the database"
    )


def test_live_rewrites_the_row(
    data_dir: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert normalize_stamps.main(["--data-dir", str(data_dir), "--live"]) == 0
    assert "[OK] rewrote 1 value(s)" in capsys.readouterr().out
    assert _stored_stamp(data_dir) == "2024-11-01T12:00:00.000000+00:00"


def test_a_clean_db_says_so_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    state_db.open_rw(normalize_stamps.state_db_path(tmp_path)).close()
    assert normalize_stamps.main(["--data-dir", str(tmp_path), "--dry-run"]) == 0
    assert "[OK]" in capsys.readouterr().out


def test_a_missing_db_fails_loudly_rather_than_reporting_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert normalize_stamps.main(
        ["--data-dir", str(tmp_path / "nope"), "--dry-run"]
    ) == 2
    assert "[ERROR]" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [[], ["--dry-run", "--live"]])
def test_the_write_mode_is_never_defaulted(
    data_dir: Path, flags: list[str],
) -> None:
    """Neither flag, or both, must be an argparse error. A pass that writes
    to a database by default is a hidden default with a blast radius."""
    with pytest.raises(SystemExit):
        normalize_stamps.main(["--data-dir", str(data_dir), *flags])
