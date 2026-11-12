"""Recoverable CloudSync conflict rows stored in the R2 trash namespace.

The apply engine can serialize a losing row before removing it, while the
transport layer decides when and where to upload it.  Records are immutable
and content addressed, so retrying the same loser is a no-op.  R2 lifecycle
configuration should expire the ``trash/`` prefix after :data:`RETENTION_DAYS`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from apps.cloud.asset_store import AssetS3Client, require_credentials
from apps.cloud.config import CloudConfig, MissingEnvError
from apps.shared.state.sync_tables import pk_predicate as _pk_predicate
from apps.sync_hub.protocol import SPEC_BY_TABLE, UPDATED_AT, ORIGIN_DEVICE_ID, table_columns

TRASH_PREFIX: str = "trash"
RETENTION_DAYS: int = 14

REASON_LWW_OVERWRITE: str = "lww-overwrite"
REASON_TOMBSTONE: str = "tombstone"
REASON_IDENTITY_COLLAPSE: str = "identity-collapse"
REASON_QUARANTINE_DROP: str = "quarantine-drop"
REASON_NATURAL_KEY_LOSER: str = "natural-key-loser"


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
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    identity_body = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(identity_body).hexdigest()
    return f"{TRASH_PREFIX}/{digest[:2]}/{digest}.json"


def store_record(cfg: CloudConfig, s3: AssetS3Client, record: TrashRecord) -> StoreResult:
    """Conditionally create one trash object in the state bucket."""
    require_credentials(cfg)
    body = serialize_record(record)
    key = trash_object_key(body)
    created, _etag = s3.put_object_if_none_match(cfg.state_bucket, key, body)
    if not created and s3.head_object(cfg.state_bucket, key) is None:
        raise ConflictTrashError(f"trash upload was rejected and {cfg.state_bucket}/{key} is absent")
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
        raise ExpiredTrashError(f"trash object {object_key} expired at {_timestamp(record.expires_at)}")
    return record


# ----- apply capture ---------------------------------------------------------


@dataclass
class ConflictTrashCapture:
    """Upload losing rows to R2 for the duration of one apply transaction."""

    conn: sqlite3.Connection
    cfg: CloudConfig
    s3: AssetS3Client
    clock: Callable[[], datetime]

    def store(
        self,
        *,
        table: str,
        primary_key: Sequence[str],
        values: Mapping[str, Any],
        reason: str,
    ) -> StoreResult | None:
        """Write one loser if capture is active."""
        updated_at = values.get(UPDATED_AT)
        origin_device_id = values.get(ORIGIN_DEVICE_ID)
        if updated_at is not None and not isinstance(updated_at, str):
            updated_at = str(updated_at)
        if origin_device_id is not None and not isinstance(origin_device_id, str):
            origin_device_id = str(origin_device_id)
        record = TrashRecord(
            table=table,
            primary_key=tuple(str(item) for item in primary_key),
            values=dict(values),
            origin_machine=_origin_machine_name(self.conn, origin_device_id),
            updated_at=updated_at,
            origin_device_id=origin_device_id,
            reason=reason,
            captured_at=self.clock(),
        )
        return store_record(self.cfg, self.s3, record)

    def capture_stored_row(self, table: str, pk: Sequence[str], reason: str) -> StoreResult | None:
        """Serialize the stored row at ``pk`` before it is removed or overwritten."""
        values = _stored_row_values(self.conn, table, pk)
        if values is None:
            return None
        return self.store(table=table, primary_key=pk, values=values, reason=reason)


def _origin_machine_name(conn: sqlite3.Connection, origin_device_id: str | None) -> str:
    if not origin_device_id:
        return "unknown"
    row = conn.execute(
        "SELECT name FROM machines WHERE machine_id = ?",
        (origin_device_id,),
    ).fetchone()
    return str(row[0]) if row is not None else origin_device_id


def _stored_row_values(conn: sqlite3.Connection, table: str, pk: Sequence[str]) -> dict[str, Any] | None:
    spec = SPEC_BY_TABLE.get(table)
    if spec is None:
        return None
    columns = table_columns(conn, table)
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {_pk_predicate(spec)}",
        tuple(pk),
    ).fetchone()
    if row is None:
        return None
    return {column: row[index] for index, column in enumerate(columns)}


_active_capture: ContextVar[ConflictTrashCapture | None] = ContextVar("conflict_trash_capture", default=None)


def active_capture() -> ConflictTrashCapture | None:
    """The capture installed for the current apply, if any."""
    return _active_capture.get()


@contextmanager
def capture_pass(
    conn: sqlite3.Connection,
    cfg: CloudConfig,
    s3: AssetS3Client,
    *,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[ConflictTrashCapture]:
    """Bind one capture for nested apply calls in the same transaction."""
    capture = ConflictTrashCapture(
        conn=conn,
        cfg=cfg,
        s3=s3,
        clock=clock or (lambda: datetime.now(UTC)),
    )
    token = _active_capture.set(capture)
    try:
        yield capture
    finally:
        _active_capture.reset(token)


@contextmanager
def maybe_capture_pass(conn: sqlite3.Connection) -> Iterator[ConflictTrashCapture | None]:
    """Use live R2 trash capture when credentials are configured."""
    try:
        cfg = CloudConfig.from_env()
        require_credentials(cfg)
    except (MissingEnvError, ConflictTrashError):
        yield None
        return
    from apps.cloud.asset_store import boto3_asset_client

    with capture_pass(conn, cfg, boto3_asset_client(cfg)) as capture:
        yield capture


def capture_stored_loser(conn: sqlite3.Connection, table: str, pk: Sequence[str], reason: str) -> None:
    """No-op without an active capture."""
    capture = active_capture()
    if capture is None:
        return
    capture.capture_stored_row(table, pk, reason)


def capture_values_loser(
    table: str,
    pk: Sequence[str],
    values: Mapping[str, Any],
    reason: str,
) -> None:
    """Capture an offered row that never reached storage."""
    capture = active_capture()
    if capture is None:
        return
    capture.store(table=table, primary_key=pk, values=values, reason=reason)


def reset_capture_for_tests() -> None:
    """Clear a leaked capture context."""
    _active_capture.set(None)


__all__ = [
    "RETENTION_DAYS",
    "TRASH_PREFIX",
    "REASON_IDENTITY_COLLAPSE",
    "REASON_LWW_OVERWRITE",
    "REASON_NATURAL_KEY_LOSER",
    "REASON_QUARANTINE_DROP",
    "REASON_TOMBSTONE",
    "ConflictTrashCapture",
    "ConflictTrashError",
    "ExpiredTrashError",
    "StoreResult",
    "TrashRecord",
    "active_capture",
    "capture_pass",
    "capture_stored_loser",
    "capture_values_loser",
    "maybe_capture_pass",
    "reset_capture_for_tests",
    "restore_record",
    "serialize_record",
    "store_record",
    "trash_object_key",
]
