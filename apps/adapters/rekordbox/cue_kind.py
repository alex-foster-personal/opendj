"""Re-export ``apps.shared.rekordbox_cue_kind`` for rekordbox adapter callers (issue #4069).

The map lives in ``apps.shared`` so ingest and ``rekordbox_db`` can use it without
violating the import-linter ``shared-is-the-stable-core`` contract.
"""
from __future__ import annotations

from apps.shared.rekordbox_cue_kind import (
    HOT_CUE_KINDS,
    HOT_CUE_SLOTS,
    KIND_BY_SLOT,
    MAPPED_KIND_MAX,
    MEMORY_KIND,
    SLOT_BY_KIND,
    HotCueSlotError,
    is_mapped_kind,
    kind_to_slot,
    slot_to_kind,
)

__all__ = [
    "HOT_CUE_KINDS",
    "HOT_CUE_SLOTS",
    "KIND_BY_SLOT",
    "MAPPED_KIND_MAX",
    "MEMORY_KIND",
    "SLOT_BY_KIND",
    "HotCueSlotError",
    "is_mapped_kind",
    "kind_to_slot",
    "slot_to_kind",
]
