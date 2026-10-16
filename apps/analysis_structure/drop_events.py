"""Drop, break and build MOMENTS from DJ-labeled sections, plus merged drop spans.

WHY. "The drop" is said of two things: the downbeat where the kick and bass
come back in after a break or build, and the full-energy span that follows.
``dj_labels.py`` labels spans only, and labels every full section ``drop``, so
a long drop the segmenter cut in two read as two drops. This module adds the
moment and fixes the count, from the labels already assigned, without
changing any label. The rules come from the project proposal "What the drop
means" (Fri 9 Oct 2026, ``segmentation/drop-labels-proposal.md`` in the
project files).

THE RULES, deterministic:

1. A ``drop`` section right after another ``drop`` section is the same drop:
   it gets ``continuation: true`` and no moment of its own.
2. A ``drop`` moment is the start of a non-continuation ``drop`` section whose
   previous section is a ``buildup`` or scored at or below ``WITHDRAWN_DB``
   (drums or bass clearly out). A drop with nothing quieter before it (the
   track opens full) has no moment: there was nothing to drop back in from.
3. A fake drop: a drop shorter than ``FAKE_MAX_BARS`` bars followed by a
   withdrawn section gets ``fake: true`` and no number; the next real drop
   takes the number.
4. Real drops are numbered 1, 2, ... in time order. Drop 1 is the one a
   "time to drop" feature uses.
5. A ``break`` moment is the start of a ``breakdown`` right after a drop, a
   ``build`` moment the start of every ``buildup``.

Section starts are already downbeats of the track's own grid (both producers
cut on it), so a moment's time and bar are its section's start, unmoved.

Pure stdlib, like ``dj_labels.py``.

-Claude
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .dj_labels import WITHDRAWN_DB

FAKE_MAX_BARS = 4


def _score(section: dict[str, Any]) -> float | None:
    energy = section.get("energy") or {}
    score = energy.get("score_db")
    return float(score) if score is not None else None


def _withdrawn(section: dict[str, Any]) -> bool:
    score = _score(section)
    return score is not None and score <= WITHDRAWN_DB


def _fake(sections: Sequence[dict[str, Any]], i: int) -> bool:
    """Rule 3 over the WHOLE drop: the contiguous run of ``drop`` sections from ``i``
    (rule 1 makes it one drop), and the first section after that run."""
    total = 0
    j = i
    while j < len(sections) and sections[j].get("dj_label") == "drop":
        bars = sections[j].get("bars")
        if bars is None:
            return False  # an unmeasured length is never called short
        total += bars
        j += 1
    nxt = sections[j] if j < len(sections) else None
    return total < FAKE_MAX_BARS and nxt is not None and _withdrawn(nxt)


def _moment(kind: str, section: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "time_s": section["start_s"],
        # Required key: a section without one is malformed and fails here. Its
        # value is None only for All-In-One's lead section at 0 s, which
        # quantize.py documents as not a measured downbeat.
        "bar": section["start_bar"],
        **extra,
    }


def _starts_a_drop(section: dict[str, Any], prev: dict[str, Any] | None) -> bool:
    """Rules 1 and 2 for a ``drop`` section whose ``continuation`` is already set.

    Rule 1 first and unconditionally: a drop span's energy score can sit at or
    below WITHDRAWN_DB, and its continuation is still not a drop.
    """
    if section["continuation"] or prev is None:
        return False
    return prev.get("dj_label") == "buildup" or _withdrawn(prev)


def mark_drops(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set ``continuation`` on drop sections in place; return the moments in time order.

    ``sections`` must carry ``dj_label`` (an unlabeled track has no moments).
    """
    moments: list[dict[str, Any]] = []
    number = 0
    for i, section in enumerate(sections):
        label = section.get("dj_label")
        prev = sections[i - 1] if i > 0 else None
        prev_label = prev.get("dj_label") if prev else None
        if label == "drop":
            section["continuation"] = prev_label == "drop"
            if not _starts_a_drop(section, prev):
                continue
            if _fake(sections, i):
                moments.append(_moment("drop", section, number=None, fake=True))
                continue
            number += 1
            moments.append(_moment("drop", section, number=number, fake=False))
        elif label == "breakdown" and prev_label == "drop":
            moments.append(_moment("break", section))
        elif label == "buildup":
            moments.append(_moment("build", section))
    return moments


__all__ = ["FAKE_MAX_BARS", "mark_drops"]
