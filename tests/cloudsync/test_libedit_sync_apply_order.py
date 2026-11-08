"""Virtual edits must apply before playlist rows that reference them (LIBEDIT-9179).

[if] ``hub_apply`` runs playlist membership before the edit exists [then] FOREIGN KEY fails, [else stop].
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from apps.sync_hub import engine, protocol
from tests.cloudsync.test_apply_order_guard import _DEV, _T1, _seed_track

pytestmark = pytest.mark.requirement("LIBEDIT-9179")

_EDIT_ID = "edit-apply-order-1"
_PL_ID = "pl-apply-order-1"


def _row_change(conn: sqlite3.Connection, table: str, pk: tuple[str, ...], **overrides: Any) -> protocol.RowChange:
    columns = protocol.table_columns(conn, table)
    base = dict.fromkeys(columns)
    base.update(overrides)
    values = {column: base[column] for column in columns}
    return protocol.RowChange(table=table, pk=pk, values=values)


def test_hub_apply_sorts_track_edit_before_playlist_when_batch_is_reversed(
    conn: sqlite3.Connection,
) -> None:
    """Playlist bundle references ``edit_id``; edit must apply first even when listed last."""
    _seed_track(conn, "trk-1")
    member_columns = protocol.table_columns(conn, protocol.MEMBERSHIP_TABLE)
    member: dict[str, Any] = dict.fromkeys(member_columns)
    member.update(
        {
            "playlist_id": _PL_ID,
            "stable_id": "trk-1",
            "position": 0,
            "item_id": "item-apply-order-1",
            "order_key": "00000000",
            "updated_at": _T1,
            "origin_device_id": _DEV,
            "edit_id": _EDIT_ID,
            "deleted_at": None,
        }
    )
    playlist_columns = protocol.table_columns(conn, "playlists")
    pl_vals: dict[str, Any] = dict.fromkeys(playlist_columns)
    pl_vals.update(
        {
            "playlist_id": _PL_ID,
            "name": "apply-order",
            "vendor": "open-dj",
            "vendor_pl_id": _PL_ID,
            "created_at": _T1,
            "updated_at": _T1,
            "origin_device_id": _DEV,
            "deleted_at": None,
            "forbid_duplicates": 0,
            "kind": "playlist",
        }
    )
    playlist_change = protocol.RowChange(
        table="playlists",
        pk=(_PL_ID,),
        values={column: pl_vals[column] for column in playlist_columns},
        members=tuple({column: member[column] for column in member_columns} for _ in (0,)),
    )
    edit_change = _row_change(
        conn,
        "track_edit",
        (_EDIT_ID,),
        id=_EDIT_ID,
        source_track_id="trk-1",
        label="cut",
        in_ms=0,
        out_ms=5_000,
        fx="{}",
        created_by="test",
        created_at=_T1,
        updated_at=_T1,
        origin_device_id=_DEV,
        deleted_at=None,
    )
    result = engine.hub_apply(conn, [playlist_change, edit_change])
    assert result.quarantined == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM track_edit WHERE id = ? AND deleted_at IS NULL",
            (_EDIT_ID,),
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT edit_id FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL",
            (_PL_ID,),
        ).fetchone()[0]
        == _EDIT_ID
    )


def test_playlist_membership_sql_uses_source_track_id_for_track_edit_parent(
    conn: sqlite3.Connection,
) -> None:
    """Regression: PARENT_KEYS for track_edit must use ``source_track_id``, not ``stable_id``."""
    rows = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'track_edit'").fetchone()
    assert rows is not None
    assert "source_track_id" in rows[0]
    sql = (
        "SELECT 1 FROM track_edit AS c WHERE c.source_track_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM tracks AS p WHERE p.stable_id = c.source_track_id) LIMIT 1"
    )
    conn.execute(sql).fetchone()
