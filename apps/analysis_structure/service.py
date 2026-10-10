"""``StructureService`` implementation: delegates to the existing functions, no logic of its own.

-Claude
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import serve


class LocalStructureService:
    """In-process ``StructureService`` over this package's own modules."""

    def merge_own_phrases(
        self, payload: dict[str, Any], stable_id: str, data_dir: Path, *, source: str
    ) -> dict[str, Any]:
        return serve.merge_own_phrases(payload, stable_id, data_dir, source=source)


__all__ = ["LocalStructureService"]
