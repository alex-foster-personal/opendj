"""Hub changelog stamp repair converges spokes past max pull seq (#3171).

[if] hub repair re-offers true stamps [then] spoke at max seq converges, [else stop].
[if] spoke is newer than repaired hub row [then] LWW keeps spoke value, [else stop].
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.shared.state import schema_markers, sync_stamp
from apps.shared.state.migrations_v17 import REPAIR_MARKER
from apps.sync_hub import client, digest_diff, engine_apply, protocol, service
from apps.sync_hub.engine_common import HUB_CHANGELOG_TABLE, LOCAL_CHANGELOG_TABLE
from apps.sync_hub.protocol_common import EPOCH
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _insert_track,
    _open,
    _sync,
)

_TestClientTransport = TestClientTransport

pytestmark = pytest.mark.requirement("CLOUDSYNC-03")


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def spoke_a(tmp_path: Path) -> Path:
    return tmp_path / "spoke-a"


@pytest.fixture
def hub(hub_dir: Path) -> Iterator[_TestClientTransport]:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield _TestClientTransport(http)

_T_HUB = "2026-08-05T20:12:14.347000+00:00"
_T_SPOKE_OLD = "2024-04-17T21:53:19.274000+00:00"
_T_SPOKE_NEWER = "2027-01-01T12:00:00.000000+00:00"
_STABLE_ID = "04ff3ae9c8b14f6a9f0d2e3b1a4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
_VENDOR = "rekordbox"


@contextmanager
def _hub_env() -> Iterator[None]:
    previous = os.environ.get("MDT_IS_HUB")
    os.environ["MDT_IS_HUB"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MDT_IS_HUB", None)
        else:
            os.environ["MDT_IS_HUB"] = previous


@contextmanager
def _spoke_env() -> Iterator[None]:
    previous = os.environ.get("MDT_IS_HUB")
    os.environ.pop("MDT_IS_HUB", None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MDT_IS_HUB", None)
        else:
            os.environ["MDT_IS_HUB"] = previous


def _seed_hub_with_epoch_changelog(
    hub_path: Path,
    *,
    field_value: str = "128.0",
    vendor_id: str = "rb-1",
) -> int:
    db_path = client.state_db_path(hub_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Stop the ladder at v16. The repair is a v17 hook, so a store seeded at
    # the top of the ladder has already been through it and marked itself
    # done; the sentinel rows inserted afterwards would never be looked at.
    # Stopping at v16 reproduces the live hub: bad rows present, upgrade
    # pending.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    state_schema._ensure_meta(conn)
    for step_idx in range(16):
        for stmt in state_schema.MIGRATIONS[step_idx]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (step_idx + 1, "2026-09-01T00:00:00+00:00"),
        )
    _insert_track(conn, _STABLE_ID, title="repair", updated_at=_T0, origin=_DEV_A)
    conn.execute(
        """
        INSERT INTO track_fields(
            stable_id, field_name, value_json, source, confidence,
            modified_at, updated_at, origin_device_id
        )
        VALUES (?, 'bpm', ?, 'rekordbox', 1.0, ?, ?, ?)
        """,
        (_STABLE_ID, field_value, _T_SPOKE_OLD, _T_HUB, _DEV_A),
    )
    conn.execute(
        """
        INSERT INTO track_vendor_ids(
            stable_id, vendor, vendor_id, updated_at, origin_device_id
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (_STABLE_ID, _VENDOR, vendor_id, _T_HUB, _DEV_A),
    )
    field_pk = sync_stamp.encode_row_pk((_STABLE_ID, "bpm"))
    vendor_pk = sync_stamp.encode_row_pk((_STABLE_ID, _VENDOR))
    received_at = "2026-09-15T10:07:28.000000+00:00"
    conn.execute(
        """
        INSERT INTO hub_changelog(
            table_name, row_pk, updated_at, origin_device_id, received_at
        )
        VALUES ('track_fields', ?, ?, '', ?)
        """,
        (field_pk, EPOCH, received_at),
    )
    conn.execute(
        """
        INSERT INTO hub_changelog(
            table_name, row_pk, updated_at, origin_device_id, received_at
        )
        VALUES ('track_vendor_ids', ?, ?, '', ?)
        """,
        (vendor_pk, EPOCH, received_at),
    )
    max_seq = int(conn.execute("SELECT MAX(seq) FROM hub_changelog").fetchone()[0])
    conn.commit()
    conn.close()
    return max_seq


def _domain_rows(conn: sqlite3.Connection, stable_id: str) -> dict[str, Any]:
    """Tracks, track_fields, and track_vendor_ids rows for one stable_id."""
    track = conn.execute(
        """
        SELECT stable_id, title, updated_at, origin_device_id, deleted_at
        FROM tracks WHERE stable_id = ?
        """,
        (stable_id,),
    ).fetchone()
    fields = conn.execute(
        """
        SELECT stable_id, field_name, value_json, modified_at, updated_at, origin_device_id
        FROM track_fields WHERE stable_id = ? ORDER BY field_name
        """,
        (stable_id,),
    ).fetchall()
    vendors = conn.execute(
        """
        SELECT stable_id, vendor, vendor_id, updated_at, origin_device_id
        FROM track_vendor_ids WHERE stable_id = ? ORDER BY vendor
        """,
        (stable_id,),
    ).fetchall()
    return {
        "track": track,
        "track_fields": [tuple(row) for row in fields],
        "track_vendor_ids": [tuple(row) for row in vendors],
    }


def _expected_converged_domain() -> dict[str, Any]:
    return {
        "track": (_STABLE_ID, "repair", _T0, _DEV_A, None),
        "track_fields": [
            (_STABLE_ID, "bpm", "128.0", _T_SPOKE_OLD, _T_HUB, _DEV_A),
        ],
        "track_vendor_ids": [
            (_STABLE_ID, _VENDOR, "rb-1", _T_HUB, _DEV_A),
        ],
    }


def _sync_state(conn: sqlite3.Connection) -> tuple[int, int]:
    row = conn.execute(
        "SELECT last_push_seq, last_pull_seq FROM sync_state WHERE peer = 'hub'"
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _seed_spoke_at_max_seq(
    spoke_path: Path,
    *,
    max_seq: int,
    field_value: str = "120.0",
    vendor_id: str = "rb-spoke",
) -> None:
    db_path = client.state_db_path(spoke_path)
    with _spoke_env():
        state_db.open_rw(db_path).close()
    conn = _open(spoke_path)
    try:
        _insert_track(conn, _STABLE_ID, title="repair", updated_at=_T0, origin=_DEV_A)
        conn.execute(
            """
            INSERT INTO track_fields(
                stable_id, field_name, value_json, source, confidence,
                modified_at, updated_at, origin_device_id
            )
            VALUES (?, 'bpm', ?, 'rekordbox', 1.0, ?, ?, ?)
            """,
            (_STABLE_ID, field_value, _T_SPOKE_OLD, _T_SPOKE_OLD, _DEV_A),
        )
        conn.execute(
            """
            INSERT INTO track_vendor_ids(
                stable_id, vendor, vendor_id, updated_at, origin_device_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (_STABLE_ID, _VENDOR, vendor_id, _T_SPOKE_OLD, _DEV_A),
        )
        conn.execute(
            """
            INSERT INTO sync_state(peer, last_push_seq, last_pull_seq, last_sync_at)
            VALUES ('hub', 0, ?, '2026-09-15T10:00:00+00:00')
            """,
            (max_seq,),
        )
        conn.commit()
    finally:
        conn.close()


def test_hub_repair_lets_spoke_past_max_seq_converge(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] spoke cursor is at hub max seq [then] repair plus sync converges,
    [else stop]."""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    max_seq = _seed_hub_with_epoch_changelog(hub_dir)
    _seed_spoke_at_max_seq(spoke_a, max_seq=max_seq)

    with _hub_env():
        state_db.open_rw(client.state_db_path(hub_dir)).close()
    hub_conn = _open(hub_dir)
    try:
        rows = hub_conn.execute(
            "SELECT seq, updated_at FROM hub_changelog "
            "WHERE table_name = 'track_fields' ORDER BY seq"
        ).fetchall()
        assert len(rows) == 2
        assert rows[-1][0] > max_seq
        assert rows[-1][1] == _T_HUB
        assert schema_markers.has_marker(hub_conn, REPAIR_MARKER)
    finally:
        hub_conn.close()

    result = _sync(spoke_a, hub, "spoke-a")
    assert result.digest_inconclusive is False
    assert result.rounds == 1

    expected = _expected_converged_domain()
    for data_dir in (spoke_a, hub_dir):
        conn = _open(data_dir)
        try:
            assert _domain_rows(conn, _STABLE_ID) == expected
        finally:
            conn.close()

    spoke_conn = _open(spoke_a)
    try:
        local_digest = protocol.sync_digest(spoke_conn)
        divergence = digest_diff.sample_divergence(
            hub,
            spoke_conn,
            result.machine_id,
            ["tracks", "track_fields", "track_vendor_ids"],
            limit=10,
        )
    finally:
        spoke_conn.close()
    hub_conn = _open(hub_dir)
    try:
        hub_digest = protocol.sync_digest(hub_conn)
    finally:
        hub_conn.close()
    assert local_digest.overall == hub_digest.overall
    assert divergence == []

    spoke_conn = _open(spoke_a)
    hub_conn = _open(hub_dir)
    try:
        spoke_before = _domain_rows(spoke_conn, _STABLE_ID)
        hub_before = _domain_rows(hub_conn, _STABLE_ID)
        spoke_sync_before = _sync_state(spoke_conn)
        hub_changelog_before = hub_conn.execute(
            f"SELECT COUNT(*) FROM {HUB_CHANGELOG_TABLE}"
        ).fetchone()[0]
        spoke_changelog_before = spoke_conn.execute(
            f"SELECT COUNT(*) FROM {LOCAL_CHANGELOG_TABLE}"
        ).fetchone()[0]
    finally:
        spoke_conn.close()
        hub_conn.close()

    quiet = _sync(spoke_a, hub, "spoke-a")
    assert quiet.digest_inconclusive is False
    assert quiet.rounds == 1
    assert quiet.pushed == 0
    assert quiet.pulled == 0
    assert quiet.applied == 0

    spoke_conn = _open(spoke_a)
    hub_conn = _open(hub_dir)
    try:
        assert _domain_rows(spoke_conn, _STABLE_ID) == spoke_before == hub_before
        assert _sync_state(spoke_conn) == spoke_sync_before
        hub_changelog_after = hub_conn.execute(
            f"SELECT COUNT(*) FROM {HUB_CHANGELOG_TABLE}"
        ).fetchone()[0]
        spoke_changelog_after = spoke_conn.execute(
            f"SELECT COUNT(*) FROM {LOCAL_CHANGELOG_TABLE}"
        ).fetchone()[0]
    finally:
        spoke_conn.close()
        hub_conn.close()
    assert hub_changelog_after == hub_changelog_before == 4
    assert spoke_changelog_after == spoke_changelog_before


def test_spoke_newer_than_repaired_hub_row_keeps_local_value(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] spoke edited after consuming sentinel [then] LWW keeps the newer
    spoke value, [else stop]."""
    monkeypatch.setenv("MDT_IS_HUB", "1")
    max_seq = _seed_hub_with_epoch_changelog(hub_dir, field_value="128.0")
    _seed_spoke_at_max_seq(spoke_a, max_seq=max_seq, field_value="120.0")

    spoke_conn = _open(spoke_a)
    try:
        conn = spoke_conn
        conn.execute(
            """
            UPDATE track_fields
            SET value_json = '140.0', updated_at = ?, origin_device_id = ?
            WHERE stable_id = ? AND field_name = 'bpm'
            """,
            (_T_SPOKE_NEWER, _DEV_A, _STABLE_ID),
        )
        conn.commit()
    finally:
        spoke_conn.close()

    with _hub_env():
        state_db.open_rw(client.state_db_path(hub_dir)).close()

    result = _sync(spoke_a, hub, "spoke-a")
    assert result.digest_inconclusive is False

    for data_dir in (spoke_a, hub_dir):
        conn = _open(data_dir)
        try:
            row = conn.execute(
                "SELECT value_json, updated_at FROM track_fields "
                "WHERE stable_id = ? AND field_name = 'bpm'",
                (_STABLE_ID,),
            ).fetchone()
            assert row == ("140.0", _T_SPOKE_NEWER)
        finally:
            conn.close()


def test_hub_apply_logs_live_domain_stamp_not_epoch(tmp_path: Path) -> None:
    """[if] hub accepts legacy track_fields row [then] changelog carries live stamp, [else stop]."""
    data_dir = tmp_path / "hub"
    data_dir.mkdir()
    state_db.open_rw(client.state_db_path(data_dir)).close()
    conn = _open(data_dir)
    try:
        _insert_track(conn, _STABLE_ID, title="legacy", updated_at=_T0, origin=_DEV_A)
        conn.commit()
        incoming = protocol.RowChange(
            table="track_fields",
            pk=(_STABLE_ID, "bpm"),
            values={
                "stable_id": _STABLE_ID,
                "field_name": "bpm",
                "value_json": "128.0",
                "source": "rekordbox",
                "confidence": 1.0,
                "modified_at": _T_SPOKE_OLD,
                "updated_at": None,
                "origin_device_id": None,
                "deleted_at": None,
            },
        )
        engine_apply.hub_apply(conn, [incoming])
        logged = conn.execute(
            "SELECT updated_at FROM hub_changelog WHERE table_name = 'track_fields'"
        ).fetchone()
        assert logged is not None
        assert logged[0] == _T_SPOKE_OLD
        assert logged[0] != EPOCH
    finally:
        conn.close()
