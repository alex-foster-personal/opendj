"""Parser tests for SYNC_WIRE_VERSION update-required status."""

from __future__ import annotations

import pytest

from apps.sync_hub import wire_version

_SILVER_MESSAGE = (
    'POST https://agentbox.<tailnet>:8870/api/v1/sync/hello -> HTTP 409: '
    '{"detail":{"code":"SYNC_WIRE_VERSION","message":"peer speaks sync wire v4, '
    'this machine speaks v3 (schema v14 vs v13). The synced row shapes differ, '
    'so no row may cross; upgrade whichever machine is on the lower wire version, '
    'then sync again."}}'
)


def test_parse_update_required_from_silver_http_409_message() -> None:
    parsed = wire_version.parse_update_required(_SILVER_MESSAGE)
    assert parsed is not None
    assert parsed.code == "SYNC_WIRE_VERSION"
    assert parsed.local_wire_version == 3
    assert parsed.peer_wire_version == 4
    assert parsed.action == "install the latest Open DJ"


def test_parse_update_required_from_sync_version_mismatch_exception() -> None:
    message = (
        "SyncVersionMismatch: peer speaks sync wire v4, this machine speaks v3 "
        "(schema v14 vs v13). The synced row shapes differ, so no row may cross; "
        "upgrade whichever machine is on the lower wire version, then sync again."
    )
    parsed = wire_version.parse_update_required(message)
    assert parsed is not None
    assert parsed.local_wire_version == 3
    assert parsed.peer_wire_version == 4


def test_parse_update_required_from_feedback_wrapper() -> None:
    message = (
        "[FEEDBACK_SYNC_FAILED] SyncVersionMismatch: peer speaks sync wire v4, "
        "this machine speaks v3 (schema v14 vs v13)."
    )
    parsed = wire_version.parse_update_required(message)
    assert parsed is not None
    assert parsed.local_wire_version == 3
    assert parsed.peer_wire_version == 4


@pytest.mark.parametrize(
    "message",
    [
        "",
        "Connection refused",
        'POST https://hub.example/api/v1/sync/hello -> HTTP 409: {"detail":{"code":"SYNC_APPLY"}}',
        '{"detail":{"code":"SYNC_WIRE_VERSION"}}',
    ],
)
def test_parse_update_required_returns_none_for_non_wire_mismatch(message: str) -> None:
    assert wire_version.parse_update_required(message) is None
