"""Conflict-trash serialization and R2 object-key contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

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
    assert conflict_trash.trash_object_key(body) == (
        "trash/"
        + hashlib.sha256(body).hexdigest()[:2]
        + "/"
        + hashlib.sha256(body).hexdigest()
        + ".json"
    )


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
    second = conflict_trash.store_record(_cfg(), s3, record)

    assert first.uploaded is True
    assert second.uploaded is False
    assert conflict_trash.restore_record(_cfg(), s3, first.object_key, now=now) == record
    with pytest.raises(conflict_trash.ExpiredTrashError, match="expired"):
        conflict_trash.restore_record(
            _cfg(), s3, first.object_key, now=now + timedelta(days=14, microseconds=1)
        )
