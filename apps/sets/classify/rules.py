"""Rules-based transition classifier (v0).

Plan 12-02 Step 2. Zero-training-data cold-start classifier that runs
off a transition's :attr:`features` dict. Output class subset is
``cut, blend, quick_double, unknown``; ``filter_sweep`` + ``fx`` are
unreachable without knob telemetry (see CONTEXT D4).

Thresholds (RESEARCH §5):

* ``overlap_s < 0.5``                    -> ``(cut, 0.9)``
* ``is_same_deck_reload and fade_s < 1`` -> ``(quick_double, 0.85)``
* ``4 <= overlap_s <= 30``               -> ``(blend, 0.75)``
* ``overlap_s > 30``                     -> ``(blend, 0.6)``
* otherwise                               -> ``(unknown, 0.0)``

Order matters: quick_double runs before blend so a same-deck reload
with a brief overlap stays a quick_double.
"""
from __future__ import annotations

from typing import Tuple

from ..transitions import Transition


def classify(transition: Transition) -> Tuple[str, float]:
    """Return ``(class, confidence)`` per the rules above."""
    f = transition.features
    overlap_s = float(f.get("overlap_s", 0.0))
    fade_s = float(f.get("fade_s", 0.0))
    same_deck = bool(f.get("is_same_deck_reload", 0.0))

    if overlap_s < 0.5:
        return ("cut", 0.9)
    if same_deck and fade_s < 1.0:
        return ("quick_double", 0.85)
    if 4.0 <= overlap_s <= 30.0:
        return ("blend", 0.75)
    if overlap_s > 30.0:
        return ("blend", 0.6)
    return ("unknown", 0.0)


def classify_all(transitions: list[Transition]) -> list[Tuple[str, float]]:
    """Convenience batch call."""
    return [classify(t) for t in transitions]


__all__ = ["classify", "classify_all"]
