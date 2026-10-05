"""Tempo preference wire schemas.

Supersedes: definitions in models.py; that module re-exports these classes.
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator


class TempoPrefOut(BaseModel):
    """PREF-01: a track's user-set preferred tempo plus its playable range.

    Any of the three may be null (unset). Never fabricated on read - a track
    with no tempo_pref field row at all projects as a null ``TrackOut.tempo_pref``,
    not this shape with all-null members (see sqlite_backend._row_to_track).
    """

    regular: float | None = None
    min: float | None = None
    max: float | None = None


class TempoPrefPatch(BaseModel):
    regular: float | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _min_less_than_max(self) -> TempoPrefPatch:
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError("tempo_pref.min must be less than tempo_pref.max")
        return self


