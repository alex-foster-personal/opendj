"""Show quality metrics for a planned show (spec 07 section 5, L63).

Pure functions over a :data:`ShowPlan` from ``plan_show`` plus the track's beat grid and sections:

- ``phrase_alignment``: share of look changes within one beat of a section boundary.
- ``palette_entropy``: Shannon bits over the colours shown, weighted by beats on screen.
- ``change_rate``: look changes per minute.
- ``limiter_held_frames``: frames the flash limiter altered (should be 0 for planned shows).
- ``repeat_score``: consecutive tracks whose same section type opens with the same look.

Thresholds are out of scope; they come from the calibration (#9907).
"""

from __future__ import annotations

import bisect
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise

from apps.lighting.safety import FixtureLight, FlashLimiter
from apps.lighting.sections import LightSection, SectionSpan
from apps.lighting.show_plan import ShowPlan

Rgb = tuple[int, int, int]


def look_changes(plan: ShowPlan) -> list[int]:
    """Beat indexes where the plan's look differs from the one before (the first cue counts)."""
    changes: list[int] = []
    previous: str | None = None
    for cue in plan:
        if cue.look_id != previous:
            changes.append(cue.start_beat)
        previous = cue.look_id
    return changes


def _beat_length_s(beats_s: Sequence[float], index: int) -> float:
    if len(beats_s) < 2:
        return 0.0
    i = min(index, len(beats_s) - 2)
    return beats_s[i + 1] - beats_s[i]


def phrase_alignment(plan: ShowPlan, beats_s: Sequence[float], sections: Sequence[SectionSpan]) -> float:
    """Share of look changes within one beat of a section boundary (1.0 when there are no changes)."""
    changes = look_changes(plan)
    if not changes:
        return 1.0
    bounds = sorted(span.start_ms / 1000.0 for span in sections)
    aligned = 0
    for beat in changes:
        t = beats_s[beat]
        i = bisect.bisect_left(bounds, t)
        near = [abs(bounds[j] - t) for j in (i - 1, i) if 0 <= j < len(bounds)]
        if near and min(near) <= _beat_length_s(beats_s, beat) + 1e-9:
            aligned += 1
    return aligned / len(changes)


def palette_entropy(plan: ShowPlan, total_beats: int, colours: Mapping[str, Rgb]) -> float:
    """Shannon entropy in bits of the colour on screen per beat; ``colours`` maps look id to RGB."""
    weights: Counter[Rgb] = Counter()
    for cue, nxt in pairwise((*plan, None)):
        end = nxt.start_beat if nxt is not None else total_beats
        weights[colours[cue.look_id]] += max(0, end - cue.start_beat)
    total = sum(weights.values())
    if total == 0:
        return 0.0
    return max(0.0, -sum(w / total * math.log2(w / total) for w in weights.values() if w))


def change_rate(plan: ShowPlan, duration_s: float) -> float:
    """Look changes per minute over the track, not counting the opening look."""
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s}")
    return max(0, len(look_changes(plan)) - 1) * 60.0 / duration_s


def limiter_held_frames(
    frames: Iterable[tuple[float, Mapping[str, FixtureLight]]], limiter: FlashLimiter | None = None
) -> int:
    """Count frames whose limiter output differs from the rendered input (flash vetoes)."""
    limiter = limiter if limiter is not None else FlashLimiter()
    held = 0
    for now, frame in frames:
        if limiter.limit(now, frame) != dict(frame):
            held += 1
    return held


def opening_looks(plan: ShowPlan, beats_s: Sequence[float], sections: Sequence[SectionSpan]) -> dict[LightSection, str]:
    """Look on screen at the first span of each section type."""
    out: dict[LightSection, str] = {}
    starts = [cue.start_beat for cue in plan]
    for span in sorted(sections, key=lambda s: s.start_ms):
        if span.section in out or not plan:
            continue
        beat = min(range(len(beats_s)), key=lambda b: abs(beats_s[b] - span.start_ms / 1000.0))
        i = bisect.bisect_right(starts, beat) - 1
        out[span.section] = plan[max(i, 0)].look_id
    return out


def repeat_score(tracks: Sequence[Mapping[LightSection, str]]) -> int:
    """Consecutive track pairs, per section type, that open the section with the same look."""
    return sum(
        1
        for a, b in pairwise(tracks)
        for section, look in a.items()
        if section is not LightSection.UNKNOWN and b.get(section) == look
    )
