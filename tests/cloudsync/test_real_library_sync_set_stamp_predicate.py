"""CSSTATUS-08 acceptance on the packaged real library (slow tier).

[if] the fence reports a stamp fault [then] normalize_stamps repairs that same row, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import normalize_stamps
from apps.sync_hub import sync_set

pytestmark = pytest.mark.requirement("CSSTATUS-08")


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
