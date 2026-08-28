"""The two row shapes the rekordbox read path hands out.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C0 (source lines
172-195 on ``af--t4-design``) per ``.planning/t3b-decomposition-map.md``
section 2 target #1.

Both field shapes are a public contract, not an internal detail:
``tests/test_reconcile_route.py`` and ``tests/test_relocate_route.py``
construct :class:`RbRowMeta` directly and monkeypatch ``bulk_rb_meta`` to
return it, so renaming or reordering a field breaks callers that never
import this module.
"""
from __future__ import annotations

from dataclasses import dataclass


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


__all__ = ["RbContent", "RbRowMeta"]
