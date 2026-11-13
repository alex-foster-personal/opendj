"""Edge sampler rekordbox loop reader: read-only, absent and unreadable states, loop mapping.

[if] the sampler reads rekordbox loops wrongly [then] these tests fail, [else stop].

Regression one-liners:
  - if reading loops changes the rekordbox master DB file then broken
  - if a missing rekordbox DB raises instead of reporting state absent then broken
  - if a non-sqlite master DB raises instead of reporting state unreadable then broken
  - if a broken query on a real database is swallowed then broken
  - if a hot loop is not its bank letter, a memory loop not None, or a point cue listed then broken
  - if a soft-deleted vendor mapping or track still gets loops then broken
  - if a changed database file is served from the cache then broken
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server import sampler_rekordbox as rb
from apps.webui.server.sampler_rekordbox import RbLoop

VID, OTHER_VID = "900", "901"
SID, OTHER_SID = "a" * 40, "b" * 40


def _master(path: Path, cues: list[tuple[str, int, int, int, int]]) -> Path:
    """A rekordbox master DB holding (ContentID, Kind, InMsec, OutMsec, rb_local_deleted) cues."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, FolderPath VARCHAR(255))")
    conn.execute(
        "CREATE TABLE djmdCue (ID VARCHAR(255) PRIMARY KEY, ContentID VARCHAR(255), InMsec INTEGER, InFrame INTEGER, "
        "InMpegFrame INTEGER, InMpegAbs INTEGER, OutMsec INTEGER, OutFrame INTEGER, Kind INTEGER, Color INTEGER, "
        "ColorTableIndex INTEGER, ActiveLoop INTEGER, Comment VARCHAR(255), BeatLoopSize INTEGER, "
        "rb_local_deleted TINYINT(1) DEFAULT 0, created_at DATETIME, updated_at DATETIME)"
    )
    conn.executemany("INSERT INTO djmdContent (ID) VALUES (?)", [(VID,), (OTHER_VID,)])
    conn.executemany(
        "INSERT INTO djmdCue (ID, ContentID, Kind, InMsec, OutMsec, rb_local_deleted, ActiveLoop) "
        "VALUES (?,?,?,?,?,?,0)",
        [(str(i), *cue) for i, cue in enumerate(cues)],
    )
    conn.commit()
    conn.close()
    return path


def _state(path: Path) -> sqlite3.Connection:
    """A migrated state.db: SID mapped to VID, OTHER_SID's mapping to OTHER_VID soft-deleted."""
    conn = state_db.open_rw(path)
    for sid in (SID, OTHER_SID):
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, created_at, updated_at) "
            "VALUES (?, 'inferred', 240000, '2026-01-01', '2026-01-01')",
            (sid,),
        )
    conn.execute("INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', ?)", (SID, VID))
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id, deleted_at) "
        "VALUES (?, 'rekordbox', ?, '2026-01-02')",
        (OTHER_SID, OTHER_VID),
    )
    conn.commit()
    return conn


# A hot loop on A (Kind 1), a hot loop on C (Kind 3), a memory loop (Kind 0), a point cue on B
# (OutMsec 0), a deleted hot loop, an unmapped Kind 40, and a hot loop of the other track.
CUES = [
    (VID, 1, 50_000, 52_000, 0),
    (VID, 3, 40_010, 42_000, 0),
    (VID, 0, 60_000, 64_000, 0),
    (VID, 2, 70_000, 0, 0),
    (VID, 4, 80_000, 84_000, 1),
    (VID, 40, 90_000, 94_000, 0),
    (OTHER_VID, 1, 1_000, 3_000, 0),
]


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    rb.reset_cache()
    yield
    rb.reset_cache()


@pytest.fixture
def master(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = _master(tmp_path / "master.plain.db", CUES)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", path)
    return path


def test_if_loops_are_read_then_the_master_db_file_is_unchanged(master: Path) -> None:
    """[if] rekordbox loops are read [then] the master DB bytes and mtime are unchanged, [else stop]."""
    before = (hashlib.sha256(master.read_bytes()).hexdigest(), master.stat().st_mtime_ns)
    assert rb.loop_cues().state == "ok"
    assert (hashlib.sha256(master.read_bytes()).hexdigest(), master.stat().st_mtime_ns) == before


def test_if_rekordbox_loops_are_read_then_hot_loops_carry_their_letter_and_memory_loops_none(master: Path) -> None:
    """[if] cues are read [then] hot loops map to A..H, memory to None, point and deleted cues drop, [else stop]."""
    loops = rb.loop_cues()
    assert (loops.state, loops.status, loops.reason) == ("ok", "ok", None)
    assert dict(loops.by_vendor_id) == {
        VID: (RbLoop("C", 40_010, 42_000), RbLoop("A", 50_000, 52_000), RbLoop(None, 60_000, 64_000)),
        OTHER_VID: (RbLoop("A", 1_000, 3_000),),
    }


def test_if_the_master_db_is_absent_then_state_is_absent_with_no_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] MASTER_PLAIN_DB does not exist [then] state absent, no loops, status names the path, [else stop]."""
    missing = tmp_path / "absent.db"
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", missing)
    loops = rb.loop_cues()
    assert (loops.state, dict(loops.by_vendor_id)) == ("absent", {})
    assert loops.status == f"unavailable: rekordbox database not found at {missing}"


def test_if_the_master_db_is_not_sqlite_then_state_is_unreadable_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the master DB is not sqlite (still encrypted) [then] state unreadable with the reason, [else stop]."""
    garbage = tmp_path / "master.plain.db"
    garbage.write_bytes(os.urandom(4096))
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", garbage)
    loops = rb.loop_cues()
    assert (loops.state, dict(loops.by_vendor_id)) == ("unreadable", {})
    assert loops.status.startswith("unavailable: rekordbox database master.plain.db is not a readable sqlite file: ")


def test_if_the_master_db_lacks_the_cue_table_then_the_error_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a real sqlite master DB has no djmdCue table [then] OperationalError is raised, [else stop]."""
    empty = tmp_path / "master.plain.db"
    sqlite3.connect(empty).execute("CREATE TABLE djmdContent (ID TEXT)").connection.close()
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", empty)
    with pytest.raises(sqlite3.OperationalError, match="djmdCue"):
        rb.loop_cues()


def test_if_loops_are_mapped_then_only_live_vendor_mappings_get_them(master: Path, tmp_path: Path) -> None:
    """[if] one mapping is live and one soft-deleted [then] only the live track gets its loops, [else stop]."""
    conn = _state(tmp_path / "state.db")
    try:
        assert rb.loops_by_stable_id(conn, rb.loop_cues()) == {
            SID: (RbLoop("C", 40_010, 42_000), RbLoop("A", 50_000, 52_000), RbLoop(None, 60_000, 64_000)),
        }
        assert rb.loops_by_stable_id(conn, rb.RbLoops("absent", {VID: (RbLoop("A", 0, 1),)}, "x")) == {}
    finally:
        conn.close()


def test_if_the_master_db_changes_then_the_cache_reads_it_again(master: Path) -> None:
    """[if] the master DB gains a loop after a cached read [then] the next read lists it, [else stop]."""
    assert len(rb.loop_cues().by_vendor_id[VID]) == 3
    conn = sqlite3.connect(master)
    conn.execute("INSERT INTO djmdCue (ID, ContentID, Kind, InMsec, OutMsec) VALUES ('n', ?, 5, 10000, 12000)", (VID,))
    conn.commit()
    conn.close()
    # A one-row insert can keep the file size, and a write inside the same clock tick as the
    # cached read keeps the mtime too (seen on a Linux CI seat). rekordbox writes seconds
    # after a read, so move the mtime on by a second, as a real later write would.
    stat = master.stat()
    os.utime(master, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert rb.loop_cues().by_vendor_id[VID][0] == RbLoop("E", 10_000, 12_000)
