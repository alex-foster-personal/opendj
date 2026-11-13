"""The two row shapes the rekordbox read path hands out.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C0 (source lines
172-195 on ``af--t4-design``) per ``(private path)``
section 2 target #1.

Both field shapes are a public contract, not an internal detail:
``(private path)`` and ``(private path)``
construct :class:`RbRowMeta` directly and monkeypatch ``bulk_rb_meta`` to
return it, so renaming or reordering a field breaks callers that never
import this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

VendorUsbSource = Literal["rekordbox", "djay"]


@dataclass(frozen=True)
class VendorUsbMention:
    """A read-only USB stick name the user set in a DJ vendor library."""

    name: str
    source: VendorUsbSource
    volume_uuid: str | None = None
    device_id: str | None = None


@dataclass(frozen=True)
class RbContent:
    """One resolved djmdContent row (only the columns rb-assets needs)."""

    stable_id: str
    vendor_id: str
    folder_path: str | None
    image_path: str | None
    analysis_data_path: str | None
    length_s: int | None
    comment: str | None
    genre: str | None


@dataclass(frozen=True)
class RbRowMeta:
    """Rekordbox columns a hydrated track row needs (bulk-resolved)."""

    vendor_id: str
    folder_path: str | None
    analysis_data_path: str | None
    comment: str | None
    genre: str | None
    play_count: int
    # ImagePath is included in the bulk listing query so artwork availability
    # can be decided without a second /rb-meta request per visible table row.
    image_path: str | None = None
    # LIBUX-13803: djmdContent.DateCreated, the day rekordbox added the track.
    date_created: str | None = None
    # Issue #9737: djmdContent.ColorID, the track colour label 1..8 (0 or null: none).
    color_id: int | None = None


__all__ = ["RbContent", "RbRowMeta", "VendorUsbMention", "VendorUsbSource"]
