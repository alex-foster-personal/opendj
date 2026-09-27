"""Conflict-trash serialization and R2 object-key contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig
from apps.sync_hub import conflict_trash

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


def test_trash_payload_is_canonical_and_retained_for_fourteen_days() -> None:
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


def test_store_is_idempotent_and_rejects_expired_restore() -> None:
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
    second = conflict_trash.store_record(
        _cfg(), s3, replace(record, captured_at=now + timedelta(minutes=1))
    )

    assert first.uploaded is True
    assert second.uploaded is False
    assert conflict_trash.restore_record(_cfg(), s3, first.object_key, now=now) == record
    with pytest.raises(conflict_trash.ExpiredTrashError, match="expired"):
        conflict_trash.restore_record(
            _cfg(), s3, first.object_key, now=now + timedelta(days=14, microseconds=1)
        )


@pytest.mark.live_r2
def test_live_r2_conflict_trash_round_trip() -> None:
    """The production boto3 adapter must store and restore a real object."""
    from tests.cloudsync.conftest import LIVE_R2_ENV_VARS, live_r2_config

    cfg = live_r2_config()
    if cfg is None:
        pytest.skip("live R2 creds absent; set " + ", ".join(LIVE_R2_ENV_VARS))
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
        assert (
            conflict_trash.restore_record(cfg, s3, result.object_key, now=datetime.now(UTC))
            == record
        )
    finally:
        assert s3.delete_object(cfg.state_bucket, result.object_key)
