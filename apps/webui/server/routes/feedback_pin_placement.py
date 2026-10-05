"""Where a comment pin sits relative to the UI it was dropped on (Thu 1 Oct 2026).

A pin has always stored ``anchor`` (one selector, diagnostic) plus viewport
``x_pct``/``y_pct`` (the durable reference). That keeps a pin on the canvas,
but once the element it was dropped on moves or disappears the pin floats at
an old screen position with nothing tying it to the UI. These two optional
records let the browser re-place it against the UI instead:

``element_offset``
    where inside the ``anchor`` element's own box the click landed, as 0..100
    of its width and height. Percent, because the point is INSIDE that box,
    so it survives the element being resized.

``nearby_anchors``
    up to three other stable elements near the click (an id, a data-testid,
    or a tag plus aria-label, each unique on the page when captured), each
    with the pin's offset from that element's top-left corner in CSS pixels.
    Pixels, because a neighbour is often a sibling the point lies OUTSIDE of,
    where a percent of its box is not a meaningful position.

The browser resolves in order: ``anchor`` + ``element_offset``, then the
first nearby anchor that still resolves, then ``x_pct``/``y_pct``. Both
records are optional: pins created before them, and agent pins created
through the API without them, render exactly as before. The server only
validates and stores them; it never resolves selectors itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_NEARBY_ANCHORS = 3

# Bounds that only exist to refuse garbage: a selector is a short generated
# string, and no viewport is 100k px across.
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
    """An offset inside an element no selector names cannot ever be resolved."""

    if element_offset is not None and anchor is None:
        raise ValueError("element_offset needs an anchor selector to be relative to")
