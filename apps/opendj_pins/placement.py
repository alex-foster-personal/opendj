"""Pin placement relative to anchored UI elements."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_NEARBY_ANCHORS = 3
_SELECTOR_MAX_CHARS = 512
_PX_BOUND = 100_000.0


class PinElementOffset(BaseModel):
    """The click point inside the ``anchor`` element's box, 0..100 per axis."""

    model_config = ConfigDict(frozen=True)

    dx_pct: float = Field(ge=0, le=100)
    dy_pct: float = Field(ge=0, le=100)


class PinNearbyAnchor(BaseModel):
    """A stable neighbour of the pinned element and the pin's px offset from it."""

    model_config = ConfigDict(frozen=True)

    selector: str = Field(min_length=1, max_length=_SELECTOR_MAX_CHARS)
    dx_px: float = Field(ge=-_PX_BOUND, le=_PX_BOUND)
    dy_px: float = Field(ge=-_PX_BOUND, le=_PX_BOUND)


def require_anchor_for_offset(anchor: str | None, element_offset: PinElementOffset | None) -> None:
    if element_offset is not None and anchor is None:
        raise ValueError("element_offset needs an anchor selector to be relative to")


__all__ = [
    "MAX_NEARBY_ANCHORS",
    "PinElementOffset",
    "PinNearbyAnchor",
    "require_anchor_for_offset",
]
