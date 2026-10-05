"""Tripwire continued: per-machine locations, and the swept list itself.

Split out of ``test_writer_sync_stamps.py`` (round 4 quality-gate ratchet:
that file crossed 600 lines). Same contract
(``specs/design_decision_08.md`` point 2); ``_exercise_state_writer`` and the
shared constants are imported from ``test_writer_sync_stamps`` rather than
duplicated so the two files cannot drift on what a swept writer path does.

Acceptance criteria, one assertion block each:
- if a location row is not scoped to the writing machine, availability
  computed from it counts another machine's disk (round 1 finding 7a) --
  broken.
- if probing the same file twice mints a second ``location_id`` for one
  logical row, the partial UNIQUE index wedges the push (round 1 finding 1)
  -- broken.
- if the swept table list drifts from ``protocol.DIGEST_TABLES`` -- gains an
  exclusion or loses a table -- the tripwire above is blind to whatever fell
  out of scope, exactly how round 2 finding N1 happened.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.shared.state import locations as state_locations
from apps.shared.state import sync_stamp
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.sync_hub import protocol as hub_protocol

from .test_writer_sync_stamps import _TS, SID, SYNCED_TABLES, _exercise_state_writer


def test_locations_reads_are_scoped_to_the_writing_machine(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Round 1 finding 7a: another machine's file is not availability here."""
    _exercise_state_writer(state_conn, tmp_path)
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
        "'/Users/old/Music/only-on-silver.aiff', ?, ?, 'm-silver')",
        (SID, _TS, _TS),
    )

    local = state_locations.list_locations(state_conn, SID)
    assert local, "the local machine's own rows must still be visible"
    assert {loc.machine_id for loc in local} == {mine}
    assert all(
        loc.file_path != "/Users/old/Music/only-on-silver.aiff"
        for loc in local
    )

    paths = state_locations.list_location_paths(state_conn, [SID])
    assert "/Users/old/Music/only-on-silver.aiff" not in paths[SID]

    # The fleet view is still reachable, but only by asking for it.
    remote = state_locations.list_locations(
        state_conn, SID, machine_id="m-silver",
    )
    assert [loc.file_path for loc in remote] == [
        "/Users/old/Music/only-on-silver.aiff"
    ]


def test_synced_foreign_path_does_not_become_a_local_primary(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """A cloud-synced path keeps its origin machine.

    Upserting metadata for a row whose file_path arrived from another
    machine must not stamp this machine's id on a local primary. A path
    this machine inserts still gets one.
    """
    air_path = "/Users/dev/Music/only-on-air.aiff"
    state_conn.execute(
        "INSERT INTO machines(machine_id, name, platform, is_hub, first_seen, "
        "last_seen) VALUES ('m-air', 'air', 'macos', 0, ?, ?)",
        (_TS, _TS),
    )
    foreign_id = "c" * 40
    state_conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
        "file_path, created_at, updated_at, origin_device_id) "
        "VALUES (?, 'inferred', 'Foreign', '[]', ?, ?, ?, 'm-air')",
        (foreign_id, air_path, _TS, _TS),
    )
    state_conn.commit()

    writer = StateWriter(state_conn, FakeEventBus(), actor="tripwire")
    local_audio = tmp_path / "written-here.aiff"
    local_audio.write_bytes(b"local")
    local_id = "d" * 40
    try:
        changed = writer.upsert_track(
            stable_id=foreign_id, stable_id_tier="inferred", title="Retitled",
            artists=[], album=None, isrc=None, duration_ms=None,
            file_path=air_path,
        )
        writer.upsert_track(
            stable_id=local_id, stable_id_tier="inferred", title="Local",
            artists=[], album=None, isrc=None, duration_ms=None,
            file_path=str(local_audio),
        )
    finally:
        writer.close()

    assert changed is True
    mine = sync_stamp.ensure_local_machine(state_conn)
    foreign_locals = state_conn.execute(
        "SELECT machine_id FROM track_locations WHERE stable_id = ?",
        (foreign_id,),
    ).fetchall()
    assert foreign_locals == []
    local_row = state_conn.execute(
        "SELECT machine_id, role, kind FROM track_locations WHERE stable_id = ?",
        (local_id,),
    ).fetchone()
    assert local_row == (mine, "primary", "local")


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


def test_the_swept_table_list_matches_the_protocol() -> None:
    """The sweep is the digest set, exactly. No exclusions, ever.

    Round 2 finding N1's root cause was this assertion's previous form, which
    PINNED an exclusion: ``sync_policies`` and ``playlist_pins`` were declared
    out of scope "by ownership", so the tripwire was blind to the CLOUDSYNC
    feature's own UI writing them unstamped. Ownership is not a reason a table
    can wedge the fleet less. If a new table joins the digest, it joins the
    sweep, and the exercise above must grow a writer for it -- the
    ``populated`` assertions fail loudly until it does.
    """
    assert set(SYNCED_TABLES) == set(hub_protocol.DIGEST_TABLES)
    assert len(SYNCED_TABLES) == len(hub_protocol.DIGEST_TABLES)
