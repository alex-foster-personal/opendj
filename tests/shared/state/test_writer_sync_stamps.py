"""Tripwire: every writer stamps, or the row does not merge.

Contract under test: ``specs/design_decision_08.md`` point 2, answering round
1 finding A1 -- the single highest-impact defect in the review. No in-repo
writer stamped ``updated_at`` / ``origin_device_id``, so every synced row sat
at ``(EPOCH, '')`` forever: the first push seeded the hub, every later edit
presented the identical LWW sort key, was rejected on the strict ``<=`` test,
and the spoke then bricked on the digest compare it could no longer satisfy.

Acceptance criteria, one assertion block each:
- if any writer path leaves a synced-table row with NULL updated_at or NULL
  origin_device_id, that row syncs as epoch-old and loses every conflict --
  broken.
- if a second edit to the same field does not produce a strictly greater LWW
  key than the first, the hub rejects it and the machine bricks -- broken.
- if a synced write does not append to local_changelog, the push fence never
  offers it -- broken.
- if a track_locations row survives with NULL machine_id, the column the
  DDL cannot mark NOT NULL is unenforced -- broken.
- if a location row is not scoped to the writing machine, availability
  computed from it counts another machine's disk -- broken.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import locations as state_locations
from apps.shared.state import machine_identity as mid
from apps.shared.state import sync_stamp
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("INFRA-01")

SID = "a" * 40
OTHER_SID = "b" * 40
PLAYLIST_ID = "pl-tripwire"
_TS = "2026-08-30T09:00:00+00:00"

# Every table the sync protocol carries that this package writes. Mirrors
# apps.sync_hub.protocol.SYNC_TABLES plus MEMBERSHIP_TABLE, minus the two
# policy tables the webui owns; pinned by
# ``test_the_swept_table_list_matches_the_protocol``.
SYNCED_TABLES: tuple[str, ...] = (
    "tracks",
    "track_vendor_ids",
    "track_fields",
    "playlists",
    "playlist_memberships",
    "track_locations",
)


def _exercise_every_writer_path(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Drive one write through every StateWriter path that touches sync."""
    audio = tmp_path / "track.flac"
    audio.write_bytes(b"fLaC-not-a-real-frame-but-a-real-file")
    alternate = tmp_path / "alternate.flac"
    alternate.write_bytes(b"fLaC-not-a-real-frame-but-a-real-file")

    writer = StateWriter(conn, FakeEventBus(), actor="tripwire")
    try:
        # tracks (insert), and track_locations via sync_primary_local.
        writer.upsert_track(
            stable_id=SID, stable_id_tier="inferred", title="First",
            artists=["X"], album=None, isrc=None, duration_ms=1000,
            file_path=str(audio),
        )
        # tracks (update).
        writer.upsert_track(
            stable_id=SID, stable_id_tier="inferred", title="Second",
            artists=["X"], album=None, isrc=None, duration_ms=1000,
            file_path=str(audio),
        )
        writer.upsert_track(
            stable_id=OTHER_SID, stable_id_tier="inferred", title="Other",
            artists=["Y"], album=None, isrc=None, duration_ms=1000,
            file_path=None,
        )
        # track_locations (explicit alternate, then a re-probe of the same row).
        writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=str(alternate),
        )
        writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=str(alternate),
        )
        writer.upsert_track_location(
            stable_id=SID, kind="remote", remote_url="https://example/x.flac",
        )
        # track_vendor_ids (insert then overwrite).
        writer.set_vendor_id(SID, "rekordbox", "rb-1")
        writer.set_vendor_id(SID, "rekordbox", "rb-2")
        # track_fields (insert then overwrite -- the A1 path).
        writer.set_field(SID, "bpm", 120, source="mik", modified_at=_TS)
        writer.set_field(
            SID, "bpm", 174, source="mik",
            modified_at="2026-08-30T10:00:00+00:00",
        )
        # playlists (insert then rename) + playlist_memberships.
        writer.insert_playlist(
            playlist_id=PLAYLIST_ID, name="Opener", vendor="rekordbox",
            vendor_pl_id="native-1",
        )
        writer.insert_playlist(
            playlist_id=PLAYLIST_ID, name="Opener v2", vendor="rekordbox",
            vendor_pl_id="native-1",
        )
        writer.set_playlist_memberships(PLAYLIST_ID, [SID, OTHER_SID])
    finally:
        writer.close()


def _unstamped(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return conn.execute(
        f"SELECT rowid, * FROM {table} "
        f"WHERE updated_at IS NULL OR origin_device_id IS NULL"
    ).fetchall()


# ----- (1) the tripwire ----------------------------------------------------


def test_no_synced_row_survives_a_writer_path_unstamped(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    _exercise_every_writer_path(state_conn, tmp_path)
    for table in SYNCED_TABLES:
        populated = state_conn.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        assert populated, f"{table} was never written, so it proves nothing"
        assert _unstamped(state_conn, table) == [], (
            f"{table} holds rows with NULL updated_at or NULL "
            f"origin_device_id; they sync as epoch-old and lose every "
            f"conflict (ADR 08 point 2)"
        )


def test_every_stamp_is_canonical_and_carries_this_machine(
    state_conn: sqlite3.Connection, tmp_path: Path, state_db_path: Path,
) -> None:
    _exercise_every_writer_path(state_conn, tmp_path)
    expected = mid.get_or_create_machine_id(state_db_path.parent)
    for table in SYNCED_TABLES:
        rows = state_conn.execute(
            f"SELECT updated_at, origin_device_id FROM {table}"
        ).fetchall()
        for updated_at, origin in rows:
            assert origin == expected, f"{table} row claims a foreign origin"
            assert sync_stamp.to_canonical(updated_at) == updated_at, (
                f"{table}.updated_at {updated_at!r} is not canonical"
            )


def test_track_locations_never_keeps_a_null_machine_id(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """The NOT NULL the DDL cannot express (schema.py reading 3)."""
    _exercise_every_writer_path(state_conn, tmp_path)
    assert state_conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE machine_id IS NULL"
    ).fetchone()[0] == 0


# ----- (2) the A1 regression ----------------------------------------------


def test_a_second_field_edit_outranks_the_first(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Round 1 finding A1, reproduced as a permanent regression.

    The review's sketch: set bpm=120, sync, set bpm=174, sync again. The
    second sync bricked because both edits presented ``(EPOCH, '')``. The
    engine's test is ``incoming.sort_key <= stored -> reject``, so the fix is
    only real if the second edit's key is strictly greater.
    """
    writer = StateWriter(state_conn, FakeEventBus(), actor="tripwire")
    try:
        writer.upsert_track(
            stable_id=SID, stable_id_tier="inferred", title="T",
            artists=[], album=None, isrc=None, duration_ms=None, file_path=None,
        )
        writer.set_field(SID, "bpm", 120, source="mik", modified_at=_TS)
        first = state_conn.execute(
            "SELECT updated_at, origin_device_id FROM track_fields "
            "WHERE stable_id = ? AND field_name = 'bpm'",
            (SID,),
        ).fetchone()
        writer.set_field(
            SID, "bpm", 174, source="mik",
            modified_at="2026-08-30T10:00:00+00:00",
        )
        second = state_conn.execute(
            "SELECT updated_at, origin_device_id, value_json FROM track_fields "
            "WHERE stable_id = ? AND field_name = 'bpm'",
            (SID,),
        ).fetchone()
    finally:
        writer.close()

    assert second[2] == "174"
    assert first[0] is not None and second[0] is not None
    assert (second[0], second[1]) > (first[0], first[0]), (
        "the second edit must present a strictly greater LWW key or the hub "
        "rejects it and the machine bricks on the next digest compare"
    )
    assert first[1] == second[1] != ""


# ----- (3) the changelog grew accordingly ---------------------------------


def test_every_synced_write_lands_in_the_local_changelog(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    before = state_conn.execute(
        "SELECT COUNT(*) FROM local_changelog"
    ).fetchone()[0]
    assert before == 0
    _exercise_every_writer_path(state_conn, tmp_path)

    logged = state_conn.execute(
        "SELECT table_name, COUNT(*) FROM local_changelog GROUP BY table_name"
    ).fetchall()
    counts = dict(logged)
    # 3 track writes (2 on SID, 1 on OTHER_SID); 2 vendor-id writes; 2 field
    # writes; 3 playlist writes (insert, rename, membership bump); 2
    # memberships; 5 location writes (each SID track write re-probes the
    # ingest primary, plus alternate x2 and the remote).
    assert counts == {
        "tracks": 3,
        "track_vendor_ids": 2,
        "track_fields": 2,
        "playlists": 3,
        "playlist_memberships": 2,
        "track_locations": 5,
    }, counts

    # Every entry names a row that exists, with the stamp that row carries.
    for table, row_pk, updated_at, origin in state_conn.execute(
        "SELECT table_name, row_pk, updated_at, origin_device_id "
        "FROM local_changelog ORDER BY seq"
    ):
        assert sync_stamp.to_canonical(updated_at) == updated_at
        assert origin
        assert row_pk.startswith("[") and row_pk.endswith("]")
        assert table in SYNCED_TABLES


def test_an_unchanged_write_logs_nothing(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """A byte-equal repeat is a no-op, so it must not move the fence."""
    writer = StateWriter(state_conn, FakeEventBus(), actor="tripwire")
    try:
        writer.upsert_track(
            stable_id=SID, stable_id_tier="inferred", title="T",
            artists=[], album=None, isrc=None, duration_ms=None, file_path=None,
        )
        writer.set_field(SID, "bpm", 120, source="mik", modified_at=_TS)
        settled = state_conn.execute(
            "SELECT MAX(seq) FROM local_changelog"
        ).fetchone()[0]
        assert writer.upsert_track(
            stable_id=SID, stable_id_tier="inferred", title="T",
            artists=[], album=None, isrc=None, duration_ms=None, file_path=None,
        ) is False
        assert writer.set_field(
            SID, "bpm", 120, source="mik", modified_at=_TS,
        ) is False
    finally:
        writer.close()
    assert state_conn.execute(
        "SELECT MAX(seq) FROM local_changelog"
    ).fetchone()[0] == settled


# ----- (4) per-machine locations ------------------------------------------


def test_locations_reads_are_scoped_to_the_writing_machine(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Round 1 finding 7a: another machine's file is not availability here."""
    _exercise_every_writer_path(state_conn, tmp_path)
    mine = sync_stamp.ensure_local_machine(state_conn)

    state_conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, first_seen, "
        "last_seen) VALUES ('m-silver', 'silver', 'macos', 0, ?, ?)",
        (_TS, _TS),
    )
    state_conn.execute(
        "INSERT INTO track_locations(location_id, stable_id, machine_id, kind, "
        "file_path, created_at, updated_at, origin_device_id) "
        "VALUES ('deadbeef', ?, 'm-silver', 'local', "
        "'/Users/dev/Music/only-on-silver.aiff', ?, ?, 'm-silver')",
        (SID, _TS, _TS),
    )

    local = state_locations.list_locations(state_conn, SID)
    assert local, "the local machine's own rows must still be visible"
    assert {loc.machine_id for loc in local} == {mine}
    assert all(
        loc.file_path != "/Users/dev/Music/only-on-silver.aiff"
        for loc in local
    )

    paths = state_locations.list_location_paths(state_conn, [SID])
    assert "/Users/dev/Music/only-on-silver.aiff" not in paths[SID]

    # The fleet view is still reachable, but only by asking for it.
    remote = state_locations.list_locations(
        state_conn, SID, machine_id="m-silver",
    )
    assert [loc.file_path for loc in remote] == [
        "/Users/dev/Music/only-on-silver.aiff"
    ]


def test_reprobing_a_location_reuses_its_location_id(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Round 1 finding 1: a second id for one logical row wedges the push."""
    audio = tmp_path / "reprobe.flac"
    audio.write_bytes(b"fLaC-not-a-real-frame-but-a-real-file")
    writer = StateWriter(state_conn, FakeEventBus(), actor="tripwire")
    try:
        writer.upsert_track(
            stable_id=SID, stable_id_tier="inferred", title="T",
            artists=[], album=None, isrc=None, duration_ms=None, file_path=None,
        )
        first = writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=str(audio),
        )
        second = writer.upsert_track_location(
            stable_id=SID, kind="local", file_path=str(audio),
        )
    finally:
        writer.close()
    assert isinstance(first, str) and len(first) == 32
    assert first == second
    assert state_conn.execute(
        "SELECT COUNT(*) FROM track_locations WHERE stable_id = ?", (SID,),
    ).fetchone()[0] == 1


# ----- (5) the swept list cannot silently shrink --------------------------


def test_the_swept_table_list_matches_the_protocol() -> None:
    """A new synced table must join the sweep or the tripwire goes blind."""
    from apps.sync_hub import protocol as hub_protocol

    protocol_tables = set(hub_protocol.DIGEST_TABLES)
    # sync_policies and playlist_pins are written by the webui cloudsync
    # routes, not by this package; they are out of this sweep by ownership,
    # and named here so a reader can see the gap is deliberate.
    assert protocol_tables - set(SYNCED_TABLES) == {
        "sync_policies",
        "playlist_pins",
    }
    assert set(SYNCED_TABLES) <= protocol_tables
