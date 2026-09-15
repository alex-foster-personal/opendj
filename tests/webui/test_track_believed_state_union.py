"""Track rows union active transfers with stale locations (CLOUDSYNC-10).

[if] track rows union active transfers with stale locations [then] missing files stay unavailable while transfers are active, [else stop].
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.cloud import transfer_status
from apps.engine_core.jobs.store import JobStore
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp
from apps.webui.server import backend
from apps.webui.server.rb_vendor_pkg import track_rows

pytestmark = pytest.mark.requirement("CLOUDSYNC-10")

SID = "a" * 40


def test_active_transfer_with_missing_file_keeps_file_exists_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
            "created_at, updated_at) VALUES (?, 'inferred', 't', 180000, 't0', 't0')",
            (SID,),
        )
        machine_id = sync_stamp.ensure_local_machine(conn)
        conn.execute(
            "INSERT INTO track_locations(stable_id, machine_id, kind, role, remote_url, "
            "available, created_at, updated_at) "
            "VALUES (?, ?, 'remote', 'alternate', 'assets/ab/cd', 1, 't0', 't0')",
            (SID, machine_id),
        )
        conn.commit()
    finally:
        conn.close()

    jobs_store = JobStore(tmp_path / "jobs.db", boot_id="boot", owner_pid=os.getpid())
    jobs_store.recover()
    jobs_store.enqueue(
        "cloud.hydrate",
        payload={
            "stable_id": SID,
            "asset_kind": "audio",
            "machine_id": "m1",
            "data_dir": str(tmp_path),
        },
    )

    monkeypatch.setattr(track_rows.config, "STATE_DB", db_path)
    track = backend.Track(stable_id=SID)
    monkeypatch.setattr(track_rows, "bulk_rb_meta", lambda _ids: {})
    monkeypatch.setattr(
        track_rows, "bulk_availability", lambda *_a, **_k: {SID: False}
    )
    monkeypatch.setattr(track_rows, "bulk_quality", lambda *_a, **_k: {SID: {}})
    monkeypatch.setattr(track_rows, "bulk_stem_summaries", lambda _ids: {SID: {}})

    token = transfer_status.begin_transfer(SID, "download", bytes_total=100)
    transfer_status.update_transfer(SID, token, 25)
    try:
        rows = track_rows.build_track_rows([track], jobs_store=jobs_store)
    finally:
        transfer_status.clear_transfer(SID, token)

    assert len(rows) == 1
    row = rows[0]
    assert row["file_exists"] is False
    assert row["has_remote_copy"] is True
    assert row["cloud_transfer"] == {
        "direction": "download",
        "bytes_transferred": 25,
        "bytes_total": 100,
    }
