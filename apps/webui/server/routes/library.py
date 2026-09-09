"""Library-wide views that are not a single track/playlist -- LIBUX-06."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query

from apps.library_wheel.query import AXES, LibraryWheelError, query_library_wheel
from apps.shared import paths as shared_paths

router = APIRouter(prefix="/library", tags=["library"])

_AxisKey = Literal[
    "genre", "decade", "play_count", "popularity", "overplayed_ness", "playlist", "set_played_in"
]


@router.get("/wheel")
def get_library_wheel(
    axis: Annotated[_AxisKey, Query(
        description=(
            "Per-track overlay on the genre-grouped tree. A disabled axis "
            "(decade, overplayed_ness, set_played_in) still returns the "
            "real tree with every axis_value null and a stated reason, "
            "never a fabricated number."
        ),
    )] = "play_count",
) -> dict[str, object]:
    try:
        return query_library_wheel(
            shared_paths.STATE_DB, shared_paths.REKORDBOX_PLAIN_DB, axis=axis
        )
    except LibraryWheelError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "LIBRARY_WHEEL_UNAVAILABLE", "message": str(exc)},
        ) from exc


@router.get("/wheel/axes")
def get_library_wheel_axes() -> list[dict[str, object]]:
    """Axis catalog alone, so a picker can render enabled/disabled without
    paying for a full tree fetch."""
    return [
        {"key": a.key, "label": a.label, "enabled": a.enabled, "reason": a.reason}
        for a in AXES
    ]


__all__ = ["router"]
