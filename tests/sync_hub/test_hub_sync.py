"""SYNCEXPORT: sync_hub regression for hub storage-full push rollback.

[if] hub sqlite is full on push [then] 507, fence and changelog unchanged, [else stop].
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.sync_hub import client
from tests.cloudsync.enrollment_transport import TestClientTransport
from tests.cloudsync.test_hub_storage_full import (
    _cap_hub_pages,
    _first_successful_sync,
    _hub_changelog_count,
    _hub_changelog_has_track,
    _hub_changelog_max_seq,
    _lift_hub_pages,
    _spoke_push_seq,
    _track_title_on_hub,
)
from tests.cloudsync.test_hub_sync import _DEV_A, _T2, _insert_track, _open, _sync

_TRACK_TWO = "sync-hub-storage-track-two"
_SPOKE_NAME = "sync-hub-storage-spoke"

pytestmark = pytest.mark.requirement("SYNCEXPORT-13500")


def test_push_to_full_hub_returns_sync_hub_storage(
    monkeypatch: pytest.MonkeyPatch,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """[if] hub sqlite cannot grow [then] push is 507 SYNC_HUB_STORAGE, [else stop]."""
    _first_successful_sync(enroll_spoke_dir, enroll_hub)
    _cap_hub_pages(monkeypatch, enroll_hub_dir, enroll_hub)

    conn = _open(enroll_spoke_dir)
    try:
        _insert_track(
            conn,
            _TRACK_TWO,
            title="second track",
            updated_at=_T2,
            origin=_DEV_A,
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(client.SyncTransportError) as excinfo:
        _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)

    message = str(excinfo.value)
    assert "HTTP 507" in message
    assert "SYNC_HUB_STORAGE" in message
    assert "Internal Server Error" not in message


def test_storage_full_push_leaves_fence_and_changelog_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    enroll_hub: TestClientTransport,
    enroll_hub_dir: Path,
    enroll_spoke_dir: Path,
) -> None:
    """[if] that push fails [then] fence and changelog stay put and later sync converges, [else stop]."""
    first = _first_successful_sync(enroll_spoke_dir, enroll_hub)
    peer = first.hub_machine_id
    push_seq_before = _spoke_push_seq(enroll_spoke_dir, peer)
    max_seq_before = _hub_changelog_max_seq(enroll_hub_dir)
    count_before = _hub_changelog_count(enroll_hub_dir)

    _cap_hub_pages(monkeypatch, enroll_hub_dir, enroll_hub)

    conn = _open(enroll_spoke_dir)
    try:
        _insert_track(
            conn,
            _TRACK_TWO,
            title="held back track",
            updated_at=_T2,
            origin=_DEV_A,
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(client.SyncTransportError) as excinfo:
        _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)
    message = str(excinfo.value)
    assert "HTTP 507" in message
    assert "SYNC_HUB_STORAGE" in message

    assert _spoke_push_seq(enroll_spoke_dir, peer) == push_seq_before
    assert _hub_changelog_max_seq(enroll_hub_dir) == max_seq_before
    assert _hub_changelog_count(enroll_hub_dir) == count_before
    assert not _hub_changelog_has_track(enroll_hub_dir, _TRACK_TWO)

    _lift_hub_pages(monkeypatch, enroll_hub_dir)

    result = _sync(enroll_spoke_dir, enroll_hub, _SPOKE_NAME)
    assert re.fullmatch(r"[0-9a-f]{64}", result.digest)
    assert _track_title_on_hub(enroll_hub_dir, _TRACK_TWO) == "held back track"
