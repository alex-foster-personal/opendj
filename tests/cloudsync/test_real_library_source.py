"""The real-library tier reads a read-only SNAPSHOT of a live library.

The default source is the packaged app's ``state.db``, which a running app
writes through WAL. These tests pin the two properties that make reading it
safe and honest: the snapshot carries rows that are still only in the WAL
(a byte copy would drop them), and the source's bytes never change.

Single-line intent:
  - if the snapshot drops rows still in the WAL then broken
  - if snapshotting writes to the source then broken
  - if a snapshot overwrites an existing file then broken
  - if the tier's default points outside this machine's home then broken
  - if the migrated copy is taken by byte copy instead of the snapshot then broken
  - if a default library without legacy stamps errors instead of skipping then broken
  - if a named library without legacy stamps skips instead of failing loud then broken
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.state import db as state_db

from . import real_library, real_library_premise
from .real_library_source import PACKAGED_LIBRARY_STATE_DB, snapshot_read_only

pytestmark = pytest.mark.requirement("CAT-04")

PROBE_ROWS: int = 5


@pytest.fixture
def wal_source(tmp_path: Path) -> Iterator[Path]:
    """A WAL database whose committed rows are still ONLY in the WAL.

    The writer connection stays open with autocheckpoint off, which is the
    state a running app leaves its library in between checkpoints.
    """
    source = tmp_path / "live" / "state.db"
    source.parent.mkdir()
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE probe(n INTEGER)")
        writer.executemany("INSERT INTO probe(n) VALUES (?)", [(n,) for n in range(PROBE_ROWS)])
        writer.commit()
        yield source
    finally:
        writer.close()


def _probe_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_a_snapshot_carries_rows_that_are_still_only_in_the_wal(
    wal_source: Path, tmp_path: Path
) -> None:
    """if the snapshot drops rows still in the WAL then broken"""
    byte_copy = tmp_path / "byte-copy.db"
    shutil.copyfile(wal_source, byte_copy)
    # CONTROL: the premise holds, a byte copy of state.db alone loses them.
    assert _probe_count(byte_copy) == 0

    snapshot = snapshot_read_only(wal_source, tmp_path / "snap" / "state.db")

    assert _probe_count(snapshot) == PROBE_ROWS


def test_snapshotting_leaves_the_source_bytes_untouched(wal_source: Path, tmp_path: Path) -> None:
    """if snapshotting writes to the source then broken"""
    wal = wal_source.with_name("state.db-wal")
    before = (_fingerprint(wal_source), _fingerprint(wal))

    snapshot_read_only(wal_source, tmp_path / "snap" / "state.db")

    assert (_fingerprint(wal_source), _fingerprint(wal)) == before


def test_a_snapshot_refuses_to_overwrite_an_existing_file(wal_source: Path, tmp_path: Path) -> None:
    """if a snapshot overwrites an existing file then broken"""
    target = tmp_path / "taken.db"
    target.write_bytes(b"someone else's file")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        snapshot_read_only(wal_source, target)

    assert target.read_bytes() == b"someone else's file"


def test_the_default_candidate_is_this_machines_packaged_library() -> None:
    """if the tier's default points outside this machine's home then broken"""
    assert PACKAGED_LIBRARY_STATE_DB in real_library.REAL_LIBRARY_CANDIDATES
    home = Path.home()
    outside = [
        path for path in real_library.REAL_LIBRARY_CANDIDATES if not path.is_relative_to(home)
    ]
    assert outside == [], (
        f"candidates outside {home} name another user's disk and skip everywhere else: {outside}"
    )


def test_the_migrated_copy_is_taken_through_the_read_only_snapshot(
    tmp_path: Path,
) -> None:
    """if the migrated copy is taken by byte copy instead of the snapshot then broken"""
    source = tmp_path / "library" / "state" / "state.db"
    state_db.open_rw(source).close()
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE probe(n INTEGER)")
        writer.executemany("INSERT INTO probe(n) VALUES (?)", [(n,) for n in range(PROBE_ROWS)])
        writer.commit()

        target, version = real_library._migrated_copy(source, tmp_path / "copy")

        assert version == real_library.state_schema.SCHEMA_VERSION
        assert target != source
        assert _probe_count(target) == PROBE_ROWS
    finally:
        writer.close()


def _library_without_legacy_stamps(tmp_path: Path) -> Path:
    """A real, migrated state.db holding no unorderable stamp at all."""
    source = tmp_path / "clean" / "state" / "state.db"
    state_db.open_rw(source).close()
    return source


def test_a_default_library_without_legacy_stamps_skips_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """if a default library without legacy stamps errors instead of skipping then broken"""
    monkeypatch.delenv(real_library.REAL_LIBRARY_ENV_VAR, raising=False)
    source = _library_without_legacy_stamps(tmp_path)

    with pytest.raises(pytest.skip.Exception, match="no unorderable stored stamp") as caught:
        real_library_premise.prepared_or_skip(source, tmp_path / "copy", repaired=True)

    assert str(source) in str(caught.value)


def test_an_overridden_library_without_legacy_stamps_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """if a named library without legacy stamps skips instead of failing loud then broken"""
    source = _library_without_legacy_stamps(tmp_path)
    monkeypatch.setenv(real_library.REAL_LIBRARY_ENV_VAR, str(source))

    with pytest.raises(
        real_library_premise.LibraryPremiseMissing, match="no unorderable stored stamp"
    ):
        try:
            real_library_premise.prepared_or_skip(source, tmp_path / "copy", repaired=False)
        except pytest.skip.Exception as skipped:
            # A skip is a BaseException that pytest.raises does not catch: left
            # alone it would escape and report this test SKIPPED, i.e. green.
            pytest.fail(f"a named library must fail loud, not skip: {skipped}")
