"""The ``library_related_criteria`` UI pref (LIBUX-10046): Related Tracks rules.

Stored in ``ui-prefs.json`` beside the other prefs and served by
``GET/PUT /api/v1/ui-prefs``; the related route reads it for any criterion
its query omits. Kept here so the ui_prefs route only wires it.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from .related_tracks import BPM_PERCENT_MAX, BPM_PERCENT_MIN, DEFAULT_RELATED_CRITERIA

DEFAULT_LIBRARY_RELATED_CRITERIA: dict[str, Any] = DEFAULT_RELATED_CRITERIA.to_dict()
_BOOL_KEYS = ("key", "genre", "color", "rating", "mytag", "artist")


class LibraryRelatedCriteriaOut(BaseModel):
    """Related Tracks criteria: BPM range percent plus six on/off rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bpm_percent: StrictInt = DEFAULT_RELATED_CRITERIA.bpm_percent
    key: StrictBool = DEFAULT_RELATED_CRITERIA.key
    genre: StrictBool = DEFAULT_RELATED_CRITERIA.genre
    color: StrictBool = DEFAULT_RELATED_CRITERIA.color
    rating: StrictBool = DEFAULT_RELATED_CRITERIA.rating
    mytag: StrictBool = DEFAULT_RELATED_CRITERIA.mytag
    artist: StrictBool = DEFAULT_RELATED_CRITERIA.artist


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "UI_PREFS_INVALID", "message": message})


def parse_library_related_criteria(raw: Any) -> dict[str, Any]:
    """Validate a stored or merged criteria object. Missing keys take defaults."""
    if raw is None:
        return dict(DEFAULT_LIBRARY_RELATED_CRITERIA)
    if not isinstance(raw, dict):
        raise _invalid("library_related_criteria must be an object")
    unknown = sorted(set(raw) - set(DEFAULT_LIBRARY_RELATED_CRITERIA))
    if unknown:
        raise _invalid(f"library_related_criteria has unknown key {unknown[0]!r}")
    out = dict(DEFAULT_LIBRARY_RELATED_CRITERIA)
    if "bpm_percent" in raw:
        pct = raw["bpm_percent"]
        if type(pct) is not int or not BPM_PERCENT_MIN <= pct <= BPM_PERCENT_MAX:
            raise _invalid(
                f"library_related_criteria.bpm_percent must be an integer from {BPM_PERCENT_MIN} to {BPM_PERCENT_MAX}"
            )
        out["bpm_percent"] = pct
    for key in _BOOL_KEYS:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise _invalid(f"library_related_criteria.{key} must be a boolean")
            out[key] = raw[key]
    return out


def merge_library_related_criteria(current: dict[str, Any], patch: LibraryRelatedCriteriaOut | None) -> None:
    """Merge the named fields of a PUT onto what is stored."""
    if patch is None:
        return
    current["library_related_criteria"] = parse_library_related_criteria(
        {
            **current.get("library_related_criteria", DEFAULT_LIBRARY_RELATED_CRITERIA),
            **patch.model_dump(exclude_unset=True),
        }
    )
