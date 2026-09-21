"""Empty-spoke first-sync must not abort on child rows whose parents
land in a later pull chunk (or never land).

Live shape (Sat 12 Sep 2026, agentbox hub): pull windows by hub_changelog
seq, then offers LIVE child rows. A playlist logged at seq 1860 is offered
with members whose tracks logged at seq 5000+. A track_locations row
logged early can name a track logged later (identity collapse / retarget).
The spoke applies that chunk with foreign_keys ON and raises
SyncApplyError, stopping the drain. First-sync of an empty machine cannot
complete. Playlist-only buffering is not enough: nucbox first-sync still
died on track_locations after that landed.

Acceptance:
- if a playlist bundle names a track this machine does not have yet, the
  playlist row still applies and the missing member is skipped -- broken if
  the whole sync aborts.
- if the same bundle names a track that IS here, that member lands -- a
  skip-everything pass would satisfy the first check and delete playlists.
- if a later pull chunk carries the missing track, buffering ALL rows
  until the drain finishes lands the member rather than freezing an
  incomplete bundle behind the pull watermark.
- if a track_locations row in chunk one names a track in chunk two,
  buffering all rows then apply_rank lands the location -- broken if
  per-chunk apply raises FOREIGN KEY.

[if] empty first-sync dies on a child row [then] fail, [else stop].
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import sync_stamp
from apps.sync_hub import client, engine, protocol, service
from apps.sync_hub.engine_common import SyncApplyError

from .test_hub_sync import (
    _DEV_A,
    _T0,
    _T1,
    _T2,
    _insert_playlist,
    _insert_track,
    _log,
    _members,
    _open,
    _set_members,
    _sync,
    _TestClientTransport,
)

_LOC_ID = "cccccccccccccccccccccccccccccccc"


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def spoke_b(tmp_path: Path) -> Path:
    return tmp_path / "spoke-b"


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[_TestClientTransport]:
    hub_dir = tmp_path / "hub"
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)


pytestmark = pytest.mark.requirement("CLOUDSYNC-02")


def _playlist_change(
    conn,
    playlist_id: str,
    *,
    name: str,
    members: tuple[str, ...],
    updated_at: str,
    origin: str,
) -> protocol.RowChange:
    """A playlists RowChange whose columns match this DB, members included."""
    playlist_columns = protocol.table_columns(conn, "playlists")
    values = dict.fromkeys(playlist_columns)
    values.update(
        {
            "playlist_id": playlist_id,
            "name": name,
            "vendor": "open-dj",
            "vendor_pl_id": playlist_id,
            "created_at": _T0,
            "updated_at": updated_at,
            "origin_device_id": origin,
            "deleted_at": None,
            "forbid_duplicates": 0,
        }
    )
    member_columns = protocol.table_columns(conn, protocol.MEMBERSHIP_TABLE)
    bundle: list[dict] = []
    for position, stable_id in enumerate(members):
        row = dict.fromkeys(member_columns)
        row.update(
            {
                "playlist_id": playlist_id,
                "stable_id": stable_id,
                "position": position,
                "updated_at": updated_at,
                "origin_device_id": origin,
                "item_id": f"item-{position}",
                "order_key": f"{position:08d}",
            }
        )
        bundle.append({column: row[column] for column in member_columns})
    return protocol.RowChange(
        table="playlists",
        pk=(playlist_id,),
        values={column: values[column] for column in playlist_columns},
        members=tuple(bundle),
    )


def test_spoke_apply_skips_members_whose_track_is_not_here_yet(
    tmp_path: Path,
) -> None:
    """A missing member must not abort the playlist or the rest of the batch."""
    conn = _open(tmp_path / "spoke")
    try:
        _insert_track(conn, "trk-here", title="here", updated_at=_T0, origin=_DEV_A)
        conn.commit()
        change = _playlist_change(
            conn,
            "pl-1",
            name="mixed",
            members=("trk-here", "trk-never"),
            updated_at=_T1,
            origin=_DEV_A,
        )
        result = engine.spoke_apply(conn, [change])
        assert result.accepted == 1
        assert _members(conn, "pl-1") == ("trk-here",)
        assert conn.execute(
            "SELECT name FROM playlists WHERE playlist_id = 'pl-1'"
        ).fetchone()[0] == "mixed"
    finally:
        conn.close()


def test_spoke_apply_keeps_every_member_whose_track_is_here(
    tmp_path: Path,
) -> None:
    """Skip-everything would pass the missing-member test and empty playlists."""
    conn = _open(tmp_path / "spoke")
    try:
        _insert_track(conn, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _insert_track(conn, "trk-2", title="two", updated_at=_T0, origin=_DEV_A)
        conn.commit()
        change = _playlist_change(
            conn,
            "pl-1",
            name="full",
            members=("trk-1", "trk-2"),
            updated_at=_T1,
            origin=_DEV_A,
        )
        result = engine.spoke_apply(conn, [change])
        assert result.accepted == 1
        assert _members(conn, "pl-1") == ("trk-1", "trk-2")
    finally:
        conn.close()


def test_first_sync_pull_lands_members_whose_tracks_are_in_a_later_chunk(
    hub: _TestClientTransport,
    spoke_a: Path,
    spoke_b: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playlist seq in chunk one, member track seq in chunk two.

    Hub offers LIVE members on the early playlist changelog row, so the
    late track is named before it has been pulled. Buffering playlists
    until the drain finishes is what makes the member land.
    """
    conn = _open(spoke_a)
    try:
        _insert_track(conn, "trk-1", title="one", updated_at=_T0, origin=_DEV_A)
        _insert_track(conn, "trk-2", title="two", updated_at=_T0, origin=_DEV_A)
        _insert_playlist(conn, "pl-1", name="late-member", updated_at=_T1, origin=_DEV_A)
        _set_members(conn, "pl-1", ("trk-1",), updated_at=_T1, origin=_DEV_A)
    finally:
        conn.close()
    _sync(spoke_a, hub, "spoke-a")

    conn = _open(spoke_a)
    try:
        _insert_track(conn, "trk-3", title="three", updated_at=_T2, origin=_DEV_A)
        _set_members(conn, "pl-1", ("trk-1", "trk-3"), updated_at=_T2, origin=_DEV_A)
    finally:
        conn.close()
    _sync(spoke_a, hub, "spoke-a")

    monkeypatch.setattr(client, "PULL_LIMIT", 3)
    try:
        _sync(spoke_b, hub, "spoke-b")
    except SyncApplyError as exc:
        pytest.fail(
            "empty-spoke first-sync aborted on a playlist member whose track "
            f"was in a later pull chunk: {exc}"
        )

    conn = _open(spoke_b)
    try:
        assert _members(conn, "pl-1") == ("trk-1", "trk-3"), (
            "buffering the pull drain until it finishes must land the "
            "member whose track arrived in a later chunk"
        )
    finally:
        conn.close()


def _insert_location(
    conn,
    *,
    location_id: str,
    stable_id: str,
    machine_id: str,
    file_path: str,
    updated_at: str,
    origin: str,
) -> None:
    stamped = _log(conn, "track_locations", (location_id,), origin, updated_at)
    conn.execute(
        "INSERT INTO track_locations("
        "location_id, stable_id, machine_id, kind, role, file_path, "
        "available, created_at, updated_at, origin_device_id) "
        "VALUES (?, ?, ?, 'local', 'primary', ?, 0, ?, ?, ?)",
        (location_id, stable_id, machine_id, file_path, _T0, stamped, origin),
    )


def _retarget_location(
    conn,
    *,
    location_id: str,
    stable_id: str,
    updated_at: str,
    origin: str,
) -> None:
    stamped = _log(conn, "track_locations", (location_id,), origin, updated_at)
    conn.execute(
        "UPDATE track_locations SET stable_id = ?, updated_at = ?, "
        "origin_device_id = ? WHERE location_id = ?",
        (stable_id, stamped, origin, location_id),
    )


def test_first_sync_pull_lands_locations_whose_tracks_are_in_a_later_chunk(
    hub: _TestClientTransport,
    spoke_a: Path,
    spoke_b: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Location seq in chunk one, parent track seq in chunk two.

    Hub offers the LIVE location on the early changelog row, so the late
    track is named before it has been pulled. Buffering every row until
    the drain finishes is what makes apply_rank land the parent first.
    """
    conn = _open(spoke_a)
    try:
        machine_id = sync_stamp.ensure_local_machine(conn)
        _insert_track(conn, "trk-loc-1", title="early", updated_at=_T0, origin=_DEV_A)
        _insert_location(
            conn,
            location_id=_LOC_ID,
            stable_id="trk-loc-1",
            machine_id=machine_id,
            file_path="/tmp/early.mp3",
            updated_at=_T0,
            origin=_DEV_A,
        )
    finally:
        conn.close()
    _sync(spoke_a, hub, "spoke-a")

    conn = _open(spoke_a)
    try:
        _insert_track(conn, "trk-loc-2", title="late", updated_at=_T2, origin=_DEV_A)
        _retarget_location(
            conn,
            location_id=_LOC_ID,
            stable_id="trk-loc-2",
            updated_at=_T2,
            origin=_DEV_A,
        )
    finally:
        conn.close()
    _sync(spoke_a, hub, "spoke-a")

    monkeypatch.setattr(client, "PULL_LIMIT", 1)
    try:
        _sync(spoke_b, hub, "spoke-b")
    except SyncApplyError as exc:
        pytest.fail(
            "empty-spoke first-sync aborted on a track_locations row whose "
            f"parent track was in a later pull chunk: {exc}"
        )

    conn = _open(spoke_b)
    try:
        row = conn.execute(
            "SELECT stable_id FROM track_locations WHERE location_id = ?",
            (_LOC_ID,),
        ).fetchone()
        assert row is not None, "location must land on the empty spoke"
        assert row[0] == "trk-loc-2", (
            "buffering the pull drain must land the location on the track "
            "that arrived in a later chunk"
        )
        landed = {
            str(item[0])
            for item in conn.execute("SELECT stable_id FROM tracks")
        }
        assert {"trk-loc-1", "trk-loc-2"} <= landed
    finally:
        conn.close()
