"""Public contract for the rescue snapshot ring (RESCUE-01).

Code outside ``apps.rescue`` imports only this module (delivery layers may also
use ``apps.rescue.http.build_service``). It depends on stdlib and pydantic only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, JsonValue

CONTRACT_VERSION = "rescue/2"

RING_SIZE = 8
MAX_BYTES = 32 * 1024


class RescueSnapshotTooLargeError(ValueError):
    """Serialized snapshot exceeds the 32 KiB cap."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class RescueSnapshotInvalidError(ValueError):
    """Snapshot payload failed structural validation."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


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


class RescueAppendReceipt(BaseModel):
    """Shape of the mapping ``RescueService.append_snapshot`` returns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: int
    captured_at_ms: int
    bytes: int


@runtime_checkable
class RescueService(Protocol):
    """Operations code outside ``apps.rescue`` uses on the snapshot ring."""

    def append_snapshot(self, data_dir: Path, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Validate and write one snapshot; returns a ``RescueAppendReceipt``-shaped dict."""
        ...

    def read_index(self, data_dir: Path) -> RescueIndex:
        """Read the ring index (empty index when none exists)."""
        ...

    def read_latest(self, data_dir: Path) -> dict[str, JsonValue] | None:
        """Newest readable snapshot, or None."""
        ...


__all__ = [
    "CONTRACT_VERSION",
    "MAX_BYTES",
    "RING_SIZE",
    "RescueAppendReceipt",
    "RescueIndex",
    "RescueService",
    "RescueSlotMeta",
    "RescueSnapshotInvalidError",
    "RescueSnapshotTooLargeError",
]
