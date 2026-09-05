"""Track-order target selection shared by request validation and the worker."""
from __future__ import annotations

from fastapi import HTTPException


def select_track_target(stable_id: str, targets: list[tuple[str, str]]) -> tuple[str, str]:
    """Return exactly one on-disk target, or reject the invalid order."""
    selected = [(sid, path) for sid, path in targets if sid == stable_id]
    if len(selected) != 1:
        raise HTTPException(422, f"track {stable_id!r} is not an on-disk library track")
    return selected[0]
