"""Structural jobs-store seam for adapters, cloud, and webui listing.

The concrete store is ``apps.engine_core.jobs.store.JobStore``. This package
cannot import that class (layer contract and ``python.package_cycles``), so
callers annotate with these Protocols instead of ``Any`` or a TYPE_CHECKING
import of ``JobStore``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class JobStoreLike(Protocol):
    """``db_path`` plus ``enqueue``: the surface ``enqueue_hydrate_asset`` uses."""

    db_path: Path

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        external_ref: str | None = None,
    ) -> dict[str, Any]: ...


class JobStoreListLike(Protocol):
    """``list``: the surface ``build_track_rows`` uses."""

    def list(self, *, limit: int = 200) -> list[dict[str, Any]]: ...
