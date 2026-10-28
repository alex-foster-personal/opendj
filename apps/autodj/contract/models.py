"""Wire models for ``apps.autodj``: closed, frozen, and extra-forbidding.

No planner state crosses this module. Each model is the JSON shape a caller
receives, so a module can move out of process without its callers changing.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StyleBars(_Wire):
    """The bar range a transition style accepts; ``default`` is used when none is asked."""

    min: int
    max: int
    default: int


class StyleParamsWire(_Wire):
    """AGENT-34: a parametric style's settings, as ``list_styles`` reports them."""

    volume: str
    fade_shape: str
    swap_position: str
    filter: str | None
    stems: str | None
    overlap_comp_db: float
    comp_with_low_kill: bool


class StyleSummary(_Wire):
    """One named transition style as an agent reads it (``list_styles``)."""

    name: str
    summary: str
    bars: StyleBars
    needs_beatmatch: bool
    controls: tuple[str, ...]
    when: str
    available: bool
    unavailable_reason: str | None
    # AGENT-34: a parametric style's settings (volume lane, fade_shape, swap_position, filter, stems,
    # overlap_comp_db, comp_with_low_kill), null for a fixed style; the Spotify transition it mirrors, or null.
    params: StyleParamsWire | None
    spotify: str | None

