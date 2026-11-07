"""Default path resolution for the dedup pipeline."""

from __future__ import annotations

from pathlib import Path

from apps.shared import paths


def resolve_dedup_db(db_path: Path | None) -> Path:
    """Return ``db_path`` when set, else the phase-7 fallback cache DB."""
    return db_path if db_path is not None else paths.DEDUP_FALLBACK_DB
