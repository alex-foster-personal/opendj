"""Bar quantizer: snap model section boundaries onto the track's own downbeat grid.

WHY THIS EXISTS. A segmentation model (All-In-One today) emits section
boundaries in seconds. A DJ reads structure in bars and phrases, and the
research behind this lane found bar alignment worth more than several years of
model progress on bar-regular music
(`docs/research/beatgrid-and-segmentation-sota-20260906-b-segmentation.md`,
Q2 and Q5 Tier 0). So the model's own beats and downbeats are DISCARDED: only
its boundaries and labels survive, and each boundary is snapped to the
downbeat grid the beatgrid lane already measured, then to a phrase multiple.

THE SNAP IS RECORDED, NEVER HIDDEN. Every boundary keeps its raw time, the
distance it moved, and whether it landed on a phrase. A boundary that would
have to move more than ``max_move_bars`` bars to reach a downbeat is left
where the model put it and flagged ``unsnapped``, because silently dragging it
a bar or more manufactures a phrase start the audio does not have.

THE PHRASE OFFSET IS VOTED, NOT ASSUMED. Counting phrases from the first
downbeat breaks on every track whose intro is not a whole number of phrases
(a 2-bar pickup, a 6-bar intro). The offset is the residue, modulo the phrase
length, that the most downbeat-snapped boundaries already share, so the grid
follows the music rather than bar 1.

Pure stdlib: no numpy, no torch. It runs inside the app venv and is exercised
by unit tests; the model that produces the input runs as a PEP 723 script.

-Claude
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from statistics import median
from typing import Any

# Labels All-In-One uses for the silence before and after the music. They are
# boundaries of the track, not of the song's structure.
EDGE_LABELS = frozenset({"start", "end"})

# The research's Tier 1 triggers for "do not trust this track's structure":
# a section shorter than 4 bars, or fewer than 3 boundaries across 6 minutes.
MIN_SECTION_BARS = 4
SPARSE_WINDOW_S = 360.0
SPARSE_MIN_BOUNDARIES = 3


@dataclass(frozen=True)
class Boundary:
    """One section start after quantization."""

    raw_s: float
    time_s: float
    label: str
    bar: int | None  # downbeat index, None when unsnapped
    moved_s: float
    phrase_aligned: bool
    status: str  # "snapped" | "unsnapped"


@dataclass(frozen=True)
class Structure:
    """The quantized section list plus everything needed to audit it."""

    boundaries: list[Boundary]
    phrase_bars: int
    phrase_offset: int | None
    merged: int
    implausible: list[str] = field(default_factory=list)

    def sections(self, duration_s: float) -> list[dict[str, Any]]:
        """Sections as ``{start_s, end_s, label, start_bar, bars}``, PSSI-shaped."""
        out: list[dict[str, Any]] = []
        for i, b in enumerate(self.boundaries):
            nxt = self.boundaries[i + 1] if i + 1 < len(self.boundaries) else None
            end_s = nxt.time_s if nxt else duration_s
            bars = (
                (nxt.bar - b.bar) if (nxt and nxt.bar is not None and b.bar is not None) else None
            )
            out.append(
                {
                    "start_s": round(b.time_s, 3),
                    "end_s": round(end_s, 3),
                    "label": b.label,
                    "start_bar": b.bar,
                    "bars": bars,
                }
            )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundaries": [asdict(b) for b in self.boundaries],
            "phrase_bars": self.phrase_bars,
            "phrase_offset": self.phrase_offset,
            "merged": self.merged,
            "implausible": list(self.implausible),
        }


def _nearest(grid: Sequence[float], t: float) -> int:
    i = bisect.bisect_left(grid, t)
    if i == 0:
        return 0
    if i == len(grid):
        return len(grid) - 1
    return i if grid[i] - t < t - grid[i - 1] else i - 1


def _local_bar(grid: Sequence[float], i: int, fallback: float, half_window: int = 4) -> float:
    """Median bar length around downbeat ``i``.

    A median over the neighboring bars, not the two gaps touching ``i``: a
    missed downbeat leaves one double-length gap, and reading that as the bar
    length would let a boundary move two bars and still count as snapped.
    The window still follows a tempo change, which a global median would not.
    """
    lo, hi = max(0, i - half_window), min(len(grid) - 1, i + half_window)
    gaps = [grid[k + 1] - grid[k] for k in range(lo, hi)]
    return median(gaps) if gaps else fallback


def vote_phrase_offset(bars: Sequence[int], phrase_bars: int) -> int:
    """The residue mod ``phrase_bars`` most snapped boundaries share; ties go to 0."""
    counts = [0] * phrase_bars
    for b in bars:
        counts[b % phrase_bars] += 1
    best = max(counts)
    return next(k for k in range(phrase_bars) if counts[k] == best)


def quantize(
    segments: Sequence[tuple[float, float, str]],
    downbeats: Sequence[float],
    *,
    phrase_bars: int = 4,
    max_move_bars: float = 1.0,
    max_phrase_move_bars: int = 1,
    phrase_offset: int | None = None,
) -> Structure:
    """Snap segment starts to ``downbeats``, then to ``phrase_bars`` multiples.

    ``segments`` are ``(start_s, end_s, label)`` from the model. ``downbeats``
    come from the beatgrid lane, never from the segmentation model.
    ``phrase_bars=1`` disables the phrase step (downbeat snap only).
    ``phrase_offset=None`` votes the offset; an int pins it (0 = count from
    the first downbeat).
    """
    if phrase_bars < 1:
        raise ValueError("phrase_bars must be >= 1")
    grid = sorted(float(t) for t in downbeats)
    starts = _model_starts(segments)
    if len(grid) < 2:
        # No grid to snap to: keep the model's times and say so.
        raw = [Boundary(s, s, lab, None, 0.0, False, "unsnapped") for s, lab in starts]
        return Structure(raw, phrase_bars, None, 0, ["no_downbeat_grid"] if starts else [])
    first = _downbeat_snap(starts, grid, max_move_bars)
    offset = _phrase_offset(first, phrase_bars, phrase_offset)

    out: list[Boundary] = []
    seen_bars: set[int] = set()
    merged = 0
    for s, lab, downbeat in first:
        if downbeat is None:
            out.append(Boundary(s, s, lab, None, 0.0, False, "unsnapped"))
            continue
        bar, aligned = _phrase_snap(downbeat, len(grid), phrase_bars, offset, max_phrase_move_bars)
        if bar in seen_bars:
            merged += 1  # two model boundaries collapsed onto one bar; keep the first
            continue
        seen_bars.add(bar)
        out.append(Boundary(s, grid[bar], lab, bar, abs(grid[bar] - s), aligned, "snapped"))
    out.sort(key=lambda b: b.time_s)

    return Structure(out, phrase_bars, offset, merged, _implausible(out, grid))


def _model_starts(segments: Sequence[tuple[float, float, str]]) -> list[tuple[float, str]]:
    """Section starts inside the song: the track's own start and the silence labels drop out."""
    return sorted(
        (float(s), str(label))
        for s, _e, label in segments
        if float(s) > 0.0 and str(label) not in EDGE_LABELS
    )


def _downbeat_snap(
    starts: Sequence[tuple[float, str]], grid: Sequence[float], max_move_bars: float
) -> list[tuple[float, str, int | None]]:
    """Each start with its nearest downbeat index, or None when that is too far to move."""
    fallback_bar = median(b - a for a, b in pairwise(grid))
    first: list[tuple[float, str, int | None]] = []
    for s, lab in starts:
        i = _nearest(grid, s)
        near = abs(grid[i] - s) <= max_move_bars * _local_bar(grid, i, fallback_bar)
        first.append((s, lab, i if near else None))
    return first


def _phrase_offset(
    first: Sequence[tuple[float, str, int | None]], phrase_bars: int, pinned: int | None
) -> int | None:
    if phrase_bars == 1:
        return None
    if pinned is not None:
        return pinned
    snapped_bars = [i for _s, _l, i in first if i is not None]
    return vote_phrase_offset(snapped_bars, phrase_bars) if snapped_bars else 0


def _phrase_snap(
    downbeat: int, n_bars: int, phrase_bars: int, offset: int | None, max_move: int
) -> tuple[int, bool]:
    """(bar, phrase_aligned) after moving at most ``max_move`` bars onto a phrase start."""
    if phrase_bars == 1 or offset is None:
        return downbeat, phrase_bars == 1
    j = offset + round((downbeat - offset) / phrase_bars) * phrase_bars
    if 0 <= j < n_bars and abs(j - downbeat) <= max_move:
        return j, True
    return downbeat, False


def _implausible(bounds: Sequence[Boundary], grid: Sequence[float]) -> list[str]:
    reasons: list[str] = []
    if any(b.status == "unsnapped" for b in bounds):
        reasons.append("boundary_moved_over_a_bar")
    bars = [b.bar for b in bounds if b.bar is not None]
    if any(b - a < MIN_SECTION_BARS for a, b in pairwise(bars)):
        reasons.append("section_under_4_bars")
    span = grid[-1] - grid[0]
    if span >= SPARSE_WINDOW_S and len(bounds) < SPARSE_MIN_BOUNDARIES * span / SPARSE_WINDOW_S:
        reasons.append("too_few_boundaries")
    return reasons


__all__ = ["Boundary", "Structure", "quantize", "vote_phrase_offset"]
