"""Delivery-layer entry point for the rescue snapshot ring."""

from __future__ import annotations

from apps.rescue.contract import (
    MAX_BYTES,
    RING_SIZE,
    RescueService,
    RescueSnapshotInvalidError,
    RescueSnapshotTooLargeError,
)
from apps.rescue.ring import write_atomic
from apps.rescue.service import RingRescueService


def build_service() -> RescueService:
    return RingRescueService()


__all__ = [
    "MAX_BYTES",
    "RING_SIZE",
    "RescueSnapshotInvalidError",
    "RescueSnapshotTooLargeError",
    "build_service",
    "write_atomic",
]
