"""Atomic rolling rescue snapshot ring under ``<data-dir>/state/rescue/``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.rescue.contract import (
    MAX_BYTES,
    RING_SIZE,
    RescueIndex,
    RescueSlotMeta,
    RescueSnapshotInvalidError,
    RescueSnapshotTooLargeError,
)
from apps.rescue.validate import (
    compact_json,
    empty_index,
    index_to_dict,
    parse_index,
    validate_snapshot,
)
from apps.shared.atomic_text import write_atomic

__all__ = [
    "MAX_BYTES",
    "RING_SIZE",
    "RescueIndex",
    "RescueSlotMeta",
    "RescueSnapshotInvalidError",
    "RescueSnapshotTooLargeError",
    "append_snapshot",
    "read_index",
    "read_latest",
    "write_atomic",
]


def _ring_has_data(index: RescueIndex) -> bool:
    return any(slot.bytes > 0 for slot in index.slots)


@dataclass(frozen=True)
class _RescueRingPaths:
    """On-disk locations for the rescue ring under ``<data-dir>/state/rescue/``."""

    rescue_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> _RescueRingPaths:
        return cls(data_dir / "state" / "rescue")

    @property
    def index_file(self) -> Path:
        return self.rescue_dir / "index.json"

    def ring_slot(self, slot: int) -> Path:
        return self.rescue_dir / f"ring-{slot}.json"


def _load_index(paths: _RescueRingPaths) -> RescueIndex:
    path = paths.index_file
    if not path.is_file():
        return empty_index()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RescueSnapshotInvalidError(f"index is not parseable JSON: {error}") from error
    if not isinstance(raw, dict):
        raise RescueSnapshotInvalidError("index root must be an object")
    return parse_index(raw)


def _read_slot_snapshot(paths: _RescueRingPaths, slot: int) -> dict[str, Any] | None:
    path = paths.ring_slot(slot)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def read_index(data_dir: Path) -> RescueIndex:
    return _load_index(_RescueRingPaths.from_data_dir(data_dir))


def read_latest(data_dir: Path) -> dict[str, Any] | None:
    paths = _RescueRingPaths.from_data_dir(data_dir)
    try:
        index = _load_index(paths)
    except RescueSnapshotInvalidError:
        return None
    if not _ring_has_data(index):
        return None
    for offset in range(RING_SIZE):
        slot = (index.newest_slot - offset) % RING_SIZE
        meta = index.slots[slot]
        if meta.bytes == 0:
            continue
        snapshot = _read_slot_snapshot(paths, slot)
        if snapshot is not None:
            return snapshot
    return None


def append_snapshot(data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    size = validate_snapshot(payload)
    paths = _RescueRingPaths.from_data_dir(data_dir)
    index = _load_index(paths)
    next_slot = (index.newest_slot + 1) % RING_SIZE if _ring_has_data(index) else 0
    captured_at_ms = int(payload["captured_at_ms"])

    write_atomic(paths.ring_slot(next_slot), compact_json(payload) + "\n")

    slots = list(index.slots)
    slots[next_slot] = RescueSlotMeta(slot=next_slot, captured_at_ms=captured_at_ms, bytes=size)
    updated = RescueIndex(schema=1, newest_slot=next_slot, slots=tuple(slots))
    write_atomic(paths.index_file, json.dumps(index_to_dict(updated), indent=2) + "\n")
    return {"slot": next_slot, "captured_at_ms": captured_at_ms, "bytes": size}
