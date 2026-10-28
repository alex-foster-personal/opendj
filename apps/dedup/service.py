"""``DedupService`` implemented by delegating to the existing dedup modules."""

from __future__ import annotations

from pathlib import Path

from . import library_scan
from .contract.models import ScanStatus


class LocalDedupService:
    """In-process dedup service; no logic of its own."""

    def scan_status(self) -> ScanStatus:
        # library_scan.JOB is read at call time so a swapped-in job is honored.
        return ScanStatus.model_validate(library_scan.JOB.status())

    def start_scan(self, *, state_db: Path | None, db_path: Path) -> bool:
        return library_scan.JOB.start(state_db=state_db, db_path=db_path)
