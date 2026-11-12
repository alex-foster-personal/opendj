"""Stable A/B/C clip ids for stem split labelling (issue #11193)."""

from __future__ import annotations

STEMS_AB_CLIP_IDS: tuple[str, ...] = ("A", "B", "C")


class StemAbcIdError(ValueError):
    """Raised when the experiment arm set cannot support an ABC rank task."""


def stems_ab_clip_ids_for_arms(arm_count: int) -> tuple[str, ...]:
    if arm_count < len(STEMS_AB_CLIP_IDS):
        raise StemAbcIdError("stems_ab_rank needs at least three experiment arms")
    return STEMS_AB_CLIP_IDS


__all__ = ["STEMS_AB_CLIP_IDS", "StemAbcIdError", "stems_ab_clip_ids_for_arms"]
