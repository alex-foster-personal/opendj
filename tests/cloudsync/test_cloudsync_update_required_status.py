"""Status reader tests for SYNC_WIRE_VERSION update-required state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.sync_hub import status as sync_status

_SILVER_MESSAGE = (
    'POST https://agentbox.<tailnet>:8870/api/v1/sync/hello -> HTTP 409: '
    '{"detail":{"code":"SYNC_WIRE_VERSION","message":"peer speaks sync wire v4, '
    'this machine speaks v3 (schema v14 vs v13). The synced row shapes differ, '
    'so no row may cross; upgrade whichever machine is on the lower wire version, '
    'then sync again."}}'
)


def _write_results(data_dir: Path, results: list[dict[str, object]]) -> None:
    (data_dir / sync_status.STATUS_FILENAME).write_text(
        json.dumps({"results": results}), encoding="utf-8"
    )


@pytest.mark.requirement("CSSTATUS-06")
def test_read_status_exposes_update_required_for_wire_mismatch(tmp_path: Path) -> None:
    """[if] cloudsync-status.json records SYNC_WIRE_VERSION on hello [then] update_required is typed."""
    _write_results(
        tmp_path,
        [{
            "finished_at": "2026-09-15T06:00:00+00:00",
            "status": "error",
            "message": _SILVER_MESSAGE,
            "pushed": 0,
            "pulled": 0,
        }],
    )
    current = sync_status.read_status(tmp_path)
    assert current.update_required is not None
    assert current.update_required.code == "SYNC_WIRE_VERSION"
    assert current.update_required.local_wire_version == 3
    assert current.update_required.peer_wire_version == 4
    assert current.update_required.action == "install the latest Open DJ"
    wire = current.to_wire()
    assert wire["update_required"]["local_wire_version"] == 3
    assert wire["update_required"]["peer_wire_version"] == 4


def test_read_status_exposes_update_required_for_sync_version_mismatch(tmp_path: Path) -> None:
    message = (
        "SyncVersionMismatch: peer speaks sync wire v4, this machine speaks v3 "
        "(schema v14 vs v13)."
    )
    _write_results(
        tmp_path,
        [{
            "finished_at": "2026-09-15T06:00:00+00:00",
            "status": "error",
            "message": message,
            "pushed": 0,
            "pulled": 0,
        }],
    )
    current = sync_status.read_status(tmp_path)
    assert current.update_required is not None
    assert current.update_required.local_wire_version == 3
    assert current.update_required.peer_wire_version == 4


def test_update_required_clears_after_ok_result(tmp_path: Path) -> None:
    _write_results(
        tmp_path,
        [
            {
                "finished_at": "2026-09-15T06:01:00+00:00",
                "status": "ok",
                "message": "completed",
                "pushed": 1,
                "pulled": 2,
            },
            {
                "finished_at": "2026-09-15T06:00:00+00:00",
                "status": "error",
                "message": _SILVER_MESSAGE,
                "pushed": 0,
                "pulled": 0,
            },
        ],
    )
    current = sync_status.read_status(tmp_path)
    assert current.update_required is None


def test_connection_refused_error_has_no_update_required(tmp_path: Path) -> None:
    _write_results(
        tmp_path,
        [{
            "finished_at": "2026-09-15T06:00:00+00:00",
            "status": "error",
            "message": "[Errno 111] Connection refused",
            "pushed": 0,
            "pulled": 0,
        }],
    )
    current = sync_status.read_status(tmp_path)
    assert current.update_required is None
