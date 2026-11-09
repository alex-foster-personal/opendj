"""Default path resolution for the dedup pipeline."""

from __future__ import annotations

from pathlib import Path

from apps.shared import paths


def resolve_dedup_db(db_path: Path | None) -> Path:
    """Return ``db_path`` when set, else the phase-7 fallback cache DB."""
    return db_path if db_path is not None else paths.DEDUP_FALLBACK_DB


def resolve_music_roots(roots: list[Path] | None) -> list[Path]:
    """Return ``roots`` when set, else the configured ``paths.MUSIC_ROOTS``."""
    return roots if roots is not None else paths.MUSIC_ROOTS
