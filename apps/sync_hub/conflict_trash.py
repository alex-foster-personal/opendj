"""Recoverable CloudSync conflict rows stored in the R2 trash namespace.

The apply engine can serialize a losing row before removing it, while the
transport layer decides when and where to upload it.  Records are immutable
and content addressed, so retrying the same loser is a no-op.  R2 lifecycle
configuration should expire the ``trash/`` prefix after :data:`RETENTION_DAYS`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from apps.cloud.asset_store import AssetS3Client, require_credentials
from apps.cloud.config import CloudConfig

TRASH_PREFIX: str = "trash"
RETENTION_DAYS: int = 14


class ConflictTrashError(RuntimeError):
    """The trash object cannot be safely stored or restored."""


class ExpiredTrashError(ConflictTrashError):
    """A trash object is outside the recovery window."""


@dataclass(frozen=True)
class TrashRecord:
    """The complete losing row and the stamps needed to explain its loss."""

    table: str
    primary_key: tuple[str, ...]
    values: dict[str, Any]
    origin_machine: str
    updated_at: str | None
    origin_device_id: str | None
    reason: str
    captured_at: datetime

    @property
    def expires_at(self) -> datetime:
        """The hard recovery deadline for this record."""
        return self.captured_at + timedelta(days=RETENTION_DAYS)


@dataclass(frozen=True)
class StoreResult:
    """The immutable R2 location and whether this call created it."""

    object_key: str
    bucket: str
    uploaded: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConflictTrashError("trash timestamps must include a timezone")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def serialize_record(record: TrashRecord) -> bytes:
    """Encode a record canonically, including its computed expiry."""
    if not record.table or not record.primary_key or not record.reason:
        raise ConflictTrashError("trash records need table, primary_key and reason")
    payload = {
        "captured_at": _timestamp(record.captured_at),
        "expires_at": _timestamp(record.expires_at),
        "origin_device_id": record.origin_device_id,
        "origin_machine": record.origin_machine,
        "primary_key": list(record.primary_key),
        "reason": record.reason,
        "table": record.table,
        "updated_at": record.updated_at,
        "values": record.values,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def trash_object_key(body: bytes) -> str:
    """Return a retry-stable key derived from the losing row's identity."""
    try:
        payload = json.loads(body)
        identity = {
            "origin_device_id": payload["origin_device_id"],
            "primary_key": payload["primary_key"],
            "reason": payload["reason"],
            "table": payload["table"],
            "updated_at": payload["updated_at"],
            "values": payload["values"],
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConflictTrashError("cannot derive a trash key from invalid JSON") from exc
    identity_body = json.dumps(
        identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(identity_body).hexdigest()
    return f"{TRASH_PREFIX}/{digest[:2]}/{digest}.json"


def store_record(cfg: CloudConfig, s3: AssetS3Client, record: TrashRecord) -> StoreResult:
    """Conditionally create one trash object in the state bucket."""
    require_credentials(cfg)
    body = serialize_record(record)
    key = trash_object_key(body)
    created, _etag = s3.put_object_if_none_match(cfg.state_bucket, key, body)
    if not created and s3.head_object(cfg.state_bucket, key) is None:
        raise ConflictTrashError(
            f"trash upload was rejected and {cfg.state_bucket}/{key} is absent"
        )
    return StoreResult(object_key=key, bucket=cfg.state_bucket, uploaded=created)


def _record_from_body(body: bytes, key: str) -> TrashRecord:
    if trash_object_key(body) != key:
        raise ConflictTrashError(f"trash body does not match object key {key!r}")
    try:
        payload = json.loads(body)
        captured_at = datetime.fromisoformat(payload["captured_at"])
        expires_at = datetime.fromisoformat(payload["expires_at"])
        record = TrashRecord(
            table=str(payload["table"]),
            primary_key=tuple(str(item) for item in payload["primary_key"]),
            values=dict(payload["values"]),
            origin_machine=str(payload["origin_machine"]),
            updated_at=payload["updated_at"],
            origin_device_id=payload["origin_device_id"],
            reason=str(payload["reason"]),
            captured_at=captured_at,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConflictTrashError(f"invalid trash record at {key!r}") from exc
    if _utc(record.expires_at) != _utc(expires_at):
        raise ConflictTrashError(f"trash expiry does not match capture at {key!r}")
    return record


def restore_record(
    cfg: CloudConfig,
    s3: AssetS3Client,
    object_key: str,
    *,
    now: datetime,
) -> TrashRecord:
    """Fetch and validate one record, refusing it after its 14-day deadline."""
    require_credentials(cfg)
    stored = s3.get_object(cfg.state_bucket, object_key)
    if stored is None:
        raise ConflictTrashError(f"trash object {cfg.state_bucket}/{object_key} does not exist")
    record = _record_from_body(stored[0], object_key)
    if _utc(now) >= record.expires_at:
        raise ExpiredTrashError(
            f"trash object {object_key} expired at {_timestamp(record.expires_at)}"
        )
    return record


__all__ = [
    "RETENTION_DAYS",
    "TRASH_PREFIX",
    "ConflictTrashError",
    "ExpiredTrashError",
    "StoreResult",
    "TrashRecord",
    "restore_record",
    "serialize_record",
    "store_record",
    "trash_object_key",
]
