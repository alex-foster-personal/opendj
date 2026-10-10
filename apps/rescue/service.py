"""``RescueService`` implementation delegating to ``apps.rescue.ring``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.rescue import ring
from apps.rescue.contract import RescueIndex


class RingRescueService:
    """Implements ``apps.rescue.contract.RescueService`` over the on-disk ring."""

    def append_snapshot(self, data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
        return ring.append_snapshot(data_dir, payload)

    def read_index(self, data_dir: Path) -> RescueIndex:
        return ring.read_index(data_dir)

    def read_latest(self, data_dir: Path) -> dict[str, Any] | None:
        return ring.read_latest(data_dir)
