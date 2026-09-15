"""CLOUDSYNC-07 hold-back and cross-batch identity remap.

Unidentifiable inferred-tier tracks stay local so they cannot mint a
path-tier PK the hub cannot collapse. Identity-bearing rows still sync.
A collapse remap has to outlive one ``hub_apply`` batch: first-sync splits
tracks and playlists across HTTP requests (``PUSH_BATCH_ROWS`` is 200).

[if] an unidentifiable track has a multi-batch remap [then] it holds local, lands, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.sync_hub import client, engine, protocol, sync_set
from apps.sync_hub.engine_identity_map import (
    REMAP_TABLE,
    load_identity_remap,
    prepare_spoke_identity,
)
from tests.cloudsync.test_hub_sync import _DEV_A, _DEV_B, _T0, _T1, _TestClientTransport
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _hub_app,
    _insert_identified_track,
    _insert_location,
    _open_hub,
    _track_ids,
    _values,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")

_LOSER = "trk-silver-dup"
_SURVIVOR = "trk-silver-keep"
_PLAYLIST = "pl-afro-latin"


def _incoming_playlist(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    name: str,
    members: tuple[str, ...],
    updated_at: str,
    origin: str,
) -> protocol.RowChange:
    values = _values(
        conn,
        "playlists",
        playlist_id=playlist_id,
        name=name,
        vendor="open-dj",
        vendor_pl_id=playlist_id,
        created_at=_T0,
        updated_at=updated_at,
        origin_device_id=origin,
        deleted_at=None,
        forbid_duplicates=0,
    )
    member_columns = protocol.table_columns(conn, protocol.MEMBERSHIP_TABLE)
    bundle: list[dict[str, Any]] = []
    for position, stable_id in enumerate(members):
        row: dict[str, Any] = dict.fromkeys(member_columns)
        row.update(
            {
                "playlist_id": playlist_id,
                "stable_id": stable_id,
                "position": position,
                "updated_at": updated_at,
                "origin_device_id": origin,
            }
        )
        bundle.append({column: row[column] for column in member_columns})
    return protocol.RowChange(
        table="playlists",
        pk=(playlist_id,),
        values=values,
        members=tuple(bundle),
    )


def _membership_ids(conn: sqlite3.Connection, playlist_id: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT stable_id FROM playlist_memberships "
            "WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        )
    ]


def test_later_apply_batch_rewrites_playlist_onto_persisted_survivor(
    tmp_path: Path,
) -> None:
    """The live 409: tracks collapse in batch N, a playlist in batch N+1
    still names the loser PK. Persist the remap or FOREIGN KEY fires."""
    conn = _open_hub(tmp_path)
    try:
        _insert_identified_track(
            conn,
            _LOSER,
            title="silver loser",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
            file_path="/Silver/a.mp3",
        )
        conn.commit()

        first = engine.hub_apply(
            conn,
            [
                protocol.RowChange(
                    table="tracks",
                    pk=(_SURVIVOR,),
                    values=_values(
                        conn,
                        "tracks",
                        stable_id=_SURVIVOR,
                        stable_id_tier="inferred",
                        title="silver keep",
                        content_hash=_HASH_A,
                        file_path="/Silver/b.mp3",
                        created_at=_T0,
                        updated_at=_T1,
                        origin_device_id=_DEV_A,
                        deleted_at=None,
                    ),
                )
            ],
        )
        assert first.quarantined == 0
        assert _track_ids(conn) == {_SURVIVOR}
        assert load_identity_remap(conn)[_LOSER] == _SURVIVOR

        second = engine.hub_apply(
            conn,
            [
                _incoming_playlist(
                    conn,
                    playlist_id=_PLAYLIST,
                    name="WFP Afro Latin",
                    members=(_LOSER,),
                    updated_at=_T1,
                    origin=_DEV_A,
                )
            ],
        )
        assert second.quarantined == 0, (
            "a later batch must apply the playlist by rewriting the loser "
            f"PK; got quarantined={second.quarantined} faults={second.faults}"
        )
        assert _membership_ids(conn, _PLAYLIST) == [_SURVIVOR]
        assert conn.execute(
            f"SELECT 1 FROM {REMAP_TABLE} WHERE loser_pk = ?", (_LOSER,)
        ).fetchone() is not None
    finally:
        conn.close()


def test_prepare_spoke_identity_remaps_children_and_holds_the_loser(
    tmp_path: Path,
) -> None:
    """Intra-library duplicate hashes: remap children onto the LWW survivor
    before the offer, keep the loser row, hold it out of the sync set."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn,
            _LOSER,
            title="older copy",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
            file_path="/Silver/a.mp3",
        )
        _insert_identified_track(
            conn,
            _SURVIVOR,
            title="newer copy",
            content_hash=_HASH_A,
            updated_at=_T1,
            origin=_DEV_A,
            file_path="/Silver/b.mp3",
        )
        _insert_location(
            conn,
            location_id="loc-loser",
            stable_id=_LOSER,
            file_path="/Silver/a.mp3",
            updated_at=_T0,
            origin=_DEV_A,
        )
        conn.execute(
            """
            INSERT INTO playlists(
                playlist_id, name, vendor, vendor_pl_id, created_at,
                updated_at, origin_device_id
            )
            VALUES (?, 'dups', 'open-dj', ?, ?, ?, ?)
            """,
            (_PLAYLIST, _PLAYLIST, _T0, _T1, _DEV_A),
        )
        conn.execute(
            """
            INSERT INTO playlist_memberships(
                playlist_id, stable_id, position, updated_at, origin_device_id
            )
            VALUES (?, ?, 0, ?, ?)
            """,
            (_PLAYLIST, _LOSER, _T1, _DEV_A),
        )
        conn.commit()

        assert prepare_spoke_identity(conn) == 1
        conn.commit()
        assert _track_ids(conn) == {_LOSER, _SURVIVOR}
        assert _membership_ids(conn, _PLAYLIST) == [_SURVIVOR]
        assert conn.execute(
            "SELECT stable_id FROM track_locations WHERE location_id = ?",
            ("loc-loser",),
        ).fetchone()[0] == _SURVIVOR

        held = sync_set.HeldKeys(conn)
        columns = sync_set.deciding_columns(conn, "tracks")
        loser_row = conn.execute(
            f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id = ?",
            (_LOSER,),
        ).fetchone()
        spec = sync_set.spec_for("tracks")
        assert sync_set.row_reason(
            "tracks", columns, loser_row, spec, held
        ) == sync_set.IDENTITY_DUP_REASON
        assert not sync_set.any_stamp_fault(conn)
        assert sync_set.excluded_counts(conn).get("tracks", 0) >= 1
    finally:
        conn.close()


def test_count_unsyncable_inferred_matches_the_hold_predicate(tmp_path: Path) -> None:
    """The admin-panel backlog number must count exactly what excludes a
    ``tracks`` row for identity, no more and no less (CLOUDSYNC-16:
    the maintainer has never seen CloudSync converge, and this backlog -- not a code
    bug -- is the dominant reason on a real library)."""
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        # Held: inferred tier, no hash, no ISRC.
        _insert_identified_track(
            conn, "trk-unsyncable-a", title="a", updated_at=_T0, origin=_DEV_A
        )
        _insert_identified_track(
            conn, "trk-unsyncable-b", title="b", updated_at=_T0, origin=_DEV_A
        )
        # Free: has a content hash.
        _insert_identified_track(
            conn,
            "trk-hashed",
            title="hashed",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        # Free: no hash, but a normalizable ISRC.
        _insert_identified_track(
            conn,
            "trk-isrc",
            title="isrc",
            isrc="US-S1Z-99-00001",
            updated_at=_T0,
            origin=_DEV_A,
        )
        # Free: not inferred tier.
        _insert_identified_track(
            conn, "trk-vendor-tier", title="vendor", updated_at=_T0, origin=_DEV_A, tier="isrc"
        )
        # Excluded from the count: soft-deleted.
        _insert_identified_track(
            conn,
            "trk-deleted",
            title="deleted",
            updated_at=_T0,
            origin=_DEV_A,
            deleted_at=_T0,
        )
        conn.commit()

        assert sync_set.count_unsyncable_inferred(conn) == 2

        held = sync_set.HeldKeys(conn)
        columns = sync_set.deciding_columns(conn, "tracks")
        spec = sync_set.spec_for("tracks")
        held_reasons = 0
        for stable_id in ("trk-unsyncable-a", "trk-unsyncable-b"):
            row = conn.execute(
                f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            reason = sync_set.row_reason("tracks", columns, row, spec, held)
            if reason == sync_set.IDENTITY_HOLD_REASON:
                held_reasons += 1
        assert held_reasons == 2, (
            "count_unsyncable_inferred must count exactly the rows "
            "row_reason holds for IDENTITY_HOLD_REASON, not an approximation"
        )
    finally:
        conn.close()


def test_run_sync_holds_unidentifiable_inferred_and_still_syncs_identity(
    tmp_path: Path,
) -> None:
    """The live first-sync shape: 7330 missing files must not deadlock the
    1864 identity-bearing rows, and must not land as path-tier PKs on the hub."""
    spoke = tmp_path / "spoke"
    hub_dir = tmp_path / "hub"
    spoke_conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _insert_identified_track(
            spoke_conn,
            "trk-hashed",
            title="hashed",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
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

    with TestClient(_hub_app(hub_dir)) as http:
        result = client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=_TestClientTransport(http),
            name="spoke",
        )

    assert result.pushed >= 1, (
        "identity-bearing rows must still be offered; a silent empty "
        f"push would also not raise (pushed={result.pushed})"
    )
    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        assert _track_ids(hub_after) == {"trk-hashed"}
    finally:
        hub_after.close()
    spoke_after = state_db.open_rw(client.state_db_path(spoke))
    try:
        assert _track_ids(spoke_after) == {"trk-hashed", "trk-unsyncable"}
    finally:
        spoke_after.close()


def test_second_library_collapses_hashed_rows_and_holds_unidentifiable(
    tmp_path: Path,
) -> None:
    """Air after silver: hashed overlap collapses; missing-file rows stay on Air."""
    spoke = tmp_path / "spoke-b"
    hub_dir = tmp_path / "hub"
    spoke_conn = state_db.open_rw(client.state_db_path(spoke))
    try:
        _insert_identified_track(
            spoke_conn,
            "trk-b-hashed",
            title="air copy",
            content_hash=_HASH_A,
            updated_at=_T1,
            origin=_DEV_B,
        )
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

    hub_conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        _insert_identified_track(
            hub_conn,
            "trk-a-seeded",
            title="seeded by A",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        hub_conn.commit()
    finally:
        hub_conn.close()

    with TestClient(_hub_app(hub_dir)) as http:
        client.run_sync(
            spoke,
            "http://hub.invalid",
            transport=_TestClientTransport(http),
            name="spoke-b",
        )

    hub_after = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        ids = _track_ids(hub_after)
        assert "trk-b-unsyncable" not in ids
        assert len(ids) == 1, (
            "hashed overlap must collapse to one tracks row, unidentifiable "
            f"must stay off the hub; hub holds {ids}"
        )
        assert ids <= {"trk-a-seeded", "trk-b-hashed"}
    finally:
        hub_after.close()
