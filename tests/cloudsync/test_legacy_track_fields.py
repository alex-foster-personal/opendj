"""Legacy track_fields rows converge when only modified_at differs (#3101).

[if] legacy track_fields differ only in modified_at [then] one sync converges, [else stop].
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from apps.shared.state import db as state_db
from apps.sync_hub import client, digest_diff, engine_apply, protocol, sync_set
from apps.sync_hub import service
from apps.sync_hub.protocol_common import EPOCH, canonical_bytes, lww_key
from apps.sync_hub.transport import HttpTransport
from tests.cloudsync.conftest import free_port
from tests.cloudsync.test_hub_sync import (
    _DEV_A,
    _T0,
    _TestClientTransport,
    _insert_track,
    _open,
    _sync,
    hub,
    hub_dir,
    spoke_a,
    spoke_b,
)
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("CLOUDSYNC-03")

_T_OLD = "2024-04-17T21:53:19.274000+00:00"
_T_NEW = "2026-08-05T20:12:14.347000+00:00"
_STABLE_ID = "04ff3ae9c8b14f6a9f0d2e3b1a4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"


def _insert_legacy_field(
    conn,
    *,
    modified_at: str,
    value_json: str = "128.0",
) -> None:
    conn.execute(
        """
        INSERT INTO track_fields(
            stable_id, field_name, value_json, source, confidence,
            modified_at, updated_at, origin_device_id
        )
        VALUES (?, 'bpm', ?, 'rekordbox', 1.0, ?, NULL, NULL)
        """,
        (_STABLE_ID, value_json, modified_at),
    )


def _seed_track(data_dir: Path) -> None:
    conn = _open(data_dir)
    try:
        _insert_track(conn, _STABLE_ID, title="legacy", updated_at=_T0, origin=_DEV_A)
    finally:
        conn.close()


def test_track_fields_lww_falls_back_to_modified_at() -> None:
    """[if] updated_at is NULL on track_fields [then] modified_at orders the row, [else stop]."""
    older = lww_key(
        {"updated_at": None, "origin_device_id": None, "modified_at": _T_OLD},
        table="track_fields",
    )
    newer = lww_key(
        {"updated_at": None, "origin_device_id": None, "modified_at": _T_NEW},
        table="track_fields",
    )
    assert older < newer
    assert lww_key(
        {"updated_at": None, "origin_device_id": None},
        table="track_fields",
    ) == (EPOCH, "")


def test_equal_legacy_modified_at_remains_a_tie() -> None:
    """[if] two legacy rows share modified_at [then] lww_key ties, [else stop]."""
    left = lww_key(
        {"updated_at": None, "origin_device_id": None, "modified_at": _T_OLD},
        table="track_fields",
    )
    right = lww_key(
        {"updated_at": None, "origin_device_id": None, "modified_at": _T_OLD},
        table="track_fields",
    )
    assert left == right


def test_equivalent_modified_at_spellings_order_and_hash_equally() -> None:
    """[if] modified_at uses Z or micros spellings [then] lww and digest agree, [else stop]."""
    base = lww_key(
        {"updated_at": None, "origin_device_id": None, "modified_at": _T_OLD},
        table="track_fields",
    )
    zulu = lww_key(
        {
            "updated_at": None,
            "origin_device_id": None,
            "modified_at": "2024-04-17T21:53:19.274Z",
        },
        table="track_fields",
    )
    offset = lww_key(
        {
            "updated_at": None,
            "origin_device_id": None,
            "modified_at": "2024-04-17T22:53:19.274+01:00",
        },
        table="track_fields",
    )
    assert zulu == base
    assert offset == base

    shared = {
        "stable_id": _STABLE_ID,
        "field_name": "bpm",
        "value_json": "128.0",
        "source": "rekordbox",
        "confidence": 1.0,
        "updated_at": None,
        "origin_device_id": None,
    }
    canonical_micros = protocol.canonical_values(
        "track_fields",
        {**shared, "modified_at": _T_OLD},
    )
    canonical_zulu = protocol.canonical_values(
        "track_fields",
        {**shared, "modified_at": "2024-04-17T21:53:19.274Z"},
    )
    assert canonical_bytes(canonical_micros) == canonical_bytes(canonical_zulu)


def test_equal_legacy_modified_at_tie_rejects_incoming_at_apply(
    tmp_path: Path,
) -> None:
    """[if] stored and incoming legacy rows tie on modified_at [then] stored wins, [else stop]."""
    data_dir = tmp_path / "hub"
    data_dir.mkdir()
    state_db.open_rw(client.state_db_path(data_dir)).close()
    _seed_track(data_dir)
    conn = _open(data_dir)
    try:
        _insert_legacy_field(conn, modified_at=_T_OLD)
        conn.commit()
        spec = sync_set.spec_for("track_fields")
        incoming = protocol.RowChange(
            table="track_fields",
            pk=(_STABLE_ID, "bpm"),
            values={
                "stable_id": _STABLE_ID,
                "field_name": "bpm",
                "value_json": "999.0",
                "source": "rekordbox",
                "confidence": 1.0,
                "modified_at": "2024-04-17T21:53:19.274Z",
                "updated_at": None,
                "origin_device_id": None,
            },
        )
        resolution = engine_apply._resolve_against_stored(conn, spec, incoming)
        assert resolution.loses is True
        assert resolution.faults == ()
    finally:
        conn.close()


def test_any_stamp_fault_scans_malformed_null_updated_at_modified_at(
    tmp_path: Path,
) -> None:
    """[if] legacy track_fields has bad modified_at fallback [then] any_stamp_fault fires, [else stop]."""
    data_dir = tmp_path / "hub"
    data_dir.mkdir()
    state_db.open_rw(client.state_db_path(data_dir)).close()
    _seed_track(data_dir)
    conn = _open(data_dir)
    try:
        conn.execute(
            """
            INSERT INTO track_fields(
                stable_id, field_name, value_json, source, confidence,
                modified_at, updated_at, origin_device_id
            )
            VALUES (?, 'bpm', '128.0', 'rekordbox', 1.0, 'not-a-timestamp', NULL, NULL)
            """,
            (_STABLE_ID,),
        )
        conn.commit()
        assert sync_set.any_stamp_fault(conn) is True
    finally:
        conn.close()


def test_legacy_track_fields_converge_after_one_sync(
    hub: _TestClientTransport, hub_dir: Path, spoke_a: Path, spoke_b: Path
) -> None:
    """[if] spoke and hub hold legacy rows differing only in modified_at [then] sync converges, [else stop]."""
    _seed_track(spoke_a)
    _seed_track(spoke_b)

    conn_b = _open(spoke_b)
    try:
        _insert_legacy_field(conn_b, modified_at=_T_NEW)
        conn_b.commit()
    finally:
        conn_b.close()

    conn_a = _open(spoke_a)
    try:
        _insert_legacy_field(conn_a, modified_at=_T_OLD)
        conn_a.commit()
    finally:
        conn_a.close()

    _sync(spoke_b, hub, "spoke-b")

    result = _sync(spoke_a, hub, "spoke-a")

    for data_dir in (spoke_a, hub_dir):
        conn = _open(data_dir)
        try:
            row = conn.execute(
                "SELECT value_json, modified_at FROM track_fields "
                "WHERE stable_id = ? AND field_name = 'bpm'",
                (_STABLE_ID,),
            ).fetchone()
            assert row is not None
            assert row[0] == "128.0"
            assert row[1] == _T_NEW
        finally:
            conn.close()

    spoke_conn = _open(spoke_a)
    try:
        local_digest = protocol.sync_digest(spoke_conn)
    finally:
        spoke_conn.close()
    hub_conn = _open(hub_dir)
    try:
        hub_digest = protocol.sync_digest(hub_conn)
    finally:
        hub_conn.close()
    assert local_digest.overall == hub_digest.overall

    spoke_conn = _open(spoke_a)
    try:
        rows = digest_diff.sample_divergence(
            hub,
            spoke_conn,
            result.machine_id,
            ["track_fields"],
            limit=10,
        )
    finally:
        spoke_conn.close()
    assert rows == []


@pytest.fixture
def live_hub(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    hub_path = tmp_path / "hub"
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_path))
    app.state.sync_hub_data_dir = str(hub_path)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="legacy track_fields hub")
    try:
        yield f"http://127.0.0.1:{port}", hub_path
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_cloudsync_digest_diff_reports_no_track_fields_rows(
    live_hub: tuple[str, Path], tmp_path: Path
) -> None:
    """[if] digests agree after sync [then] digest_diff CLI prints zero rows, [else stop]."""
    hub_url, _hub_path = live_hub
    spoke_a_dir = tmp_path / "spoke-a"
    spoke_b_dir = tmp_path / "spoke-b"
    for data_dir in (spoke_a_dir, spoke_b_dir):
        data_dir.mkdir()
        state_db.open_rw(client.state_db_path(data_dir)).close()
        _seed_track(data_dir)

    conn_b = _open(spoke_b_dir)
    try:
        _insert_legacy_field(conn_b, modified_at=_T_NEW)
        conn_b.commit()
    finally:
        conn_b.close()

    conn_a = _open(spoke_a_dir)
    try:
        _insert_legacy_field(conn_a, modified_at=_T_OLD)
        conn_a.commit()
    finally:
        conn_a.close()

    transport = HttpTransport(hub_url)
    client.run_sync(spoke_b_dir, hub_url, transport=transport, name="spoke-b")
    client.run_sync(spoke_a_dir, hub_url, transport=transport, name="spoke-a")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.cloudsync_digest_diff",
            "--data-dir",
            str(spoke_a_dir),
            "--hub",
            hub_url,
            "--table",
            "track_fields",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == ""
