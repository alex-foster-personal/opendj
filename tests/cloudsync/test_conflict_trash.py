"""Conflict-trash serialization and R2 object-key contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig
from apps.sync_hub import conflict_trash, engine
from tests.cloudsync.test_hub_sync import _DEV_A, _DEV_B, _T0, _T1, _T2, _insert_track, _open
from tests.cloudsync.test_track_identity_collapse import _incoming_track

from .conftest import InMemoryAssetS3


def _cfg() -> CloudConfig:
    return CloudConfig(
        r2_account_id="account",
        r2_access_key_id="access",
        r2_secret_access_key="secret",
        state_bucket="state",
        audio_bucket="audio",
        hostname="test-host",
        bind_host="127.0.0.1",
    )


@pytest.mark.requirement("CLOUDSYNC-12622")
def test_trash_payload_is_canonical_and_retained_for_fourteen_days() -> None:
    """[if] a loser record is captured [then] payload fields include expiry, [else stop]."""
    captured = datetime(2026, 9, 25, 21, 30, tzinfo=UTC)
    record = conflict_trash.TrashRecord(
        table="tracks",
        primary_key=("track-1",),
        values={"stable_id": "track-1", "title": "Old title"},
        origin_machine="air",
        updated_at="2026-09-25T20:00:00.000000+00:00",
        origin_device_id="air-device",
        reason="lww-overwrite",
        captured_at=captured,
    )

    body = conflict_trash.serialize_record(record)
    decoded = json.loads(body)

    assert decoded["values"]["title"] == "Old title"
    assert decoded["expires_at"] == "2026-10-09T21:30:00.000000+00:00"
    assert conflict_trash.trash_object_key(body).startswith("trash/")


@pytest.mark.requirement("CLOUDSYNC-12622")
def test_store_is_idempotent_and_rejects_expired_restore() -> None:
    """[if] restore is requested at or after expires_at [then] restore fails, [else stop]."""
    now = datetime(2026, 9, 25, 21, 30, tzinfo=UTC)
    record = conflict_trash.TrashRecord(
        table="tracks",
        primary_key=("track-1",),
        values={"stable_id": "track-1"},
        origin_machine="air",
        updated_at="2026-09-25T20:00:00.000000+00:00",
        origin_device_id="air-device",
        reason="tombstone",
        captured_at=now,
    )
    s3 = InMemoryAssetS3()

    first = conflict_trash.store_record(_cfg(), s3, record)
    second = conflict_trash.store_record(_cfg(), s3, replace(record, captured_at=now + timedelta(minutes=1)))

    assert first.uploaded is True
    assert second.uploaded is False
    assert conflict_trash.restore_record(_cfg(), s3, first.object_key, now=now) == record
    with pytest.raises(conflict_trash.ExpiredTrashError, match="expired"):
        conflict_trash.restore_record(_cfg(), s3, first.object_key, now=now + timedelta(days=14, microseconds=1))


def _register_machine(conn, machine_id: str, name: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO machines(
            machine_id, name, platform, first_seen, last_seen, is_hub, data_root
        ) VALUES (?, ?, 'linux', ?, ?, 0, '')
        """,
        (machine_id, name, _T0, _T0),
    )


def _trash_keys(s3: InMemoryAssetS3, bucket: str) -> list[str]:
    return [key for (bkt, key) in s3.store if bkt == bucket and key.startswith("trash/")]


def _capture_fixture(tmp_path: Path) -> tuple:
    conn = _open(tmp_path / "hub")
    _register_machine(conn, _DEV_A, "device-a")
    _register_machine(conn, _DEV_B, "device-b")
    cfg = _cfg()
    s3 = InMemoryAssetS3()
    now = datetime(2026, 9, 25, 21, 30, tzinfo=UTC)
    return conn, cfg, s3, now


@pytest.mark.requirement("CLOUDSYNC-12622b")
def test_apply_lww_overwrite_captures_the_stored_loser(tmp_path: Path) -> None:
    """[if] LWW overwrite happens [then] trash holds the stored row stamps and values, [else stop]."""
    conn, cfg, s3, now = _capture_fixture(tmp_path)
    try:
        _insert_track(conn, "trk-1", title="old", updated_at=_T0, origin=_DEV_A)
        incoming = _incoming_track(conn, "trk-1", title="new", updated_at=_T2)
        with conflict_trash.capture_pass(conn, cfg, s3, clock=lambda: now):
            engine.hub_apply(conn, [incoming])
        keys = _trash_keys(s3, cfg.state_bucket)
        assert len(keys) == 1
        body = json.loads(s3.store[(cfg.state_bucket, keys[0])][0])
        assert body["table"] == "tracks"
        assert body["primary_key"] == ["trk-1"]
        assert body["values"]["title"] == "old"
        assert body["origin_machine"] == "device-a"
        assert body["updated_at"] == _T0
        assert body["origin_device_id"] == _DEV_A
        assert body["reason"] == conflict_trash.REASON_LWW_OVERWRITE
    finally:
        conn.close()


@pytest.mark.requirement("CLOUDSYNC-12622b")
def test_apply_replay_does_not_create_a_second_trash_object(tmp_path: Path) -> None:
    """[if] the same loser is applied again [then] no second trash object is created, [else stop]."""
    conn, cfg, s3, now = _capture_fixture(tmp_path)
    try:
        _insert_track(conn, "trk-1", title="kept", updated_at=_T2, origin=_DEV_A)
        incoming = _incoming_track(conn, "trk-1", title="stale", updated_at=_T0)
        with conflict_trash.capture_pass(conn, cfg, s3, clock=lambda: now):
            engine.hub_apply(conn, [incoming])
            engine.hub_apply(conn, [incoming])
        assert len(_trash_keys(s3, cfg.state_bucket)) == 1
    finally:
        conn.close()


@pytest.mark.requirement("CLOUDSYNC-12622b")
def test_restore_before_deadline_returns_stored_values(tmp_path: Path) -> None:
    """[if] restore is requested before 14 days [then] stored values are returned, [else stop]."""
    conn, cfg, s3, now = _capture_fixture(tmp_path)
    try:
        _insert_track(conn, "trk-1", title="old", updated_at=_T0, origin=_DEV_A)
        with conflict_trash.capture_pass(conn, cfg, s3, clock=lambda: now):
            engine.hub_apply(
                conn,
                [_incoming_track(conn, "trk-1", title="new", updated_at=_T2)],
            )
        key = _trash_keys(s3, cfg.state_bucket)[0]
        restored = conflict_trash.restore_record(cfg, s3, key, now=now)
        assert restored.values["title"] == "old"
    finally:
        conn.close()


@pytest.mark.requirement("CLOUDSYNC-12622b")
def test_restore_after_expiry_is_not_recoverable(tmp_path: Path) -> None:
    """[if] restore is requested after expiry [then] the item is not recoverable, [else stop]."""
    conn, cfg, s3, now = _capture_fixture(tmp_path)
    try:
        _insert_track(conn, "trk-1", title="old", updated_at=_T0, origin=_DEV_A)
        with conflict_trash.capture_pass(conn, cfg, s3, clock=lambda: now):
            engine.hub_apply(
                conn,
                [_incoming_track(conn, "trk-1", title="new", updated_at=_T2)],
            )
        key = _trash_keys(s3, cfg.state_bucket)[0]
        with pytest.raises(conflict_trash.ExpiredTrashError):
            conflict_trash.restore_record(
                cfg,
                s3,
                key,
                now=now + timedelta(days=14, microseconds=1),
            )
    finally:
        conn.close()


def test_live_r2_conflict_trash_round_trip() -> None:
    """The production boto3 adapter must store and restore a real object."""
    from tests.cloudsync.conftest import LIVE_R2_ENV_VARS, live_r2_config

    cfg = live_r2_config()
    if cfg is None:
        pytest.skip("live R2 creds absent; set " + ", ".join(LIVE_R2_ENV_VARS))
    assert cfg is not None
    pytest.importorskip("boto3")
    s3 = asset_store.boto3_asset_client(cfg)
    record = conflict_trash.TrashRecord(
        table="tracks",
        primary_key=(f"live-{uuid4().hex}",),
        values={"stable_id": "live-probe", "title": "trash probe"},
        origin_machine="live-probe",
        updated_at="2026-09-25T20:00:00.000000+00:00",
        origin_device_id="live-probe",
        reason="lww-overwrite",
        captured_at=datetime.now(UTC),
    )
    result = conflict_trash.store_record(cfg, s3, record)
    try:
        assert conflict_trash.restore_record(cfg, s3, result.object_key, now=datetime.now(UTC)) == record
    finally:
        assert s3.delete_object(cfg.state_bucket, result.object_key)
