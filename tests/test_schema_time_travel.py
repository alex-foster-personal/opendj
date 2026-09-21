"""Schema time travel: an OLD build's database through the full ladder.

The fixture axis this covers (GUARD-08, from the Tue 1 Sep 2026 audit): every
migration test before this one started from EMPTY, so nothing proved that a
database an old build actually wrote - old shapes, real rows - migrates
forward AND that today's accessors read the result. The #664 incident is the
proof it was missing: ``bulk_locations`` was written against the pre-cloudsync
``track_locations`` (INTEGER ``id``, no ``machine_id``) and failed on three
linked assumptions the moment it met the migrated shape.

Method: restore a PINNED v5 schema dump (``tests/fixtures/schema/v5.sql``,
checksummed in its manifest), seed rows in the old shape, then reopen through
the REAL ``open_rw`` (which runs the remaining ladder plus the machine-id
backfill) and read back through the REAL accessors.

The dump is pinned rather than replayed from ``state_schema.MIGRATIONS``
because a history built from today's migration list changes whenever those
entries are edited, so an accidental rewrite of migrations 1-5 would update
both the code under test and its own historical input together and this test
could not see it (Codex, #701).

- [if] an old-shape db with data reopens via open_rw [then] it lands on
  SCHEMA_VERSION with every row carried over [broken if the ladder only ever
  saw empty databases].
- [if] code still selects the pre-v7 ``id`` column [then] it fails LOUDLY at
  execute time [broken if it silently reads a different column's values].
- [if] a row escapes the machine-id backfill [then] the accessor refuses it
  with the runbook message, never returns a half-formed location.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state.locations import LocationError, list_locations

_OLD_BUILD_VERSION = 5  # pre-auth (v6), pre-cloudsync (v7)
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "schema"
_SUPPORTED_MANIFEST_VERSION = 1


def _verified_v5_sql() -> str:
    """The pinned v5 dump, after its manifest version and sha256 both match.

    This fixture IS the history under test. Restoring it unchecked means the
    test can pass against a dump somebody regenerated, which is a green light
    produced by a rewritten baseline rather than by a working migration ladder.
    The version gate runs before the hash, because a checksum compared against
    a field that may have moved is not a verification.
    """
    manifest = json.loads((_FIXTURE_DIR / "manifest.json").read_text())
    version = manifest.get("version")
    assert version == _SUPPORTED_MANIFEST_VERSION, (
        f"schema fixture manifest declares version {version!r}, this module "
        f"supports {_SUPPORTED_MANIFEST_VERSION}. Failing closed rather than "
        "reading a format whose meaning is not known."
    )
    entry = manifest["files"]["v5.sql"]
    assert entry["schema_version"] == _OLD_BUILD_VERSION, (
        f"the pinned dump declares schema {entry['schema_version']}, this test "
        f"is about v{_OLD_BUILD_VERSION}"
    )
    path = _FIXTURE_DIR / "v5.sql"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == entry["sha256"], (
        "v5.sql does not match its manifest checksum. This dump is IMMUTABLE: "
        "it is the v5 shape, not a view of the current migration list, and "
        "regenerating it to make a test pass deletes the only evidence of what "
        f"v5 was. expected {entry['sha256']}, got {digest}"
    )
    return path.read_text()


def _write_v5_database(path: Path) -> None:
    """Restore the pinned v5 dump, then seed rows the way a v5 build's writer did."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_verified_v5_sql())
        stamped = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
        assert stamped == _OLD_BUILD_VERSION, (
            f"the restored dump stamps schema {stamped}, not v{_OLD_BUILD_VERSION}"
        )
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path,"
            " created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?)",
            ("timetravel01", "Time Travel Fixture", "/music/timetravel.wav", now, now),
        )
        # The v4 backfill INSERT...SELECT ran with tracks EMPTY when the dump
        # was pinned, so write the old-shape row the way a v5 build's own
        # writer did: naming the INTEGER id implicitly, knowing no machine_id.
        conn.execute(
            "INSERT OR IGNORE INTO track_locations(stable_id, kind, role,"
            " file_path, created_at, updated_at)"
            " VALUES (?, 'local', 'primary', ?, ?, ?)",
            ("timetravel01", "/music/timetravel.wav", now, now),
        )
        old_id = conn.execute(
            "SELECT id FROM track_locations WHERE stable_id = 'timetravel01'"
        ).fetchone()
        assert old_id is not None and isinstance(old_id[0], int), (
            "fixture must exhibit the OLD shape (INTEGER id) or this test"
            " proves nothing"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def migrated(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "state" / "state.db"
    _write_v5_database(db_path)
    conn = state_db.open_rw(db_path)
    yield conn
    conn.close()


def test_old_build_database_lands_on_current_version(migrated: sqlite3.Connection) -> None:
    row = migrated.execute("SELECT MAX(version) FROM schema_meta").fetchone()
    assert row[0] == state_schema.SCHEMA_VERSION


def test_rows_survive_the_rebuild_and_the_real_accessor_reads_them(
    migrated: sqlite3.Connection,
) -> None:
    locations = list_locations(migrated, "timetravel01")
    assert len(locations) == 1, (
        "the v5 row must come through the v7 table rebuild exactly once"
    )
    loc = locations[0]
    assert loc.stable_id == "timetravel01"
    assert loc.file_path == "/music/timetravel.wav"
    assert loc.kind == "local" and loc.role == "primary"
    # The rebuild mints a TEXT location_id and the open hook stamps THIS
    # machine as owner - both were assumptions #664's code got wrong.
    assert isinstance(loc.location_id, str) and loc.location_id
    assert loc.machine_id, "backfill_local_machine_id must have claimed the row"


def test_code_written_against_the_old_id_column_fails_loudly(
    migrated: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.OperationalError, match="no such column: id"):
        migrated.execute("SELECT id FROM track_locations").fetchone()


def test_unclaimed_machine_id_is_refused_not_served(
    migrated: sqlite3.Connection,
) -> None:
    """A row that escaped the backfill must RAISE, not silently vanish.

    Exercised through the public accessor on a real migrated database, not by
    handing a fabricated tuple to the private row mapper. Codex found on #701
    that the guard was unreachable that way: `list_locations` filtered on
    `machine_id = ?`, so an unclaimed row never reached the mapper and the
    accessor returned a SHORT list that read as a complete one. The filter now
    admits `machine_id IS NULL` so the promised refusal is the real behavior.
    """
    migrated.execute(
        "UPDATE track_locations SET machine_id = NULL WHERE stable_id = ?",
        ("timetravel01",),
    )
    migrated.commit()
    orphans = migrated.execute(
        "SELECT COUNT(*) FROM track_locations WHERE machine_id IS NULL"
    ).fetchone()[0]
    assert orphans == 1, "the fixture must actually contain an unclaimed row"

    with pytest.raises(LocationError, match="machine_id"):
        list_locations(migrated, "timetravel01")
