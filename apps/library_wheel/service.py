"""``LibraryWheelService`` implementation: delegates to the existing functions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .genre_families import simple_genre_family
from .query import genre_tags_by_stable_id, query_library_wheel


class LibraryWheel:
    """Implements ``apps.library_wheel.contract.LibraryWheelService``."""

    def query(self, state_db: Path, master_db: Path, *, axis: str = "play_count") -> dict[str, Any]:
        return query_library_wheel(state_db, master_db, axis=axis)

    def genre_tags_by_stable_id(self, state_db: Path, master_db: Path) -> dict[str, str | None]:
        return genre_tags_by_stable_id(state_db, master_db)

    def simple_genre_family(self, tag: str | None) -> tuple[str, str] | None:
        return simple_genre_family(tag)


__all__ = ["LibraryWheel"]
