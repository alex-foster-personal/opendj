"""hub_sync_row_page pagination (issue #3059, CSSTATUS-09 infrastructure)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync_hub import client, digest_diff, protocol, service
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _insert_playlist,
    _insert_track,
    _log,
    _open,
    _set_members,
    _sync,
)

pytestmark = pytest.mark.requirement("CSSTATUS-09")

_PL_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_PL_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_TRACK = "trk-page-test"


class _RowCounter:
    count: int = 0


class _CountingCursor:
    def __init__(self, cursor: sqlite3.Cursor, counter: _RowCounter) -> None:
        self._cursor = cursor
        self._counter = counter

    def __iter__(self) -> Iterator[sqlite3.Row]:
        for row in self._cursor:
            self._counter.count += 1
            yield row

    def __getattr__(self, name: str) -> object:
        return getattr(self._cursor, name)


class _CountingConnection:
    def __init__(self, conn: sqlite3.Connection, table: str) -> None:
        self._conn = conn
        self._table = table
        self.counter = _RowCounter()

    def execute(self, sql: str, params: object = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, params)
        normalized = " ".join(sql.split())
        if (
            f"FROM {self._table}" in normalized
            and "ORDER BY" in normalized
            and "WHERE playlist_id = ?" not in normalized
        ):
            return _CountingCursor(cursor, self.counter)  # type: ignore[return-value]
        return cursor

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


def _seed_membership_page_fixture(
    conn: sqlite3.Connection,
    playlist_id: str,
    count: int,
    *,
    include_track: bool = True,
) -> None:
    if include_track:
        _insert_track(conn, _TRACK, title="page test", updated_at=_T0, origin=_DEV_A)
    _insert_playlist(conn, playlist_id, name="page test", updated_at=_T0, origin=_DEV_A)
    stable_ids = tuple(_TRACK for _ in range(count))
    _set_members(conn, playlist_id, stable_ids, updated_at=_T0, origin=_DEV_A)


def _collect_all_pages(
    conn: sqlite3.Connection, table: str, *, page_size: int
) -> list[tuple[str, ...]]:
    collected: list[tuple[str, ...]] = []
    cursor: str | None = None
    while True:
        page, next_cursor = digest_diff.hub_sync_row_page(
            conn, table, cursor=cursor, limit=page_size
        )
        collected.extend(row.pk for row in page)
        if next_cursor is None:
            break
        cursor = next_cursor
    return collected


@pytest.mark.requirement("CSSTATUS-09")
def test_membership_pages_cover_every_position_once(tmp_path: Path) -> None:
    """[if] numeric pk spans 0..145 [then] page size 50 returns every row once."""
    data_dir = tmp_path / "hub"
    conn = _open(data_dir)
    try:
        _seed_membership_page_fixture(conn, _PL_A, 146)
        conn.commit()
        collected = _collect_all_pages(conn, "playlist_memberships", page_size=50)
        positions = {int(pk[1]) for pk in collected}
        assert len(collected) == 146
        assert len(set(collected)) == 146
        assert positions == set(range(146))
    finally:
        conn.close()


@pytest.mark.requirement("CSSTATUS-09")
def test_membership_page_resumes_strictly_after_cursor(tmp_path: Path) -> None:
    """[if] a page ends at position 49 [then] the next page starts at 50."""
    data_dir = tmp_path / "hub"
    conn = _open(data_dir)
    try:
        _seed_membership_page_fixture(conn, _PL_A, 146)
        conn.commit()
        page1, cursor1 = digest_diff.hub_sync_row_page(
            conn, "playlist_memberships", cursor=None, limit=50
        )
        assert int(page1[-1].pk[1]) == 49
        page2, _ = digest_diff.hub_sync_row_page(
            conn, "playlist_memberships", cursor=cursor1, limit=50
        )
        assert int(page2[0].pk[1]) == 50

        page_to_99, cursor_99 = digest_diff.hub_sync_row_page(
            conn, "playlist_memberships", cursor=None, limit=100
        )
        assert int(page_to_99[-1].pk[1]) == 99
        page_after_99, _ = digest_diff.hub_sync_row_page(
            conn, "playlist_memberships", cursor=cursor_99, limit=10
        )
        assert int(page_after_99[0].pk[1]) == 100
    finally:
        conn.close()


@pytest.mark.requirement("CSSTATUS-09")
def test_membership_page_seeks_from_cursor_not_row_zero(tmp_path: Path) -> None:
    """[if] cursor is past playlist A [then] scan does not revisit playlist A."""
    data_dir = tmp_path / "hub"
    conn = _open(data_dir)
    try:
        _seed_membership_page_fixture(conn, _PL_A, 500)
        _seed_membership_page_fixture(conn, _PL_B, 50, include_track=False)
        conn.commit()
        cursor_pk = (_PL_A, "499")
        cursor = protocol.encode_row_pk(cursor_pk)
        counting_conn = _CountingConnection(conn, "playlist_memberships")
        page, _ = digest_diff.hub_sync_row_page(
            counting_conn, "playlist_memberships", cursor=cursor, limit=50
        )
        assert counting_conn.counter.count <= 50
        assert all(pk[0] != _PL_A for pk in (row.pk for row in page))
        assert page
        assert page[0].pk[0] == _PL_B
    finally:
        conn.close()


@pytest.mark.requirement("CSSTATUS-09")
def test_rows_endpoint_paginates_memberships_through_digit_boundary(
    tmp_path: Path,
) -> None:
    """HTTP /rows pagination covers positions 0..145 without gaps."""
    hub_dir = tmp_path / "hub"
    spoke = tmp_path / "spoke"
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        transport = TestClientTransport(http)
        conn = _open(spoke)
        try:
            _seed_membership_page_fixture(conn, _PL_A, 146)
            conn.commit()
        finally:
            conn.close()
        _sync(spoke, transport, "spoke-a")
        conn = _open(spoke)
        try:
            machine_id = conn.execute("SELECT machine_id FROM machines LIMIT 1").fetchone()[
                0
            ]
        finally:
            conn.close()

        collected: list[list[str]] = []
        cursor: str | None = None
        while True:
            params = {
                "machine_id": machine_id,
                "table": "playlist_memberships",
                "limit": "50",
                "capabilities": "quarantine/v1",
            }
            if cursor is not None:
                params["cursor"] = cursor
            payload = transport.get("/api/v1/sync/rows", params)
            collected.extend(row["pk"] for row in payload["rows"])
            cursor = payload.get("next_cursor")
            if not cursor:
                break

        positions = {int(pk[1]) for pk in collected}
        assert len(collected) == 146
        assert positions == set(range(146))


@pytest.mark.requirement("CSSTATUS-09")
def test_track_fields_pagination_scales_linearly(tmp_path: Path) -> None:
    """~3000 track_fields rows paginate in under 5 s (hermetic scale guard)."""
    data_dir = tmp_path / "hub"
    conn = _open(data_dir)
    try:
        _insert_track(conn, _TRACK, title="scale", updated_at=_T0, origin=_DEV_A)
        conn.commit()
        for index in range(3000):
            field_name = f"field-{index:04d}"
            stamped = _log(conn, "track_fields", (_TRACK, field_name), _DEV_A, _T0)
            conn.execute(
                """
                INSERT INTO track_fields(
                    stable_id, field_name, value_json, source, modified_at,
                    updated_at, origin_device_id
                )
                VALUES (?, ?, ?, 'manual', ?, ?, ?)
                """,
                (_TRACK, field_name, "{}", _T0, stamped, _DEV_A),
            )
        conn.commit()

        started = time.monotonic()
        collected = _collect_all_pages(conn, "track_fields", page_size=500)
        elapsed = time.monotonic() - started
        assert len(collected) == 3000
        assert elapsed < 5.0
    finally:
        conn.close()
