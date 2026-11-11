"""Duplicate rule for ``anlz.cache_rebuild`` enqueues."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.engine_core.jobs.store import ActiveJob


def library_identity(data_dir: object, default_data_dir: Path) -> Path:
    raw = default_data_dir if data_dir is None else Path(str(data_dir))
    return raw.expanduser().resolve(strict=False)


def covering_job(
    active: list[ActiveJob],
    requested: dict[str, Any],
    *,
    default_data_dir: Path,
) -> str | None:
    """Return the id of the active job that already rebuilds the same targets."""
    library = library_identity(requested.get("data_dir"), default_data_dir)
    points = requested.get("points")
    wanted = requested.get("stable_ids")
    if not isinstance(wanted, list):
        return None
    wanted_set = set(wanted)
    for row in active:
        if library_identity(row.payload.get("data_dir"), default_data_dir) != library:
            continue
        if row.payload.get("points") != points:
            continue
        ids = row.payload.get("stable_ids")
        if isinstance(ids, list) and set(ids) == wanted_set:
            return row.id
    return None


__all__ = ["covering_job", "library_identity"]
