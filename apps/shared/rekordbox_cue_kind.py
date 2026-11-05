"""Canonical ``djmdCue.Kind`` ↔ hot-cue slot letter map (issue #4069).

Open DJ uses ``KIND_BY_SLOT`` (A-H → Kind 1-8) for ingest, legacy djmdCue
writes, and read paths. The D-H integer values are **unproven** until a
rekordbox-UI disposable-track experiment records whether slot D is Kind 4 or 5
(and H is 8 or 9). Do not infer a 5-9 remap from library histograms alone.

Kind 9-11 rows stay unmapped (issue #263 Rejected for I/J/K slots).

ANLZ PCOB/PCO2 ``hot_cue`` pad indices use a different numbering; that path
must not import this module.
"""

from __future__ import annotations

HOT_CUE_SLOTS: str = "ABCDEFGH"
MEMORY_KIND: int = 0
# Conservative mapping. D=4 and H=8 until the #4069 experiment lands.
# Do not infer a 5-9 remap from the Kind histogram.
KIND_BY_SLOT: dict[str, int] = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
}
SLOT_BY_KIND: dict[int, str] = {kind: slot for slot, kind in KIND_BY_SLOT.items()}
HOT_CUE_KINDS: frozenset[int] = frozenset(KIND_BY_SLOT.values())
MAPPED_KIND_MAX: int = max(HOT_CUE_KINDS)


class HotCueSlotError(ValueError):
    """Unknown/unsupported hot-cue slot letter."""


def slot_to_kind(slot: str) -> int:
    if len(slot) != 1 or slot not in KIND_BY_SLOT:
        raise HotCueSlotError(
            f"unsupported hot-cue slot {slot!r}; only {HOT_CUE_SLOTS} are "
            "write-supported (Kind 9-11 slot mapping unverified, see "
            "PARITY-TODO.md 'Hot-cue SAVE')"
        )
    return KIND_BY_SLOT[slot]


def kind_to_slot(kind: int) -> str | None:
    return SLOT_BY_KIND.get(int(kind))


def is_mapped_kind(kind: int) -> bool:
    return MEMORY_KIND <= int(kind) <= MAPPED_KIND_MAX
