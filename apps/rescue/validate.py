"""Structural validation for rescue ring index and snapshot payloads."""

from __future__ import annotations

import json
from typing import Any

from apps.rescue.contract import (
    MAX_BYTES,
    RING_SIZE,
    RescueIndex,
    RescueSlotMeta,
    RescueSnapshotInvalidError,
    RescueSnapshotTooLargeError,
)

_SCHEMA = 1
_SLOT_RANGE = f"0..{RING_SIZE - 1}"
_VALID_REASONS = frozenset({"periodic", "transport", "load"})


def compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def empty_index() -> RescueIndex:
    return RescueIndex(
        schema=_SCHEMA,
        newest_slot=0,
        slots=tuple(RescueSlotMeta(slot=i, captured_at_ms=0, bytes=0) for i in range(RING_SIZE)),
    )


def index_to_dict(index: RescueIndex) -> dict[str, Any]:
    return {
        "schema": index.schema,
        "newest_slot": index.newest_slot,
        "slots": [
            {"slot": slot.slot, "captured_at_ms": slot.captured_at_ms, "bytes": slot.bytes} for slot in index.slots
        ],
    }


def _require_ring_slot(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or not 0 <= value < RING_SIZE:
        raise RescueSnapshotInvalidError(f"index {label} must be {_SLOT_RANGE}")
    return value


def _require_non_negative_int(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise RescueSnapshotInvalidError(f"index {label} must be a non-negative int")
    return value


def parse_slot_meta(entry: dict[str, Any]) -> RescueSlotMeta:
    slot = _require_ring_slot(entry.get("slot"), label="slot")
    captured_at_ms = _require_non_negative_int(entry.get("captured_at_ms"), label="captured_at_ms")
    bytes_ = _require_non_negative_int(entry.get("bytes"), label="bytes")
    return RescueSlotMeta(slot=slot, captured_at_ms=captured_at_ms, bytes=bytes_)


def parse_index(raw: dict[str, Any]) -> RescueIndex:
    if raw.get("schema") != _SCHEMA:
        raise RescueSnapshotInvalidError("index schema must be 1")
    newest_slot = _require_ring_slot(raw.get("newest_slot"), label="newest_slot")
    slots_raw = raw.get("slots")
    if not isinstance(slots_raw, list) or len(slots_raw) != RING_SIZE:
        raise RescueSnapshotInvalidError(f"index slots must list {RING_SIZE} entries")
    slots: list[RescueSlotMeta] = []
    for entry in slots_raw:
        if not isinstance(entry, dict):
            raise RescueSnapshotInvalidError("index slot entry must be an object")
        slots.append(parse_slot_meta(entry))
    return RescueIndex(schema=_SCHEMA, newest_slot=newest_slot, slots=tuple(slots))


def validate_snapshot(payload: dict[str, Any]) -> int:
    if payload.get("schema") != _SCHEMA:
        raise RescueSnapshotInvalidError("snapshot schema must be 1")
    captured_at_ms = payload.get("captured_at_ms")
    if not isinstance(captured_at_ms, int) or captured_at_ms < 0:
        raise RescueSnapshotInvalidError("snapshot captured_at_ms must be a non-negative int")
    reason = payload.get("reason")
    if reason not in _VALID_REASONS:
        raise RescueSnapshotInvalidError("snapshot reason must be periodic, transport, or load")
    if payload.get("app_posture") != "gig":
        raise RescueSnapshotInvalidError("snapshot app_posture must be gig")
    decks = payload.get("decks")
    if not isinstance(decks, dict):
        raise RescueSnapshotInvalidError("snapshot decks must be an object")
    size = len(compact_json(payload).encode("utf-8"))
    if size > MAX_BYTES:
        raise RescueSnapshotTooLargeError(f"snapshot is {size} bytes; max is {MAX_BYTES}")
    return size
