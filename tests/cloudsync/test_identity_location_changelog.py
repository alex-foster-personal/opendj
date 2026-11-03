"""Identity remap must prune changelog when a twin location is discarded.

Sentry OPEN-DJ-BE-7H: after CLOUDSYNC-07 collapse, ``spoke_push`` logged
``local_changelog points at track_locations row [...] which no longer
exists``. Deletes in the sync set are soft (ADR 04 c4); this path is the
UNIQUE-collision vacuum of ``_remap_locations``. ``_drop_superseded`` and
NFD/NFC collapse already prune both changelogs; the identity location
conflict branch did not, so the next fenced offer skipped the ghost and
Sentry-captured the ERROR.

[if] remap drops a twin location [then] its changelog entries go too, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub import engine, protocol
from apps.sync_hub.engine_common import HUB_CHANGELOG_TABLE
from apps.sync_hub.engine_identity import remap_track_children
from apps.sync_hub.engine_watermark import Watermark
from tests.cloudsync.test_hub_sync import _DEV_A, _DEV_B, _T0, _T1, _open
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _INCOMING_PK,
    _STORED_PK,
    _insert_identified_track,
    _insert_location,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")

_SAME_PATH = "/music/same.mp3"
_LOSER_LOCATION = "740d3ca3ebe54259b4d3ab816b9a4200"
_SURVIVOR_LOCATION = "loc-survivor"


def _changelog_pks(conn: sqlite3.Connection, table: str, changelog: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            f"SELECT row_pk FROM {changelog} WHERE table_name = ?",
            (table,),
        )
    }


def test_twin_location_conflict_prunes_dangling_changelog(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two tracks, same path: the loser's location row is vacuumed, not offered."""
    data_dir = tmp_path / "spoke"
    conn = _open(data_dir)
    try:
        _insert_identified_track(
            conn,
            _STORED_PK,
            title="survivor",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_identified_track(
            conn,
            _INCOMING_PK,
            title="loser",
            content_hash=_HASH_A,
            updated_at=_T1,
            origin=_DEV_B,
        )
        _insert_location(
            conn,
            location_id=_SURVIVOR_LOCATION,
            stable_id=_STORED_PK,
            file_path=_SAME_PATH,
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_location(
            conn,
            location_id=_LOSER_LOCATION,
            stable_id=_INCOMING_PK,
            file_path=_SAME_PATH,
            updated_at=_T1,
            origin=_DEV_B,
        )
        conn.commit()

        remap_track_children(conn, _INCOMING_PK, _STORED_PK)
        conn.commit()

        leftover = conn.execute(
            "SELECT location_id FROM track_locations WHERE location_id = ?",
            (_LOSER_LOCATION,),
        ).fetchone()
        assert leftover is None
        kept = conn.execute(
            "SELECT location_id, stable_id FROM track_locations"
        ).fetchall()
        assert kept == [(_SURVIVOR_LOCATION, _STORED_PK)]

        encoded = protocol.encode_row_pk((_LOSER_LOCATION,))
        assert encoded not in _changelog_pks(conn, "track_locations", LOCAL_CHANGELOG_TABLE)
        assert encoded not in _changelog_pks(conn, "track_locations", HUB_CHANGELOG_TABLE)

        fence = Watermark(peer="hub", last_sync_at=_T1)
        with caplog.at_level("ERROR", logger="apps.sync_hub.engine"):
            offered = engine.spoke_push(conn, watermark=fence)
        assert not any(
            "which no longer exists" in record.getMessage()
            for record in caplog.records
        ), "fenced offer still saw a ghost track_locations changelog entry"
        offered_location_ids = {
            change.pk[0]
            for change in offered.rows
            if change.table == "track_locations"
        }
        assert _LOSER_LOCATION not in offered_location_ids
    finally:
        conn.close()
