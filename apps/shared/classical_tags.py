"""Classical and work tags read from an audio file, as a proposal.

Library Organization PRD s6.10, R-CLS-4. Reading a tag never creates a work or
stores a value: the caller shows what the file says and the user confirms.
Every read goes through :mod:`apps.shared._tagreader` (tinytag).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from apps.shared import _tagreader

_YEAR = re.compile(r"\b(\d{4})\b")
_LEADING_NUMBER = re.compile(r"\s*(\d+)")


def _first(tag: Any, *keys: str) -> str | None:
    for key in keys:
        value = _tagreader.first_other(tag, key)
        if value:
            return value
    return None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _movement_number(raw: str | None) -> int | None:
    """MVIN is "n" or "n/total". A malformed value ("/", "x/") is simply no number."""
    match = _LEADING_NUMBER.match(raw or "")
    return int(match.group(1)) if match else None


def _year(raw: Any) -> int | None:
    match = _YEAR.search(str(raw or ""))
    return int(match.group(1)) if match else None


def tag_year(tag: Any) -> int | None:
    """The 4-digit year a parsed tag carries (``2019`` from ``2019-05-03``), or None (issue #10029)."""
    return _year(tag.year)


def tag_label(tag: Any) -> str | None:
    """The record label a parsed tag carries (publisher, label or organization frame), or None."""
    return _first(tag, "publisher", "label", "organization")


def proposal_from_tag(tag: Any) -> dict[str, Any]:
    """The classical fields a parsed tag carries. Absent fields are left out."""
    proposal: dict[str, Any] = {
        "work": _first(tag, "work", "grouping"),
        "composer": _text(tag.composer),
        "movement": _first(tag, "movement_name", "movementname"),
        "movement_number": _movement_number(_first(tag, "movement")),
        "catalogue": _first(tag, "catalog_number"),
        "label": tag_label(tag),
        "conductor": _first(tag, "conductor"),
        "remixer": _first(tag, "remixer", "tpe4"),
        "recording_year": tag_year(tag),
    }
    return {name: value for name, value in proposal.items() if value is not None}


def read_proposal(path: str | Path) -> dict[str, Any]:
    """Read one file's tags. Raises :class:`~apps.shared._tagreader.TagReadError`."""
    return proposal_from_tag(_tagreader.read(path, duration=False))
