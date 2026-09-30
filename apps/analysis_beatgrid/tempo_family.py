"""Genre tempo families and half-time folding: the fast-genre half of the octave policy.

WHY THIS EXISTS. Drum and bass sits at 160 to 180 BPM, and its drums read just
as well at half that (a kick-snare pattern at 174 is a steady pulse at 87). Our
70-180 BPM band holds BOTH octaves, so `bpm.choose_octave` used to break the tie
toward the band's geometric center (112 BPM) and published 87 for a track every
DJ calls 174. Round 4 measured the damage: all 6 fixed-tempo rekordbox fixtures
at 163 to 175 BPM that the model tracked at the fast level were published at
half (`ops/beatbench/round-4/README.md`, "The octave experiment is not served").

Two separate mechanisms fix two separate failures:

1. TEMPO FAMILY FROM THE GENRE TAG. A track tagged drum and bass, jungle,
   footwork, hardcore, psytrance, dubstep or hardstyle has a conventional tempo
   range, and exactly one octave of the model's pulse lands in it. The tag is
   the user's own metadata (file tag or library genre field), NOT rekordbox's
   analysis, so using it at runtime keeps the own producer honest in the sense
   `bpm.py` enforces: rekordbox's stored BPM stays scoring-only. A family only
   ever picks among octaves (x0.25 to x4) of the tempo the model found; it can
   never invent a pulse, and when no octave lands in its range it is ignored
   and the estimate says so.

2. HALF-TIME FOLDING. A drum and bass breakdown or a dubstep-style half-time
   section often makes the model drop to every other beat for a stretch. The
   tempo-change detector then reads a 174 -> 87 -> 174 "tempo change" and the
   grid fit serves a two-line grid with bogus markers. `fold_half_time` fills
   those stretches back in at the dominant beat spacing BEFORE the fit, so an
   octave-related section can never become a tempo change on its own. It only
   fills intervals that are a whole 2x to 4x of the dominant spacing within
   `FOLD_TOLERANCE`, so a real tempo change (never an exact integer ratio for
   a stretch of beats) is left alone.

The family table is deliberately separate from `apps/library_wheel/
genre_families.py` (LIBUX-06, the 16 display families). That table groups
genres by how they LOOK in the library; this one groups them by the tempo a DJ
expects, which splits some of those families (psytrance is not trance at 128,
hardcore is not hardstyle at 150) and ignores genres whose tempo is not a
reliable octave hint (house, techno, hip-hop, pop).

Stdlib only and pure, like the rest of the policy code.

-Claude
"""

from __future__ import annotations

import itertools
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TempoFamily:
    """A genre group and the BPM range a DJ expects its tracks to be published at."""

    name: str
    pattern: re.Pattern[str]
    min_bpm: float
    max_bpm: float

    def contains(self, bpm: float) -> bool:
        return self.min_bpm <= bpm <= self.max_bpm


def _family(name: str, pattern: str, min_bpm: float, max_bpm: float) -> TempoFamily:
    return TempoFamily(name, re.compile(pattern, re.IGNORECASE), min_bpm, max_bpm)


#: First match wins, so the narrower sub-genres come before their parents
#: (hi-tech psy before psytrance). Every range is narrower than one octave
#: (max/min < 2), so at most one octave of any pulse can land inside it: the
#: family decides the octave, never a tie between two.
TEMPO_FAMILIES: tuple[TempoFamily, ...] = (
    _family(
        "drum-and-bass",
        r"\b(drum\s*(?:&|and|n|'n'|\+)\s*bass|d\s*&\s*b|d'?n'?b|dnb|jungle|neurofunk|"
        r"liquid\s*funk|jump[- ]?up|halftime)\b",
        160.0,
        185.0,
    ),
    _family("footwork", r"\b(footwork|juke)\b", 150.0, 170.0),
    _family(
        "hardcore",
        r"\b(happy\s*hardcore|uk\s*hardcore|gabber|frenchcore|uptempo|terrorcore|speedcore)\b",
        160.0,
        240.0,
    ),
    _family("hi-tech-psy", r"\b(hi[- ]?tech|dark\s*psy|forest\s*psy)\b", 145.0, 220.0),
    _family(
        "psytrance",
        r"\b(psy[- ]?trance|psychedelic\s*trance|goa(?:\s*trance)?|full[- ]?on|psy)\b",
        134.0,
        152.0,
    ),
    _family("dubstep", r"\b(dubstep|riddim|brostep|tearout)\b", 135.0, 152.0),
    _family("hardstyle", r"\b(hardstyle|rawstyle|euphoric\s*hardstyle)\b", 145.0, 162.0),
)


def tempo_family_for_genre(genre: str | None) -> TempoFamily | None:
    """The tempo family a genre tag belongs to, or None for no tag or no match.

    None is the common case (house, techno, pop, an empty tag) and means "no
    hint": the band rules in `bpm.choose_octave` decide as before.
    """
    if genre is None or not genre.strip():
        return None
    for family in TEMPO_FAMILIES:
        if family.pattern.search(genre):
            return family
    return None


def tempo_family_by_name(name: str) -> TempoFamily:
    """The family called `name`; raises for an unknown name rather than guessing."""
    for family in TEMPO_FAMILIES:
        if family.name == name:
            return family
    raise KeyError(f"no tempo family named {name!r}; known: {[f.name for f in TEMPO_FAMILIES]}")


# ----- Half-time folding ---------------------------------------------------

#: An interval within this fraction of a whole multiple of the dominant beat
#: spacing is a run of skipped beats. 8% is about 28 ms at 174 BPM: past the
#: model's 20 ms frame, well short of the 50% a real tempo change would need
#: to look like an exact 2:1.
FOLD_TOLERANCE = 0.08

#: Largest gap filled, in beats. A half-time section skips 1 of 2; a
#: quarter-time build skips 3 of 4. Longer gaps are silence, not metre.
FOLD_MAX_MULTIPLE = 4

#: Fewest intervals the dominant spacing is measured from.
FOLD_MIN_INTERVALS = 8


@dataclass(frozen=True)
class FoldResult:
    """Beats with skipped-beat gaps filled, and how many beats were added."""

    beats: list[float]
    n_filled: int
    period_s: float | None


def fold_half_time(beats: Sequence[float]) -> FoldResult:
    """Fill every gap of 2 to 4 dominant beat spacings with evenly spaced beats.

    The dominant spacing is the median interval, so the fold always goes TOWARD
    the level the model tracked most of the track at. A track that is mostly
    half-time keeps its half-time spacing here, and its octave is then the
    policy's choice (genre family or band), not this function's.
    """
    ts = [float(t) for t in beats]
    intervals = [b - a for a, b in itertools.pairwise(ts)]
    if len(intervals) < FOLD_MIN_INTERVALS:
        return FoldResult(ts, 0, None)
    period = statistics.median(intervals)
    if not period > 0:
        return FoldResult(ts, 0, None)
    out = ts[:1]
    filled = 0
    for a, b in itertools.pairwise(ts):
        gap = b - a
        multiple = round(gap / period)
        if (
            2 <= multiple <= FOLD_MAX_MULTIPLE
            and abs(gap / (multiple * period) - 1.0) <= FOLD_TOLERANCE
        ):
            out.extend(a + gap * j / multiple for j in range(1, multiple))
            filled += multiple - 1
        out.append(b)
    return FoldResult(out, filled, period)


__all__ = [
    "FOLD_MAX_MULTIPLE",
    "FOLD_TOLERANCE",
    "TEMPO_FAMILIES",
    "FoldResult",
    "TempoFamily",
    "fold_half_time",
    "tempo_family_by_name",
    "tempo_family_for_genre",
]
