"""Empty-spoke first-sync must not abort on playlist members whose tracks
land in a later pull chunk (or never land).

Live shape (Sat 12 Sep 2026, agentbox hub): every playlist_memberships
stable_id exists on hub tracks, but pull windows by hub_changelog seq.
A playlist logged at seq 1860 is offered with its LIVE member list, which
includes tracks logged at seq 5000+. The spoke applies that chunk with
foreign_keys ON and raises SyncApplyError, rolling back the playlist and
stopping the drain. First-sync of an empty machine cannot complete.

Acceptance:
- if a playlist bundle names a track this machine does not have yet, the
  playlist row still applies and the missing member is skipped -- broken if
  the whole sync aborts.
- if the same bundle names a track that IS here, that member lands -- a
  skip-everything pass would satisfy the first check and delete playlists.
- if a later pull chunk carries the missing track, buffering playlists
  until the drain finishes lands the member rather than freezing an
  incomplete bundle behind the pull watermark.

[if] empty first-sync dies on a playlist member [then] fail, [else stop].
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync_hub import client, engine, protocol, service
from apps.sync_hub.engine_common import SyncApplyError

from .test_hub_sync import (
    _DEV_A,
    _T0,
    _T1,
    _T2,
    _TestClientTransport,
    _insert_playlist,
    _insert_track,
    _members,
    _open,
    _set_members,
    _sync,
)


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
            "buffering playlists until the pull drain finishes must land the "
            "member whose track arrived in a later chunk"
        )
    finally:
        conn.close()
