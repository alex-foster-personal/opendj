"""CLOUDSYNC-07: cross-machine track identity collapse at hub apply.

Two independently ingested libraries must identify the same audio as one
``tracks`` row. Merge identity is ``content_hash`` first, then a
normalizable ISRC, then a fingerprint-tier ``stable_id``. Path-tier
(``inferred``) ``stable_id`` is never a merge key.

Acceptance, one test each:
- same ``content_hash``, different ``stable_id`` -> one tracks row and both
  machines' ``track_locations``.
- share only a path-tier ``stable_id`` or a local ``file_path`` -> not the
  same track.
- same ``content_hash``, disagreeing normalizable ISRC -> both quarantined.
- first-sync while either side still has live inferred-tier rows with no
  ``content_hash`` and no ISRC -> those rows stay local; identity-bearing
  rows still sync.
- surviving row wins -> locations, fields, vendor ids, and playlist
  memberships that pointed at the loser now point at the survivor.

[if] two machines ingest one audio under different PKs [then] the hub holds one row, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from apps.shared.state import db as state_db
from apps.sync_hub import client, engine, protocol
from apps.sync_hub.engine_identity import (
    SyncIdentityPreflightError,
    assert_identity_ready,
    assert_merge_safe,
    hub_library_size,
)
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _DEV_B,
    _T0,
    _T1,
    _insert_playlist,
    _log,
    _set_members,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_ISRC_US = "USRC17607839"
_ISRC_GB = "GBUM71800001"
_STORED_PK = "trk-silver"
_INCOMING_PK = "trk-air"


def _open_hub(tmp_path: Path) -> sqlite3.Connection:
    return state_db.open_rw(client.state_db_path(tmp_path / "hub"))


def _values(conn: sqlite3.Connection, table: str, **overrides: object) -> dict[str, Any]:
    """A complete wire row for ``table``, columns from PRAGMA table_info."""
    columns = protocol.table_columns(conn, table)
    values: dict[str, Any] = dict.fromkeys(columns)
    values.update(overrides)
    return {column: values[column] for column in columns}


def _insert_identified_track(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    title: str,
    updated_at: str,
    origin: str,
    content_hash: str | None = None,
    audio_hash: str | None = None,
    isrc: str | None = None,
    file_path: str | None = None,
    tier: str = "inferred",
    deleted_at: str | None = None,
) -> None:
    stamped = _log(conn, "tracks", (stable_id,), origin, updated_at)
    conn.execute(
        """
        INSERT INTO tracks(
            stable_id, stable_id_tier, title, isrc, file_path, content_hash, audio_hash,
            created_at, updated_at, origin_device_id, deleted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stable_id,
            tier,
            title,
            isrc,
            file_path,
            content_hash,
            audio_hash,
            _T0,
            stamped,
            origin,
            deleted_at,
        ),
    )


def _incoming_track(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    title: str,
    updated_at: str,
    content_hash: str | None = None,
    audio_hash: str | None = None,
    isrc: str | None = None,
    file_path: str | None = None,
    tier: str = "inferred",
) -> protocol.RowChange:
    return protocol.RowChange(
        table="tracks",
        pk=(stable_id,),
        values=_values(
            conn,
            "tracks",
            stable_id=stable_id,
            stable_id_tier=tier,
            title=title,
            isrc=isrc,
            file_path=file_path,
            content_hash=content_hash,
            audio_hash=audio_hash,
            created_at=_T0,
            updated_at=updated_at,
            origin_device_id=_DEV_B,
            deleted_at=None,
        ),
    )


def _incoming_location(
    conn: sqlite3.Connection,
    *,
    location_id: str,
    stable_id: str,
    file_path: str,
    updated_at: str,
) -> protocol.RowChange:
    return protocol.RowChange(
        table="track_locations",
        pk=(location_id,),
        values=_values(
            conn,
            "track_locations",
            location_id=location_id,
            stable_id=stable_id,
            machine_id=None,
            kind="local",
            role="primary",
            file_path=file_path,
            available=0,
            created_at=_T0,
            updated_at=updated_at,
            origin_device_id=_DEV_B,
        ),
    )


def _insert_location(
    conn: sqlite3.Connection,
    *,
    location_id: str,
    stable_id: str,
    file_path: str,
    updated_at: str,
    origin: str,
) -> None:
    stamped = _log(conn, "track_locations", (location_id,), origin, updated_at)
    conn.execute(
        "INSERT INTO track_locations("
        "location_id, stable_id, kind, role, file_path, available, "
        "created_at, updated_at, origin_device_id) "
        "VALUES (?, ?, 'local', 'primary', ?, 0, ?, ?, ?)",
        (location_id, stable_id, file_path, _T0, stamped, origin),
    )


def _insert_field(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    field_name: str,
    value_json: str,
    updated_at: str,
    origin: str,
) -> None:
    stamped = _log(conn, "track_fields", (stable_id, field_name), origin, updated_at)
    conn.execute(
        """
        INSERT INTO track_fields(
            stable_id, field_name, value_json, source, modified_at,
            updated_at, origin_device_id
        )
        VALUES (?, ?, ?, 'manual', ?, ?, ?)
        """,
        (stable_id, field_name, value_json, _T0, stamped, origin),
    )


def _insert_vendor(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    vendor: str,
    vendor_id: str,
    updated_at: str,
    origin: str,
) -> None:
    stamped = _log(conn, "track_vendor_ids", (stable_id, vendor), origin, updated_at)
    conn.execute(
        """
        INSERT INTO track_vendor_ids(
            stable_id, vendor, vendor_id, updated_at, origin_device_id
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (stable_id, vendor, vendor_id, stamped, origin),
    )


def _track_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT stable_id FROM tracks WHERE deleted_at IS NULL"
        )
    }


def _location_ids(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT stable_id, file_path FROM track_locations "
            "WHERE deleted_at IS NULL"
        )
    }


# ----- (1) same hash, different PK ------------------------------------------


def test_same_content_hash_different_stable_id_collapses_to_one_track(
    tmp_path: Path,
) -> None:
    """The live silver-then-Air case: same bytes, two inferred PKs.

    Without collapse the hub would hold both rows. With it, one survivor
    and both machines' locations.
    """
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            _STORED_PK,
            title="silver copy",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
            file_path="/Silver/a.mp3",
        )
        _insert_location(
            conn,
            location_id="loc-silver",
            stable_id=_STORED_PK,
            file_path="/Silver/a.mp3",
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.commit()

        result = engine.hub_apply(
            conn,
            [
                _incoming_track(
                    conn,
                    _INCOMING_PK,
                    title="air copy",
                    content_hash=_HASH_A,
                    updated_at=_T1,
                    file_path="/Air/a.mp3",
                ),
                _incoming_location(
                    conn,
                    location_id="loc-air",
                    stable_id=_INCOMING_PK,
                    file_path="/Air/a.mp3",
                    updated_at=_T1,
                ),
            ],
        )

        assert result.quarantined == 0
        assert _track_ids(conn) == {_INCOMING_PK}, (
            "same content_hash under two PKs must collapse to one tracks row"
        )
        assert _location_ids(conn) == {
            (_INCOMING_PK, "/Silver/a.mp3"),
            (_INCOMING_PK, "/Air/a.mp3"),
        }, "both machines' locations must sit on the survivor"
    finally:
        conn.close()


def test_same_audio_hash_different_content_hash_collapses_to_one_track(
    tmp_path: Path,
) -> None:
    """Retagged copies merge on audio_hash while content_hash stays distinct."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn, _STORED_PK, title="before", content_hash=_HASH_A,
            audio_hash="c" * 64, updated_at=_T0, origin=_DEV_A,
        )
        conn.commit()
        result = engine.hub_apply(conn, [_incoming_track(
            conn, _INCOMING_PK, title="after", content_hash=_HASH_B,
            audio_hash="c" * 64, updated_at=_T1,
        )])
        assert result.quarantined == 0
        assert _track_ids(conn) == {_INCOMING_PK}
    finally:
        conn.close()


# ----- (2) path-tier is not a merge key -------------------------------------


def test_path_tier_stable_id_or_file_path_is_not_a_merge_key(
    tmp_path: Path,
) -> None:
    """Inferred ids and local paths are machine-local. They must not merge."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            _STORED_PK,
            title="silver",
            updated_at=_T0,
            origin=_DEV_A,
            file_path="/Music/same-name.mp3",
        )
        conn.commit()

        result = engine.hub_apply(
            conn,
            [
                _incoming_track(
                    conn,
                    _INCOMING_PK,
                    title="air",
                    updated_at=_T1,
                    file_path="/Music/same-name.mp3",
                )
            ],
        )

        assert result.accepted == 1
        assert result.quarantined == 0
        assert _track_ids(conn) == {_STORED_PK, _INCOMING_PK}, (
            "sharing only a path-tier id or a local file_path is not identity"
        )
    finally:
        conn.close()


# ----- (3) identity-signal conflict -----------------------------------------


def test_same_content_hash_disagreeing_isrc_quarantines_both(
    tmp_path: Path,
) -> None:
    """Same bytes claiming two ISRCs: hold both, collapse neither."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            _STORED_PK,
            title="silver",
            content_hash=_HASH_A,
            isrc=_ISRC_US,
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.commit()

        result = engine.hub_apply(
            conn,
            [
                _incoming_track(
                    conn,
                    _INCOMING_PK,
                    title="air",
                    content_hash=_HASH_A,
                    isrc=_ISRC_GB,
                    updated_at=_T1,
                )
            ],
        )

        assert result.quarantined >= 1
        assert result.identity_conflicts >= 1
        assert _track_ids(conn) == {_STORED_PK}, (
            "disagreeing ISRC on one hash must not insert a second tracks row"
        )
        assert conn.execute(
            "SELECT title FROM tracks WHERE stable_id = ?", (_STORED_PK,)
        ).fetchone()[0] == "silver", "the local row must be untouched"
    finally:
        conn.close()


# ----- (4) first-sync preflight ---------------------------------------------


def test_first_sync_preflight_refuses_inferred_without_hash_or_isrc(
    tmp_path: Path,
) -> None:
    """A PK-only merge of unsyncable inferred rows would duplicate audio."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            "trk-unsyncable",
            title="no identity",
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.commit()
        with pytest.raises(SyncIdentityPreflightError, match="content_hash"):
            assert_identity_ready(conn)
        assert _track_ids(conn) == {"trk-unsyncable"}
    finally:
        conn.close()


def test_preflight_allows_inferred_rows_that_carry_hash_or_isrc(
    tmp_path: Path,
) -> None:
    """The control: identity-bearing inferred rows are syncable."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            "trk-hashed",
            title="hashed",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_identified_track(
            conn,
            "trk-isrc",
            title="isrc",
            isrc=_ISRC_US,
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.commit()
        assert_identity_ready(conn)
    finally:
        conn.close()


def _hub_app(hub_dir: Path):  # type: ignore[no-untyped-def]
    """A hub service bound to ``hub_dir``, as the live gate tests drive it."""
    from fastapi import FastAPI

    from apps.sync_hub import service

    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    return app


def test_run_sync_holds_unidentifiable_inferred_off_an_empty_hub(
    tmp_path: Path,
) -> None:
    """Unidentifiable inferred rows reach an empty hub as hash_pending."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"
    spoke_conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _insert_identified_track(
            spoke_conn,
            "trk-unsyncable",
            title="no identity",
            updated_at=_T0,
            origin=_DEV_A,
        )
        spoke_conn.commit()
    finally:
        spoke_conn.close()

    state_db.open_rw(client.state_db_path(hub_dir)).close()

    from fastapi.testclient import TestClient

    from tests.cloudsync.test_hub_sync import _TestClientTransport

    with TestClient(_hub_app(hub_dir)) as http:
        client.run_sync(
            spoke, "http://hub.invalid", transport=_TestClientTransport(http), name="spoke"
        )

    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        assert _track_ids(hub_after) == {"trk-unsyncable"}
    finally:
        hub_after.close()
    spoke_after = state_db.open_rw(client.state_db_path(spoke))
    try:
        assert _track_ids(spoke_after) == {"trk-unsyncable"}
    finally:
        spoke_after.close()


def test_run_sync_second_library_does_not_push_unidentifiable_rows(
    tmp_path: Path,
) -> None:
    """A second machine's unidentifiable rows land on the hub as hash_pending."""
    spoke_a = tmp_path / "spoke-a"
    spoke = tmp_path / "spoke-b"
    hub_dir = tmp_path / "hub"
    spoke_a_conn = state_db.open_rw(client.state_db_path(spoke_a))
    try:
        _insert_identified_track(
            spoke_a_conn,
            "trk-a-seeded",
            title="seeded by A",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        spoke_a_conn.commit()
    finally:
        spoke_a_conn.close()

    state_db.open_rw(client.state_db_path(hub_dir)).close()

    from fastapi.testclient import TestClient

    from tests.cloudsync.test_hub_sync import _TestClientTransport

    with TestClient(_hub_app(hub_dir)) as http:
        client.run_sync(
            spoke_a,
            "http://hub.invalid",
            transport=_TestClientTransport(http),
            name="spoke-a",
        )

    spoke_conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _insert_identified_track(
            spoke_conn,
            "trk-b-unsyncable",
            title="no identity",
            updated_at=_T0,
            origin=_DEV_B,
        )
        spoke_conn.commit()
    finally:
        spoke_conn.close()

    with TestClient(_hub_app(hub_dir)) as http:
        client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=_TestClientTransport(http),
            name="spoke-b",
        )

    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        assert _track_ids(hub_after) == {"trk-a-seeded", "trk-b-unsyncable"}
    finally:
        hub_after.close()


# ----- (4b) the seed/merge distinction, in isolation -------------------------


def test_merge_safe_refuses_only_a_first_sync_into_a_populated_hub(
    tmp_path: Path,
) -> None:
    """ADR-0068: hash_pending rows no longer trigger merge_safe refusal."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn, "trk-x", title="no identity", updated_at=_T0, origin=_DEV_A
        )
        conn.commit()
        assert_merge_safe(conn, hub_library_rows=None, first_sync=True)
        assert_merge_safe(conn, hub_library_rows=0, first_sync=True)
        assert_merge_safe(conn, hub_library_rows=9194, first_sync=False)
        assert_merge_safe(conn, hub_library_rows=9194, first_sync=True)
    finally:
        conn.close()


def test_hub_library_size_counts_live_rows_whatever_authored_them(
    tmp_path: Path,
) -> None:
    """A tombstoned row is not a library, and an UNATTRIBUTED row still is --
    the case that broke an origin-based check on the real library."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn, "trk-a", title="a", updated_at=_T0, origin=_DEV_A
        )
        _insert_identified_track(
            conn,
            "trk-b-dead",
            title="b",
            updated_at=_T0,
            origin=_DEV_B,
            deleted_at=_T0,
        )
        conn.commit()
        assert hub_library_size(conn) == 1
        # An unattributed row is still a library: this is what every row of a
        # migrated library looks like.
        conn.execute("UPDATE tracks SET origin_device_id = NULL")
        conn.commit()
        assert hub_library_size(conn) == 1
        # Negative control: a hub with no live tracks counts zero, which is
        # what lets a seed through.
        conn.execute("UPDATE tracks SET deleted_at = ?", (_T0,))
        conn.commit()
        assert hub_library_size(conn) == 0
    finally:
        conn.close()


# ----- (5) children remap onto the survivor ---------------------------------


def test_surviving_row_remaps_locations_fields_vendor_ids_and_memberships(
    tmp_path: Path,
) -> None:
    """Children of the loser must not keep naming a dropped stable_id."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            _STORED_PK,
            title="silver",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_location(
            conn,
            location_id="loc-silver",
            stable_id=_STORED_PK,
            file_path="/Silver/a.mp3",
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_field(
            conn,
            _STORED_PK,
            field_name="rating",
            value_json="5",
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_vendor(
            conn,
            _STORED_PK,
            vendor="rekordbox",
            vendor_id="rb-1",
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_playlist(
            conn, "pl-1", name="crate", updated_at=_T0, origin=_DEV_A
        )
        _set_members(
            conn, "pl-1", (_STORED_PK,), updated_at=_T0, origin=_DEV_A
        )
        conn.commit()

        result = engine.hub_apply(
            conn,
            [
                _incoming_track(
                    conn,
                    _INCOMING_PK,
                    title="air",
                    content_hash=_HASH_A,
                    updated_at=_T1,
                )
            ],
        )

        assert result.quarantined == 0
        assert _track_ids(conn) == {_INCOMING_PK}
        assert conn.execute(
            "SELECT stable_id FROM track_locations WHERE location_id = 'loc-silver'"
        ).fetchone()[0] == _INCOMING_PK
        assert conn.execute(
            "SELECT stable_id FROM track_fields WHERE field_name = 'rating'"
        ).fetchone()[0] == _INCOMING_PK
        assert conn.execute(
            "SELECT stable_id FROM track_vendor_ids WHERE vendor = 'rekordbox'"
        ).fetchone()[0] == _INCOMING_PK
        assert conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id = 'pl-1'"
        ).fetchone()[0] == _INCOMING_PK
        leftover = conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE stable_id = ?", (_STORED_PK,)
        ).fetchone()[0]
        assert leftover == 0
    finally:
        conn.close()


def test_isrc_merges_when_content_hash_is_absent(tmp_path: Path) -> None:
    """Second rung of the ladder: a shared normalizable ISRC is identity."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            _STORED_PK,
            title="silver",
            isrc=_ISRC_US,
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.commit()

        result = engine.hub_apply(
            conn,
            [
                _incoming_track(
                    conn,
                    _INCOMING_PK,
                    title="air",
                    isrc=_ISRC_US,
                    updated_at=_T1,
                )
            ],
        )

        assert result.quarantined == 0
        assert _track_ids(conn) == {_INCOMING_PK}
        assert conn.execute(
            "SELECT title FROM tracks WHERE stable_id = ?", (_INCOMING_PK,)
        ).fetchone()[0] == "air"
    finally:
        conn.close()
