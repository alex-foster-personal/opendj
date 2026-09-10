"""Tripwire: every writer stamps, or the row does not merge.

Contract under test: ``specs/design_decision_08.md`` point 2, answering round
1 finding A1 -- the single highest-impact defect in that review. No in-repo
writer stamped ``updated_at`` / ``origin_device_id``, so every synced row sat
at ``(EPOCH, '')`` forever: the first push seeded the hub, every later edit
presented the identical LWW sort key, was rejected on the strict ``<=`` test,
and the spoke then bricked on the digest compare it could no longer satisfy.

**Round 3 widened this file, and that widening is the point.** Round 2 finding
N1 caught three shipped writers bypassing the chokepoint -- the CLOUDSYNC
config UI's own two endpoints, the R2 hydration path, and the Spotify importer
-- and the reason this tripwire did not catch them is that it swept
``SYNC_TABLES`` MINUS the two policy tables and drove only ``StateWriter``.
The blind spot was deliberate, documented, and cost three critical defects. So:

* the sweep set is now ``protocol.DIGEST_TABLES`` with NO exclusions, pinned
  by :func:`test_the_swept_table_list_matches_the_protocol`; and
* the exercise drives the webui cloudsync routes through a ``TestClient``, the
  Spotify importer, and the hydration writers, not just the one class.

That is why a test under ``tests/shared/state`` imports from ``apps.webui``,
``apps.spotify`` and ``apps.cloud``. The tripwire has to follow the writers,
not the package boundary; a sweep scoped to one package is exactly how N1
happened.

Acceptance criteria, one assertion block each:
- if any writer path leaves a synced-table row with NULL updated_at or NULL
  origin_device_id, that row syncs as epoch-old and loses every conflict --
  broken.
- if any row in any digest table has no local_changelog entry naming its
  primary key, the push fence never offers it and the machine fails its
  digest compare forever -- broken.
- if a second edit to the same field does not produce a strictly greater LWW
  key than the first, the hub rejects it and the machine bricks -- broken.
- if a track_locations row survives with NULL machine_id, the column the
  DDL cannot mark NOT NULL is unenforced -- broken.
- if a location row is not scoped to the writing machine, availability
  computed from it counts another machine's disk -- broken.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.cloud import hydration
from apps.shared.state import machine_identity as mid
from apps.shared.state import sync_stamp
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.spotify.client import SpotifyPlaylist, SpotifyTrack
from apps.spotify.matcher_adapter import LocalTrack, MatchedPair, MatchResult
from apps.spotify.state_writer import write_playlist_and_pending
from apps.sync_hub import protocol as hub_protocol
from apps.webui.server.app import create_app
from apps.webui.server.routes import cloudsync as cloudsync_routes
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("INFRA-01")

SID = "a" * 40
OTHER_SID = "b" * 40
PLAYLIST_ID = "pl-tripwire"
SPOTIFY_PL_ID = "tripwire-spotify"
_TS = "2026-08-30T09:00:00+00:00"

# Every table the sync protocol hashes into its digest. No exclusions: a table
# that is in the digest and not in this sweep is a table any writer can wedge
# the fleet through. Pinned by ``test_the_swept_table_list_matches_the_protocol``.
SYNCED_TABLES: tuple[str, ...] = hub_protocol.DIGEST_TABLES


# ----- the writers under sweep ---------------------------------------------


def _exercise_state_writer(conn: sqlite3.Connection, tmp_path: Path) -> None:
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


def _exercise_cloudsync_routes(db_path: Path) -> None:
    """Drive the webui's OWN policy and pin endpoints (round 2 finding N1a).

    Through the real router behind a ``TestClient``, because a unit call to
    the handler would skip the dependency that hands it its connection -- and
    the connection is where this router's machine identity comes from.
    """
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="tripwire-host",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    app.include_router(cloudsync_routes.router, prefix="/api/v1")
    with TestClient(app) as http:
        machines = http.get("/api/v1/cloudsync/machines")
        machines.raise_for_status()
        machine_id = machines.json()[0]["machine_id"]
        for asset_kind, mode in (("audio", "pinned"), ("stem_bundle", "cached")):
            put = http.put("/api/v1/cloudsync/policies", json={
                "machine_id": machine_id, "asset_kind": asset_kind,
                "mode": mode,
            })
            put.raise_for_status()
        # Upsert the same cell again: the ON CONFLICT branch must stamp too.
        http.put("/api/v1/cloudsync/policies", json={
            "machine_id": machine_id, "asset_kind": "audio", "mode": "cached",
        }).raise_for_status()
        http.put("/api/v1/cloudsync/playlist-pins", json={
            "machine_id": machine_id, "playlist_id": PLAYLIST_ID,
            "mode": "pinned",
        }).raise_for_status()


def _exercise_spotify_importer(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Drive the Spotify importer's playlists + memberships writes (N1c)."""
    matched_source = SpotifyTrack(
        spotify_id="sp-1", spotify_uri="spotify:track:sp-1", isrc=None,
        title="Matched", artists=("X",), album="A", duration_ms=1000,
        is_local=False,
    )
    unmatched_source = SpotifyTrack(
        spotify_id="sp-2", spotify_uri="spotify:track:sp-2", isrc=None,
        title="Unmatched", artists=("Y",), album="A", duration_ms=1000,
        is_local=False,
    )
    target = LocalTrack(
        stable_id=SID, isrc=None, title="Matched", artists=("X",),
        duration_ms=1000,
    )
    result = MatchResult(pairs=[
        MatchedPair(matched_source, target, 1.0, (), "matched"),
        MatchedPair(unmatched_source, None, 0.0, (), "unmatched"),
    ])
    write_playlist_and_pending(
        conn,
        SpotifyPlaylist(
            id=SPOTIFY_PL_ID, name="Spotify crate", snapshot_id="snap-1",
            owner="tripwire", description="", tracks=(),
        ),
        result,
        backup_path=tmp_path / "backup.db",
        reversal_script_path=tmp_path / "reverse.py",
    )


def _exercise_hydration_writers(
    conn: sqlite3.Connection, tmp_path: Path, machine_id: str
) -> None:
    """Drive the R2 hydration path's track_locations writes (N1b).

    The two module-private writers are called directly rather than through
    ``apply_policy_after_produce``: they ARE the writers the sweep is checking,
    and reaching them through the public entry point would drag an S3 client
    and a CloudConfig into a test about stamping. The end-to-end behaviour is
    covered by ``tests/cloudsync/test_asset_tier.py``.
    """
    now = sync_stamp.canonical_now()
    with sync_stamp.stamped_transaction(conn):
        hydration._upsert_remote_location(
            conn,
            stable_id=SID,
            object_key="assets/ab/" + "c" * 64,
            content_hash="c" * 64,
            machine_id=machine_id,
            now=now,
        )
    # Re-push of the same object: the UPDATE branch must stamp and log too.
    with sync_stamp.stamped_transaction(conn):
        hydration._upsert_remote_location(
            conn,
            stable_id=SID,
            object_key="assets/ab/" + "c" * 64,
            content_hash="c" * 64,
            machine_id=machine_id,
            now=sync_stamp.canonical_now(),
        )
    with sync_stamp.stamped_transaction(conn):
        hydration._mark_local_unavailable(
            conn, SID, tmp_path / "alternate.flac", machine_id,
            sync_stamp.canonical_now(),
        )


def _exercise_lyric_verdict_writes(
    conn: sqlite3.Connection, machine_id: str
) -> None:
    """Drive the ``lyric_verdict`` write shape (schema v10, D13.1).

    Written against ``stamped_transaction`` + ``stamp_and_log`` directly
    rather than through ``apps.lyrics.store``, for the same reason
    :func:`_exercise_hydration_writers` calls two module-private writers: it
    is the CHOKEPOINT that is under test here, and reaching it through the
    lyrics store would drag that package's validation and its verdict
    vocabulary into a test about stamping. What this pins is that a v10 row
    can only be written the stamped way -- an insert AND an update, because
    the ON CONFLICT branch is where the round 2 finding N1 writers all
    slipped through.

    ``open_rw`` connections are AUTOCOMMIT, so each write owns its own
    ``stamped_transaction`` and there is no trailing ``conn.commit()``.
    """
    with sync_stamp.stamped_transaction(conn):
        stamp = sync_stamp.stamp_and_log(conn, "lyric_verdict", (SID,), machine_id)
        conn.execute(
            "INSERT INTO lyric_verdict(stable_id, verdict, coverage_pct, "
            "source, n_words, n_lines, pct_witness_red, pipeline_version, "
            "words_content_hash, computed_at, updated_at, origin_device_id) "
            "VALUES (?, 'vocal', 91.5, 'lrclib', 240, 41, 0.012, "
            "'2026.09.09-round3a', ?, ?, ?, ?)",
            (SID, "c" * 64, _TS, stamp.updated_at, stamp.origin_device_id),
        )
    # The recompute branch: same row, new numbers, second stamp.
    with sync_stamp.stamped_transaction(conn):
        stamp = sync_stamp.stamp_and_log(conn, "lyric_verdict", (SID,), machine_id)
        conn.execute(
            "UPDATE lyric_verdict SET verdict = 'sparse', coverage_pct = 12.0, "
            "updated_at = ?, origin_device_id = ? WHERE stable_id = ?",
            (stamp.updated_at, stamp.origin_device_id, SID),
        )


def _exercise_every_writer_path(
    conn: sqlite3.Connection, tmp_path: Path, db_path: Path
) -> None:
    """Every shipped writer that touches a digest table, in one pass."""
    _exercise_state_writer(conn, tmp_path)
    machine_id = sync_stamp.ensure_local_machine(conn)
    _exercise_cloudsync_routes(db_path)
    _exercise_spotify_importer(conn, tmp_path)
    _exercise_hydration_writers(conn, tmp_path, machine_id)
    _exercise_lyric_verdict_writes(conn, machine_id)


# ----- helpers -------------------------------------------------------------


def _unstamped(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return conn.execute(
        f"SELECT rowid, * FROM {table} "
        f"WHERE updated_at IS NULL OR origin_device_id IS NULL"
    ).fetchall()


def _logged_row_pks(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT row_pk FROM local_changelog WHERE table_name = ?", (table,)
        )
    }


def _stored_row_pks(conn: sqlite3.Connection, table: str) -> set[str]:
    pk_columns = hub_protocol.pk_columns(table)
    rows = conn.execute(
        f"SELECT {', '.join(pk_columns)} FROM {table}"
    ).fetchall()
    return {sync_stamp.encode_row_pk(row) for row in rows}


# ----- (1) the tripwire ----------------------------------------------------


def test_no_synced_row_survives_a_writer_path_unstamped(
    state_conn: sqlite3.Connection, tmp_path: Path, state_db_path: Path,
) -> None:
    _exercise_every_writer_path(state_conn, tmp_path, state_db_path)
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


def test_every_stored_row_is_named_by_the_local_changelog(
    state_conn: sqlite3.Connection, tmp_path: Path, state_db_path: Path,
) -> None:
    """The widened tripwire, and the one that would have caught N1.

    Coverage, not counting: every row a writer left in a digest table must be
    named by at least one ``local_changelog`` entry. A writer that bypasses
    :func:`apps.shared.state.sync_stamp.stamp_and_log` leaves a row nothing
    points at, the push fence never offers it, and the post-sync digest
    compare fails on that table on every retry forever.
    """
    _exercise_every_writer_path(state_conn, tmp_path, state_db_path)
    for table in SYNCED_TABLES:
        stored = _stored_row_pks(state_conn, table)
        assert stored, f"{table} was never written, so it proves nothing"
        unlogged = stored - _logged_row_pks(state_conn, table)
        assert unlogged == set(), (
            f"{table} holds {len(unlogged)} row(s) with no local_changelog "
            f"entry: {sorted(unlogged)[:3]}. A writer reached this table "
            f"without going through sync_stamp.stamp_and_log, so the push "
            f"fence will never offer those rows and this machine will fail "
            f"its digest compare on {table} permanently (round 2 finding N1)."
        )


def test_the_coverage_check_catches_a_writer_that_bypasses_the_chokepoint(
    state_conn: sqlite3.Connection, tmp_path: Path, state_db_path: Path,
) -> None:
    """The tripwire proves itself, on every digest table.

    A tripwire nobody has seen trip is a tripwire nobody knows is armed --
    round 2 finding N1 existed for a whole round behind a green suite. This
    simulates the exact defect (a raw INSERT into a digest table, no
    ``stamp_and_log``) once per table and asserts the coverage check notices
    every time. It is the only test here that WANTS an unlogged row.
    """
    _exercise_every_writer_path(state_conn, tmp_path, state_db_path)
    machine_id = sync_stamp.ensure_local_machine(state_conn)
    stamp = sync_stamp.canonical_now()
    bypasses: dict[str, tuple[str, tuple[object, ...]]] = {
        "tracks": (
            "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, "
            "updated_at, origin_device_id) VALUES (?, 'inferred', 'raw', ?, ?, ?)",
            ("f" * 40, stamp, stamp, machine_id),
        ),
        "playlists": (
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
            "created_at, updated_at, origin_device_id) "
            "VALUES ('pl-raw', 'raw', 'open-dj', 'raw', ?, ?, ?)",
            (stamp, stamp, machine_id),
        ),
        "playlist_memberships": (
            "INSERT INTO playlist_memberships(playlist_id, stable_id, position, "
            "updated_at, origin_device_id) VALUES (?, ?, 99, ?, ?)",
            (PLAYLIST_ID, SID, stamp, machine_id),
        ),
        "track_vendor_ids": (
            "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id, "
            "updated_at, origin_device_id) VALUES (?, 'serato', 'raw', ?, ?)",
            (SID, stamp, machine_id),
        ),
        "track_fields": (
            "INSERT INTO track_fields(stable_id, field_name, value_json, "
            "source, modified_at, updated_at, origin_device_id) "
            "VALUES (?, 'raw_field', '1', 'mik', ?, ?, ?)",
            (SID, stamp, stamp, machine_id),
        ),
        "lyric_verdict": (
            "INSERT INTO lyric_verdict(stable_id, verdict, pipeline_version, "
            "computed_at, updated_at, origin_device_id) "
            "VALUES (?, 'unknown', 'raw', ?, ?, ?)",
            (OTHER_SID, stamp, stamp, machine_id),
        ),
        "track_locations": (
            "INSERT INTO track_locations(location_id, stable_id, machine_id, "
            "kind, file_path, created_at, updated_at, origin_device_id) "
            "VALUES ('rawlocation', ?, ?, 'local', '/raw.flac', ?, ?, ?)",
            (SID, machine_id, stamp, stamp, machine_id),
        ),
        "sync_policies": (
            "INSERT INTO sync_policies(machine_id, asset_kind, mode, "
            "updated_at, origin_device_id) "
            "VALUES (?, 'vocal_cache', 'stream', ?, ?)",
            (machine_id, stamp, machine_id),
        ),
        "playlist_pins": (
            "INSERT INTO playlist_pins(machine_id, playlist_id, mode, "
            "updated_at, origin_device_id) VALUES (?, ?, 'cached', ?, ?)",
            (machine_id, f"spotify:{SPOTIFY_PL_ID}", stamp, machine_id),
        ),
    }
    assert set(bypasses) == set(SYNCED_TABLES), (
        "a digest table with no simulated bypass here is a table this proof "
        "does not cover"
    )
    for table, (sql, params) in bypasses.items():
        state_conn.execute(sql, params)
        unlogged = _stored_row_pks(state_conn, table) - _logged_row_pks(
            state_conn, table
        )
        assert unlogged, (
            f"a raw INSERT into {table} left no unlogged row, so the coverage "
            f"check cannot detect a writer that bypasses stamp_and_log"
        )


def test_every_stamp_is_canonical_and_carries_this_machine(
    state_conn: sqlite3.Connection, tmp_path: Path, state_db_path: Path,
) -> None:
    _exercise_every_writer_path(state_conn, tmp_path, state_db_path)
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
    state_conn: sqlite3.Connection, tmp_path: Path, state_db_path: Path,
) -> None:
    """The NOT NULL the DDL cannot express (schema.py reading 3)."""
    _exercise_every_writer_path(state_conn, tmp_path, state_db_path)
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


def test_every_state_writer_write_lands_in_the_local_changelog(
    state_conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Exact counts for the one writer whose paths are enumerable here.

    The other writers are covered by
    :func:`test_every_stored_row_is_named_by_the_local_changelog`, which
    asserts coverage rather than a count -- a count would break every time an
    importer changed how many rows it touches, and would say nothing about
    whether a NEW writer bypassed the chokepoint.
    """
    before = state_conn.execute(
        "SELECT COUNT(*) FROM local_changelog"
    ).fetchone()[0]
    assert before == 0
    _exercise_state_writer(state_conn, tmp_path)

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


# ----- (4) per-machine locations, (5) the swept list ------------------------
#
# Moved to test_writer_sync_stamps_locations.py (round 4 quality-gate
# file_size ratchet: this file crossed 600 lines). It imports
# _exercise_state_writer and the shared constants back from this module.
