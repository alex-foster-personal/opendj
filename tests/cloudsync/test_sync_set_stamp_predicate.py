"""CSSTATUS-08: fence stamp faults and normalize_stamps share one predicate.

[if] the fence reports a stamp fault [then] normalize_stamps repairs that same row, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import normalize_stamps, sync_stamp
from apps.sync_hub import client, sync_set
from apps.sync_hub.protocol_common import UPDATED_AT, stored_stamp_faults
from tests.cloudsync.test_hub_sync import _DEV_A, _T0, _T1, _log
from tests.cloudsync.test_track_identity_collapse import (
    _HASH_A,
    _insert_identified_track,
)

_LOSER = "trk-loser"
_SURVIVOR = "trk-survivor"

pytestmark = pytest.mark.requirement("CSSTATUS-08")


@pytest.mark.parametrize(
    "stored",
    [
        "2024-11-01 12:00:00",
        "2026-08-30T10:00:00.000000+00:00",
        "not a timestamp",
    ],
)
def test_stored_stamp_faults_matches_is_orderable(stored: str) -> None:
    faults = stored_stamp_faults("tracks", (UPDATED_AT,), (stored,))
    expected_fault = not sync_stamp.is_orderable(stored)
    assert bool(faults) is expected_fault


def test_fence_stamp_faults_are_exactly_normalize_stamps_repairs(
    real_library_db_unrepaired,
) -> None:
    conn = state_db.open_ro(real_library_db_unrepaired.path)
    try:
        stamp_rows = sync_set.stamp_fault_rows(conn)
        repairs = normalize_stamps.scan(conn)
        repair_keys = {(r.table, r.column, r.stored) for r in repairs}
        for row in stamp_rows:
            for fault in row.faults:
                assert (fault.table, fault.column, str(fault.value)) in repair_keys
    finally:
        conn.close()


def test_repaired_library_has_no_stamp_faults_or_repairs(real_library_db) -> None:
    conn = state_db.open_ro(real_library_db.path)
    try:
        assert sync_set.stamp_fault_rows(conn) == []
        assert normalize_stamps.scan(conn) == []
    finally:
        conn.close()


def test_parent_held_remedy_does_not_mention_normalize_stamps() -> None:
    reason = f"its tracks parent trk-1 {sync_set.PARENT_HELD_SUFFIX}"
    assert "normalize_stamps" not in sync_set.remedy_for(reason)


def test_membership_held_remedy_does_not_mention_normalize_stamps() -> None:
    reason = f"{sync_set.MEMBER_HELD_PREFIX} (tracks.updated_at = 'bad')"
    assert "normalize_stamps" not in sync_set.remedy_for(reason)


def test_hub_only_inconclusive_remedy_points_at_hub_engine_warn_log(
    real_library_db,
) -> None:
    conn = state_db.open_ro(real_library_db.path)
    try:
        remedy = sync_set.inconclusive_remedy(
            conn, held_here=0, hub_quarantined=3
        )
    finally:
        conn.close()
    assert "on this machine" not in remedy
    assert "hub engine-warn.log" in remedy
    assert "normalize_stamps --live`` for stamp faults" in remedy


def test_inconclusive_message_parent_held_only_omits_normalize_stamps(
    tmp_path: Path,
) -> None:
    conn = state_db.open_rw(client.state_db_path(tmp_path / "spoke"))
    try:
        _insert_identified_track(
            conn,
            _LOSER,
            title="older copy",
            content_hash=_HASH_A,
            updated_at=_T0,
            origin=_DEV_A,
        )
        _insert_identified_track(
            conn,
            _SURVIVOR,
            title="newer copy",
            content_hash=_HASH_A,
            updated_at=_T1,
            origin=_DEV_A,
        )
        child_stamp = _log(conn, "track_vendor_ids", (_LOSER, "rekordbox"), _DEV_A, _T0)
        conn.execute(
            """
            INSERT INTO track_vendor_ids(
                stable_id, vendor, vendor_id, updated_at, origin_device_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (_LOSER, "rekordbox", "1", child_stamp, _DEV_A),
        )
        conn.commit()
        assert sync_set.stamp_fault_rows(conn) == []
        held = sync_set.excluded_total(conn)
        assert held > 0
        by_kind = sync_set.exclusion_counts_by_kind(conn)
        assert by_kind.get("identity_dup")
        assert by_kind.get("parent_held")
        message = sync_set.format_inconclusive_exclusion_summary(
            conn, held, 0, hub_quarantined=0
        )
    finally:
        conn.close()
    assert "normalize_stamps" not in message
    assert "parent-held" in message
