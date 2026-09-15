"""Atomic rolling rescue snapshot ring under ``<data-dir>/state/rescue/``."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RING_SIZE = 8
MAX_BYTES = 32 * 1024
_SCHEMA = 1


class RescueSnapshotTooLargeError(ValueError):
    """Serialized snapshot exceeds the 32 KiB cap."""


class RescueSnapshotInvalidError(ValueError):
    """Snapshot payload failed structural validation."""


@dataclass(frozen=True)
class RescueSlotMeta:
    slot: int
    captured_at_ms: int
    bytes: int


@dataclass(frozen=True)
class RescueIndex:
    schema: int
    newest_slot: int
    slots: tuple[RescueSlotMeta, ...]


def _rescue_dir(data_dir: Path) -> Path:
    return data_dir / "state" / "rescue"


def _ring_path(rescue_dir: Path, slot: int) -> Path:
    return rescue_dir / f"ring-{slot}.json"


def _index_path(rescue_dir: Path) -> Path:
    return rescue_dir / "index.json"


def write_atomic(path: Path, text: str) -> None:
    """Same contract as ``apps.webui.server.routes.feedback.write_atomic``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _empty_index() -> RescueIndex:
    return RescueIndex(
        schema=_SCHEMA,
        newest_slot=0,
        slots=tuple(RescueSlotMeta(slot=i, captured_at_ms=0, bytes=0) for i in range(RING_SIZE)),
    )


def _parse_index(raw: dict[str, Any]) -> RescueIndex:
    if raw.get("schema") != _SCHEMA:
        raise RescueSnapshotInvalidError("index schema must be 1")
    newest_slot = raw.get("newest_slot")
    if not isinstance(newest_slot, int) or not 0 <= newest_slot < RING_SIZE:
        raise RescueSnapshotInvalidError("index newest_slot must be 0..7")
    slots_raw = raw.get("slots")
    if not isinstance(slots_raw, list) or len(slots_raw) != RING_SIZE:
        raise RescueSnapshotInvalidError("index slots must list 8 entries")
    slots: list[RescueSlotMeta] = []
    for entry in slots_raw:
        if not isinstance(entry, dict):
            raise RescueSnapshotInvalidError("index slot entry must be an object")
        slot = entry.get("slot")
        captured_at_ms = entry.get("captured_at_ms")
        bytes_ = entry.get("bytes")
        if not isinstance(slot, int) or not 0 <= slot < RING_SIZE:
            raise RescueSnapshotInvalidError("index slot must be 0..7")
        if not isinstance(captured_at_ms, int) or captured_at_ms < 0:
            raise RescueSnapshotInvalidError("index captured_at_ms must be a non-negative int")
        if not isinstance(bytes_, int) or bytes_ < 0:
            raise RescueSnapshotInvalidError("index bytes must be a non-negative int")
        slots.append(RescueSlotMeta(slot=slot, captured_at_ms=captured_at_ms, bytes=bytes_))
    return RescueIndex(schema=_SCHEMA, newest_slot=newest_slot, slots=tuple(slots))


def _index_to_dict(index: RescueIndex) -> dict[str, Any]:
    return {
        "schema": index.schema,
        "newest_slot": index.newest_slot,
        "slots": [
            {"slot": slot.slot, "captured_at_ms": slot.captured_at_ms, "bytes": slot.bytes}
            for slot in index.slots
        ],
    }


def _load_index(rescue_dir: Path) -> RescueIndex:
    path = _index_path(rescue_dir)
    if not path.is_file():
        return _empty_index()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RescueSnapshotInvalidError(f"index is not parseable JSON: {error}") from error
    if not isinstance(raw, dict):
        raise RescueSnapshotInvalidError("index root must be an object")
    return _parse_index(raw)


def _validate_snapshot(payload: dict[str, Any]) -> int:
    if payload.get("schema") != 1:
        raise RescueSnapshotInvalidError("snapshot schema must be 1")
    captured_at_ms = payload.get("captured_at_ms")
    if not isinstance(captured_at_ms, int) or captured_at_ms < 0:
        raise RescueSnapshotInvalidError("snapshot captured_at_ms must be a non-negative int")
    reason = payload.get("reason")
    if reason not in {"periodic", "transport", "load"}:
        raise RescueSnapshotInvalidError("snapshot reason must be periodic, transport, or load")
    if payload.get("app_posture") != "gig":
        raise RescueSnapshotInvalidError("snapshot app_posture must be gig")
    decks = payload.get("decks")
    if not isinstance(decks, dict):
        raise RescueSnapshotInvalidError("snapshot decks must be an object")
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    size = len(serialized.encode("utf-8"))
    if size > MAX_BYTES:
        raise RescueSnapshotTooLargeError(f"snapshot is {size} bytes; max is {MAX_BYTES}")
    return size


def _read_slot_snapshot(rescue_dir: Path, slot: int) -> dict[str, Any] | None:
    path = _ring_path(rescue_dir, slot)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def read_index(data_dir: Path) -> RescueIndex:
    return _load_index(_rescue_dir(data_dir))


def read_latest(data_dir: Path) -> dict[str, Any] | None:
    rescue_dir = _rescue_dir(data_dir)
    try:
        index = _load_index(rescue_dir)
    except RescueSnapshotInvalidError:
        return None
    if all(slot.bytes == 0 for slot in index.slots):
        return None
    for offset in range(RING_SIZE):
        slot = (index.newest_slot - offset) % RING_SIZE
        meta = index.slots[slot]
        if meta.bytes == 0:
            continue
        snapshot = _read_slot_snapshot(rescue_dir, slot)
        if snapshot is not None:
            return snapshot
    return None


def append_snapshot(data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    size = _validate_snapshot(payload)
    rescue_dir = _rescue_dir(data_dir)
    index = _load_index(rescue_dir)
    has_any = any(slot.bytes > 0 for slot in index.slots)
    next_slot = (index.newest_slot + 1) % RING_SIZE if has_any else 0
    captured_at_ms = int(payload["captured_at_ms"])

    ring_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    write_atomic(_ring_path(rescue_dir, next_slot), ring_text + "\n")

    slots = list(index.slots)
    slots[next_slot] = RescueSlotMeta(slot=next_slot, captured_at_ms=captured_at_ms, bytes=size)
    updated = RescueIndex(schema=_SCHEMA, newest_slot=next_slot, slots=tuple(slots))
    write_atomic(_index_path(rescue_dir), json.dumps(_index_to_dict(updated), indent=2) + "\n")
    return {"slot": next_slot, "captured_at_ms": captured_at_ms, "bytes": size}
