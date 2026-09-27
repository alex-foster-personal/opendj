"""The one-shot NFC repair for ``track_locations`` paths already stored.

Contract under test: ``apps/shared/state/normalize_locations.py``, answering
round 3 finding R3 (``.planning/cloudsync-round3-adversarial.md``).
``locations.normalize_stored_text`` stops a NEW write minting an NFD/NFC
duplicate; it does nothing for the rows a pre-round-3 macOS filesystem walk
already stored in NFD. One such row plus one NFC upsert reproduces round 2's
N2 brick exactly (spoke keeps two rows, hub collapses to one, digest compare
on ``track_locations`` fails forever). This module is the repair, modeled on
``apps.shared.state.normalize_stamps``.

Acceptance criteria, one assertion block each:
- if an NFD row and its NFC twin do not collapse to one row under ``--live``,
  the brick is unrepaired -- broken.
- if the surviving row is not the LWW winner, the collapse invented a winner
  the fleet will not agree on -- broken.
- if an exact stamp tie does not keep the smaller location_id, the rule the
  hub applies to the same pair, the spoke deletes the hub's survivor -- broken.
- if the survivor's stored path is not NFC afterwards, the next NFC upsert
  mints the duplicate again -- broken.
- if a lone NFD row (no twin) is not rewritten NFC in place -- broken.
- if the collapse leaves no ``local_changelog`` entry for the survivor, the
  convergence never reaches a peer (ADR 08 point 2) -- broken.
- if the dropped loser leaves a dangling changelog entry, the next push
  raises for a row that is gone (finding 4a's shape) -- broken.
- if ``--dry-run`` writes anything, the flag is a lie -- broken.
- if an already-NFC lone row is "repaired", the pass churns rows the wire
  already handles -- broken.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import normalize_locations
from apps.shared.state.sync_stamp import encode_row_pk

pytestmark = pytest.mark.requirement("INFRA-01")

SID = "d" * 40
MACHINE = "m" * 32

# One filename, two Unicode spellings of its accented character:
#   NFC: U+00E9  (é as a single code point)   -- what rekordbox/Windows hand back
#   NFD: U+0065 U+0301 (e + combining acute)  -- what a macOS filesystem walk hands back
_NFC_PATH = "/music/café.mp3"
_NFD_PATH = unicodedata.normalize("NFD", _NFC_PATH)

_EARLY = "2026-08-30T09:00:00.000000+00:00"
_LATE = "2026-08-31T09:00:00.000000+00:00"


def _seed_machine_and_track(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, first_seen, "
        "last_seen) VALUES (?, 'this-mac', 'macos', 0, ?, ?)",
        (MACHINE, _EARLY, _EARLY),
    )
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, "
        "updated_at) VALUES (?, 'inferred', 'Cafe', ?, ?)",
        (SID, _EARLY, _EARLY),
    )


def _seed_location(
    conn: sqlite3.Connection,
    *,
    location_id: str,
    file_path: str,
    updated_at: str,
) -> None:
    """Insert a raw local ``track_locations`` row, path bytes stored verbatim.

    Deliberately not through ``locations.upsert_location`` -- that NFC-
    normalizes, which is exactly the write path this pass exists to clean up
    AFTER. The scenario is a row that predates that normalization.
    """
    conn.execute(
        "INSERT INTO track_locations(location_id, stable_id, machine_id, kind, "
        "role, file_path, available, created_at, updated_at, origin_device_id) "
        "VALUES (?, ?, ?, 'local', 'primary', ?, 1, ?, ?, ?)",
        (location_id, SID, MACHINE, file_path, updated_at, updated_at, MACHINE),
    )


def _changelog_rows(conn: sqlite3.Connection, location_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT updated_at, origin_device_id FROM local_changelog "
        "WHERE table_name = 'track_locations' AND row_pk = ?",
        (encode_row_pk((location_id,)),),
    ).fetchall()


# ----- the R3 regression: NFD + NFC twin collapse to the LWW winner --------


def test_nfd_row_and_its_nfc_twin_collapse_to_the_lww_winner(
    state_conn: sqlite3.Connection,
) -> None:
    """Both spellings of one file on one machine, the NFC twin edited later,
    so LWW must keep the NFC row and drop the NFD one -- leaving exactly one
    row, stored NFC, with a changelog entry to carry the collapse to peers.
    """
    _seed_machine_and_track(state_conn)
    _seed_location(state_conn, location_id="loc-nfd", file_path=_NFD_PATH, updated_at=_EARLY)
    _seed_location(state_conn, location_id="loc-nfc", file_path=_NFC_PATH, updated_at=_LATE)

    collapses = normalize_locations.scan(state_conn)
    assert len(collapses) == 1, "the two spellings did not group under one natural key"
    collapse = collapses[0]
    assert collapse.winner.location_id == "loc-nfc", "LWW should keep the later NFC row"
    assert [loser.location_id for loser in collapse.losers] == ["loc-nfd"]

    assert normalize_locations.apply_collapses(state_conn, collapses) == 1

    rows = state_conn.execute(
        "SELECT location_id, file_path FROM track_locations WHERE stable_id = ?",
        (SID,),
    ).fetchall()
    assert len(rows) == 1, f"collapse left {len(rows)} rows, not one: {rows}"
    surviving_id, surviving_path = rows[0]
    assert surviving_id == "loc-nfc", "the LWW winner did not survive"
    assert unicodedata.is_normalized("NFC", surviving_path), "survivor path is not NFC"
    assert surviving_path == _NFC_PATH

    # The dropped loser must leave no dangling changelog entry, and the
    # survivor must carry one so the collapse reaches a peer.
    assert _changelog_rows(state_conn, "loc-nfd") == [], "loser changelog not pruned"
    survivor_log = _changelog_rows(state_conn, "loc-nfc")
    assert survivor_log, "survivor was not logged; the collapse will not sync"
    assert survivor_log[-1][0] == _LATE, "survivor's own updated_at must be preserved"


def test_nfd_winner_keeps_its_row_but_stored_nfc(
    state_conn: sqlite3.Connection,
) -> None:
    """When the NFD row is the LWW winner, ITS row survives, but its stored
    path is rewritten NFC so the next NFC upsert reuses it instead of forking.
    """
    _seed_machine_and_track(state_conn)
    _seed_location(state_conn, location_id="loc-nfd", file_path=_NFD_PATH, updated_at=_LATE)
    _seed_location(state_conn, location_id="loc-nfc", file_path=_NFC_PATH, updated_at=_EARLY)

    collapses = normalize_locations.scan(state_conn)
    assert collapses[0].winner.location_id == "loc-nfd"
    normalize_locations.apply_collapses(state_conn, collapses)

    rows = state_conn.execute(
        "SELECT location_id, file_path FROM track_locations WHERE stable_id = ?",
        (SID,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "loc-nfd"
    assert rows[0][1] == _NFC_PATH, "the winning NFD row was not rewritten NFC"


def test_an_exact_stamp_tie_keeps_the_smaller_location_id_like_the_hub(
    state_conn: sqlite3.Connection,
) -> None:
    """Twins with identical ``(updated_at, origin_device_id)``: the hub keeps
    the smaller primary key (``engine_apply._duplicate_incoming_wins``), so
    the spoke must too, whatever order SQLite hands the rows back in.
    """
    _seed_machine_and_track(state_conn)
    # The larger id goes in FIRST, so row order alone would elect it.
    _seed_location(state_conn, location_id="loc-b", file_path=_NFD_PATH, updated_at=_LATE)
    _seed_location(state_conn, location_id="loc-a", file_path=_NFC_PATH, updated_at=_LATE)
    row_order = [
        str(row[0]) for row in state_conn.execute("SELECT location_id FROM track_locations")
    ]
    assert row_order == ["loc-b", "loc-a"], "control: row order must favor the wrong row"

    collapses = normalize_locations.scan(state_conn)

    assert collapses[0].winner.location_id == "loc-a", "the tie ignored the hub's pk rule"
    assert [loser.location_id for loser in collapses[0].losers] == ["loc-b"]


# ----- the lone-NFD case: rewrite in place, no delete ----------------------


def test_lone_nfd_row_is_rewritten_in_place(state_conn: sqlite3.Connection) -> None:
    """A single NFD row with no twin -- the common case for this repo -- is
    normalized in place, no row dropped, one changelog entry added.
    """
    _seed_machine_and_track(state_conn)
    _seed_location(state_conn, location_id="loc-nfd", file_path=_NFD_PATH, updated_at=_EARLY)

    collapses = normalize_locations.scan(state_conn)
    assert len(collapses) == 1
    assert collapses[0].losers == ()
    normalize_locations.apply_collapses(state_conn, collapses)

    rows = state_conn.execute(
        "SELECT location_id, file_path FROM track_locations WHERE stable_id = ?",
        (SID,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == _NFC_PATH
    assert _changelog_rows(state_conn, "loc-nfd"), "in-place rewrite was not logged"


# ----- what it leaves alone ------------------------------------------------


def test_an_already_nfc_lone_row_is_left_untouched(
    state_conn: sqlite3.Connection,
) -> None:
    _seed_machine_and_track(state_conn)
    _seed_location(state_conn, location_id="loc-nfc", file_path=_NFC_PATH, updated_at=_EARLY)
    assert normalize_locations.scan(state_conn) == []


# ----- the CLI flags -------------------------------------------------------


def _data_dir_with_db(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    return data_dir, data_dir / "state" / "state.db"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    data_dir, db_path = _data_dir_with_db(tmp_path)
    conn = state_db.open_rw(db_path)
    try:
        _seed_machine_and_track(conn)
        _seed_location(conn, location_id="loc-nfd", file_path=_NFD_PATH, updated_at=_EARLY)
        _seed_location(conn, location_id="loc-nfc", file_path=_NFC_PATH, updated_at=_LATE)
    finally:
        conn.close()

    assert normalize_locations.main(["--data-dir", str(data_dir), "--dry-run"]) == 0

    conn = state_db.open_ro(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM track_locations WHERE stable_id = ?", (SID,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2, "--dry-run collapsed rows; it must write nothing"


def test_live_collapses_via_the_cli(tmp_path: Path) -> None:
    data_dir, db_path = _data_dir_with_db(tmp_path)
    conn = state_db.open_rw(db_path)
    try:
        _seed_machine_and_track(conn)
        _seed_location(conn, location_id="loc-nfd", file_path=_NFD_PATH, updated_at=_EARLY)
        _seed_location(conn, location_id="loc-nfc", file_path=_NFC_PATH, updated_at=_LATE)
    finally:
        conn.close()

    assert normalize_locations.main(["--data-dir", str(data_dir), "--live"]) == 0

    conn = state_db.open_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT file_path FROM track_locations WHERE stable_id = ?", (SID,)
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0][0] == _NFC_PATH


def test_cli_requires_exactly_one_mode(tmp_path: Path) -> None:
    data_dir, _ = _data_dir_with_db(tmp_path)
    with pytest.raises(SystemExit):
        normalize_locations.main(["--data-dir", str(data_dir)])
    with pytest.raises(SystemExit):
        normalize_locations.main(
            ["--data-dir", str(data_dir), "--dry-run", "--live"]
        )
