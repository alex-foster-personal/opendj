"""Migration v17: hub changelog stamp repair (#3171).

[if] v17 is applied [then] the covering changelog index exists, [else stop].
[if] latest changelog stamp disagrees with live row [then] repair re-offers once, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import schema_markers, sync_stamp
from apps.shared.state.migrations_v17 import REPAIR_MARKER, repair_hub_changelog_stamps
from apps.sync_hub.protocol_common import EPOCH

pytestmark = [pytest.mark.requirement("INFRA-01"), pytest.mark.requirement("CLOUDSYNC-03")]

_T_LIVE = "2026-08-05T20:12:14.347000+00:00"
_T_OTHER = "2024-04-17T21:53:19.274000+00:00"


def _seed_track_field(
    conn: sqlite3.Connection,
    *,
    stable_id: str = "trk-1",
    updated_at: str = _T_LIVE,
    origin: str | None = "dev-hub",
) -> None:
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, content_hash, "
        "created_at, updated_at) VALUES (?, 'inferred', 't', 'abc', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (stable_id,),
    )
    conn.execute(
        """
        INSERT INTO track_fields(
            stable_id, field_name, value_json, source, modified_at,
            updated_at, origin_device_id
        )
        VALUES (?, 'bpm', '128', 'rekordbox', ?, ?, ?)
        """,
        (stable_id, _T_OTHER, updated_at, origin),
    )


def _seed_v16_store_with_epoch_changelog(path: Path) -> None:
    """Build a store stopped at v16 holding a stamped row whose only changelog
    entry carries the EPOCH sentinel -- the shape the live hub is in.

    Stopping at v16 matters: the repair is a v17 hook, so a store already at
    v17 has been through it and is marked done. Seeding at the top of the
    ladder and inserting the bad rows afterwards tests nothing.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        state_schema._ensure_meta(conn)
        for step_idx in range(16):
            for stmt in state_schema.MIGRATIONS[step_idx]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (step_idx + 1, "2026-09-01T00:00:00+00:00"),
            )
        _seed_track_field(conn)
        conn.execute(
            """
            INSERT INTO hub_changelog(
                table_name, row_pk, updated_at, origin_device_id, received_at
            )
            VALUES ('track_fields', ?, ?, '', ?)
            """,
            (
                sync_stamp.encode_row_pk(("trk-1", "bpm")),
                EPOCH,
                "2026-09-15T10:07:28.000000+00:00",
            ),
        )
    finally:
        conn.close()


def test_fresh_ladder_reaches_v17() -> None:
    """[if] a fresh schema is applied [then] schema_meta records v17, [else stop]."""
    conn = sqlite3.connect(":memory:")
    state_schema.apply_migrations(conn)
    version = conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
    # The ladder's head, which later steps (v18 path_availability) move past 17.
    assert version == state_schema.SCHEMA_VERSION >= 17
    assert schema_markers.table_exists(conn, schema_markers.MARKER_TABLE)
    # The repair's hot lookup must be index-driven. Assert the property, not
    # the name of today's index: #3165 was an unindexed probe holding the
    # writer lock for nine minutes an open, and a SCAN here is that bug back.
    plan = " ".join(
        str(row[-1])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT updated_at FROM hub_changelog "
            "WHERE table_name = ? AND row_pk = ? ORDER BY seq DESC LIMIT 1",
            ("track_fields", sync_stamp.encode_row_pk(("trk-1", "bpm"))),
        ).fetchall()
    )
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan
    assert "SCAN hub_changelog" not in plan, plan
    conn.close()


def test_v17_upgrade_from_v15_adds_marker_table(tmp_path: Path) -> None:
    """[if] a v15 db opens after v17 lands [then] the marker table appears, [else stop]."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    try:
        for step_idx in range(15):
            for stmt in state_schema.MIGRATIONS[step_idx]:
                conn.execute(stmt)
            if step_idx + 1 == 15:
                from apps.shared.state.migrations_v15 import backfill_track_fields_stamps

                backfill_track_fields_stamps(conn, transactional=False)
            conn.execute(
                "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
                (step_idx + 1, "2026-09-01T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()

    state_db.open_rw(path).close()
    upgraded = sqlite3.connect(str(path))
    try:
        assert (
            upgraded.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0]
            == state_schema.SCHEMA_VERSION
        )
        assert schema_markers.table_exists(upgraded, schema_markers.MARKER_TABLE)
    finally:
        upgraded.close()


def test_repair_appends_fresh_seq_for_epoch_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] latest changelog carries EPOCH but live row is stamped
    [then] repair re-offers, [else stop]."""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    path = tmp_path / "state.db"
    _seed_v16_store_with_epoch_changelog(path)

    state_db.open_rw(path).close()
    repaired = sqlite3.connect(str(path))
    try:
        rows = repaired.execute(
            "SELECT seq, updated_at, origin_device_id FROM hub_changelog "
            "WHERE table_name = 'track_fields' ORDER BY seq"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][1] == EPOCH
        assert rows[1][1] == _T_LIVE
        assert rows[1][2] == "dev-hub"
        assert schema_markers.has_marker(repaired, REPAIR_MARKER)
        before = repaired.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0]
    finally:
        repaired.close()
    state_db.open_rw(path).close()
    after = sqlite3.connect(str(path))
    try:
        assert after.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0] == before
    finally:
        after.close()


def test_repair_marker_retry_after_failed_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] repair fails before marker write [then] a later open retries, [else stop]."""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    path = tmp_path / "state.db"
    _seed_v16_store_with_epoch_changelog(path)

    real_insert = schema_markers.insert_marker
    calls = {"n": 0}

    def _explode_once(conn, marker):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated marker failure")
        return real_insert(conn, marker)

    monkeypatch.setattr(schema_markers, "insert_marker", _explode_once)
    with pytest.raises(RuntimeError, match="simulated marker failure"):
        repair_hub_changelog_stamps(sqlite3.connect(str(path)))

    failed = sqlite3.connect(str(path))
    try:
        assert schema_markers.has_marker(failed, REPAIR_MARKER) is False
        assert failed.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0] == 1
    finally:
        failed.close()

    monkeypatch.setattr(schema_markers, "insert_marker", real_insert)
    state_db.open_rw(path).close()
    recovered = sqlite3.connect(str(path))
    try:
        assert recovered.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0] == 2
        assert schema_markers.has_marker(recovered, REPAIR_MARKER)
    finally:
        recovered.close()


def test_repair_skips_non_hub_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] MDT_IS_HUB is unset [then] repair is a no-op, [else stop]."""
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    path = tmp_path / "state.db"
    _seed_v16_store_with_epoch_changelog(path)

    state_db.open_rw(path).close()
    spoke = sqlite3.connect(str(path))
    try:
        assert spoke.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0] == 1
        assert schema_markers.has_marker(spoke, REPAIR_MARKER) is False
    finally:
        spoke.close()


def test_an_unreadable_hub_flag_defers_the_repair_instead_of_aborting_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] MDT_IS_HUB is illegible [then] the repair returns instead of
    raising, and waits for a legible flag, [else stop].

    Scoped to the v17 hook on purpose. The end-to-end proof that an illegible
    flag reaches the route as a declared CLOUDSYNC_HUB_FLAG_INVALID rather
    than aborting the open lives in
    tests/cloudsync/test_cloudsync_ops_routes.py; calling the repair directly
    here keeps this test about v17 and not about v15's own role read.
    """
    path = tmp_path / "state.db"
    _seed_v16_store_with_epoch_changelog(path)

    monkeypatch.setenv("MDT_IS_HUB", "yes")
    deferred = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert repair_hub_changelog_stamps(deferred) == 0   # must not raise
        assert deferred.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0] == 1
        assert schema_markers.has_marker(deferred, REPAIR_MARKER) is False
    finally:
        deferred.close()

    # Negative control for the deferral: the same store repairs as soon as the
    # flag is legible, so the skip above is a deferral and not a silent drop.
    monkeypatch.setenv("MDT_IS_HUB", "1")
    repaired = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert repair_hub_changelog_stamps(repaired) == 1
        assert repaired.execute("SELECT COUNT(*) FROM hub_changelog").fetchone()[0] == 2
        assert schema_markers.has_marker(repaired, REPAIR_MARKER)
    finally:
        repaired.close()
