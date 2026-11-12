"""Pydantic models and the service Protocol of the dedup contract."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class ScanStatus(BaseModel):
    """Progress of the on-device library fingerprint scan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["idle", "running", "done", "failed"]
    basis: Literal["fingerprint", "tags", "both"]
    total: int
    done: int
    computed: int
    cache_hits: int
    not_local: int
    errors: int
    clusters: int | None
    clusters_fingerprint: int | None
    clusters_tags: int | None
    tag_precision: float | None
    error: str | None
    started_at: str | None
    finished_at: str | None
    error_samples: tuple[str, ...]


@runtime_checkable
class DedupService(Protocol):
    """The dedup operations code outside ``apps.dedup`` uses today."""

    def scan_status(self) -> ScanStatus:
        """Where the background library scan is."""
        ...

    def start_scan(
        self,
        *,
        state_db: Path | None,
        db_path: Path,
        basis: Literal["fingerprint", "tags", "both"] = "both",
    ) -> bool:
        """Start the background library scan; False when one is already running."""
        ...


__all__ = ["DedupService", "ScanStatus"]
