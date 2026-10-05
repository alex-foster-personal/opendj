"""Beatgrid verdict row model, split out of models.py to keep that module small."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class GridQualityRowOut(BaseModel):
    """A track row's stored beatgrid verdict (GRIDFLAG-02).

    Read back from the grid-quality store; a listing never parses a grid.
    ``unknown`` means nobody could judge the grid (``reason`` says why) and
    must never be drawn as ``ok`` or as a flag. ``message`` is the sentence
    the deck's Beat Sync badge shows for the same grid; null for ``ok``.
    """

    grid_class: Literal["ok", "suspect", "variable_tempo", "unknown"]
    reason: str | None = None
    # The user hid this track's flag (track field ``grid_flag_dismissed``).
    dismissed: bool = False
    message: str | None = None
