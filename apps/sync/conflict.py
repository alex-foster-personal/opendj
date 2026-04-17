"""Phase 4 conflict resolver (D6).

Given a field, the RB and djay values, and optional per-side modified
timestamps, decide which side wins. Default policy: "newest-wins" inside
a 7-day window, else fall back to ``prefer`` (``rb`` | ``djay`` |
``newest``). Returns one of ``accept_rb``, ``accept_djay``, ``conflict``,
``no_change``.

Per-field hooks:
- ``rating``: zero-is-unrated. 0 + non-zero always accepts non-zero.
- ``bpm`` / ``manual_bpm``: manual-override side wins when the other is empty.

Cue-array conflicts are handled separately in Plan 3's
``resolve_cue_array``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

ConflictResolution = Literal["accept_rb", "accept_djay", "conflict", "no_change"]

DEFAULT_WINDOW_DAYS = 7


@dataclass(slots=True, frozen=True)
class ConflictInput:
    field: str
    rb_value: Any
    djay_value: Any
    rb_modified_at: datetime | None = None
    djay_modified_at: datetime | None = None
    prefer: Literal["rb", "djay", "newest"] = "newest"
    newest_window_days: int = DEFAULT_WINDOW_DAYS


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value:
        return True
    return False


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return a == b
    return a == b


def _within_window(
    a: datetime | None, b: datetime | None, window_days: int
) -> bool:
    if a is None or b is None:
        return False
    delta = abs((a - b).total_seconds())
    return delta <= window_days * 24 * 3600


def _newer(a: datetime | None, b: datetime | None) -> Literal["rb", "djay", "tie"]:
    if a is None and b is None:
        return "tie"
    if a is None:
        return "djay"
    if b is None:
        return "rb"
    if a > b:
        return "rb"
    if b > a:
        return "djay"
    return "tie"


def resolve_conflict(
    field: str,
    rb_value: Any,
    djay_value: Any,
    rb_modified_at: datetime | None = None,
    djay_modified_at: datetime | None = None,
    *,
    prefer: Literal["rb", "djay", "newest"] = "newest",
    newest_window_days: int = DEFAULT_WINDOW_DAYS,
) -> ConflictResolution:
    """Return the resolution for a single scalar field."""
    if _equal(rb_value, djay_value):
        return "no_change"

    if field == "rating":
        rb_zero = not bool(rb_value) if rb_value is not None else True
        djay_zero = not bool(djay_value) if djay_value is not None else True
        if rb_zero and not djay_zero:
            return "accept_djay"
        if djay_zero and not rb_zero:
            return "accept_rb"

    if field in ("bpm", "manual_bpm"):
        if _is_empty(rb_value) and not _is_empty(djay_value):
            return "accept_djay"
        if _is_empty(djay_value) and not _is_empty(rb_value):
            return "accept_rb"

    if _is_empty(rb_value) and not _is_empty(djay_value):
        return "accept_djay"
    if _is_empty(djay_value) and not _is_empty(rb_value):
        return "accept_rb"

    if prefer == "rb":
        side = _newer(rb_modified_at, djay_modified_at)
        if side == "djay":
            return "conflict"
        return "accept_rb"
    if prefer == "djay":
        side = _newer(rb_modified_at, djay_modified_at)
        if side == "rb":
            return "conflict"
        return "accept_djay"

    if _within_window(rb_modified_at, djay_modified_at, newest_window_days):
        side = _newer(rb_modified_at, djay_modified_at)
        if side == "rb":
            return "accept_rb"
        if side == "djay":
            return "accept_djay"
        return "conflict"

    side = _newer(rb_modified_at, djay_modified_at)
    if side == "djay":
        return "conflict"
    return "accept_rb"


# ----- Plan 3: cue-array resolver --------------------------------------


@dataclass(slots=True, frozen=True)
class CueArrayResolution:
    """Three-way split of a cue diff into actionable buckets."""

    rb_additions: tuple  # cues to add to RB (came from djay)
    djay_additions: tuple  # cues to add to djay (came from RB)
    conflicts: tuple  # ``(rb_cue, djay_cue)`` pairs with attribute mismatches


def resolve_cue_array(
    rb_cues,
    djay_cues,
    *,
    tolerance_msec: int = 20,
) -> CueArrayResolution:
    """Union two cue lists with ±tolerance_msec matching on position+kind+index.

    Returns::

        (rb_additions, djay_additions, conflicts)

    ``rb_additions`` are djay cues that have no RB match (so we'd add to RB).
    ``djay_additions`` are RB cues that have no djay match (so we'd add to djay).
    ``conflicts`` are near-matches (within tolerance and same kind/index)
    whose metadata (name, colour, loop length) differs. Hot-cue index
    collisions (same slot, positions > tolerance apart) also go into conflicts.
    """
    rb_matched: set[int] = set()
    dj_matched: set[int] = set()
    conflicts: list = []

    def _position_hit(a, b):
        if a.kind != b.kind:
            return False
        if abs(a.position_msec - b.position_msec) > tolerance_msec:
            return False
        if a.kind == "hot" and a.index is not None and b.index is not None:
            return a.index == b.index
        return True

    for i, a in enumerate(rb_cues):
        for j, b in enumerate(djay_cues):
            if j in dj_matched:
                continue
            if _position_hit(a, b):
                rb_matched.add(i)
                dj_matched.add(j)
                if (
                    (a.name or "") != (b.name or "")
                    or a.color_rgb != b.color_rgb
                    or (a.loop_length_msec or 0) != (b.loop_length_msec or 0)
                ):
                    conflicts.append((a, b))
                break

    # Hot-cue index collisions: same kind="hot" and same index but
    # positions > tolerance apart -> conflict (don't silently move).
    for i, a in enumerate(rb_cues):
        if i in rb_matched or a.kind != "hot" or a.index is None:
            continue
        for j, b in enumerate(djay_cues):
            if j in dj_matched or b.kind != "hot" or b.index is None:
                continue
            if a.index == b.index:
                conflicts.append((a, b))
                rb_matched.add(i)
                dj_matched.add(j)
                break

    rb_additions = tuple(b for j, b in enumerate(djay_cues) if j not in dj_matched)
    djay_additions = tuple(a for i, a in enumerate(rb_cues) if i not in rb_matched)

    return CueArrayResolution(
        rb_additions=rb_additions,
        djay_additions=djay_additions,
        conflicts=tuple(conflicts),
    )


__all__ = [
    "ConflictResolution",
    "ConflictInput",
    "CueArrayResolution",
    "resolve_conflict",
    "resolve_cue_array",
    "DEFAULT_WINDOW_DAYS",
]
