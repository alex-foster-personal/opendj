"""Edge sampler pure slot core: grid snap, region peaks, Camelot distance, ranking, slot ids.

Spec: ``(private path)`` sections 3.2 (what a slot carries, grid rule) and 3.4 (ranking).
No I/O: the catalogue (``sampler_catalogue.py``) feeds it grids, waveforms and slot rows.

Mini-PRD:
  ✔︎ ✅ 🎯 a region snaps to the track's EXISTING grid, never an invented one
    [if] a loop starts 30 ms after a beat [then] in_ms is that beat and out_ms the beat ``beats`` later [else ⛔️]
    [if] a grid has under 2 beats or a non-increasing beat [then] it is not usable [else ⛔️]
    [if] a region is shorter than one beat or runs past the grid [then] it yields no region [else ⛔️]
  ✔︎ ✅ 🎯 peaks are PEAK_POINTS max-peaks per band over exactly the region, 0..1
    [if] a band value is outside 0..1 or a band is empty [then] ValueError [else ⛔️]
  ✔︎ ✅ 🎯 slot_id is "<stable_id>:<source>:<in_ms>-<out_ms>" and parses back, ':' in stable ids included
  ✔︎ ✅ 🎯 ``compat`` ranks BPM ratio inside BPM_WINDOW first, then Camelot distance, then BPM distance
    [if] an out-of-window slot would rank above an in-window one [then] broken [else ⛔️]
    [if] the same slots arrive in any order [then] the ranking is identical (stable pages) [else ⛔️]
  ✔︎ ✅ 🎯 ``crate`` groups by the track's first crate in library order, ``color`` by palette order
    [if] sort=crate [then] a crate's slots are contiguous, crates in library order, crate-less last [else ⛔️]
    [if] sort=color [then] slots group by ascending ColorTableIndex, colourless (None or 0) last [else ⛔️]
"""

from __future__ import annotations

import bisect
import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from apps.shared.harmonic import CamelotKey, key_to_camelot
from apps.shared.harmonic import camelot_distance as wheel_steps

#: Mirrors LOOP_CUE_COLOR in apps/webui/frontend/src/lib/components/rb/deck/strip-waveform-render.ts:
#: the deck paints every stored loop hot cue in it, so the edge strip uses the same color.
LOOP_CUE_COLOR = "#e8a13a"
PEAK_POINTS = 120
#: The sync-lock window Beat Sync uses to refuse a follower (spec 3.4).
BPM_WINDOW = (0.84, 1.16)
#: Camelot distance of a slot or master whose key is unknown: it ranks after every known key.
UNKNOWN_KEY_DISTANCE = 99
SOURCE_LOOP, SOURCE_MEMORY, SOURCE_CAPTURE, SOURCE_STARTER = "loop_hot_cue", "memory_loop", "capture", "starter"

#: rekordbox ColorTableIndex 0 is "no colour" (``apps/shared/rb_color_palette.py``).
NO_CUE_COLOR = 0

SlotSort = Literal["compat", "track", "recent", "crate", "color"]


@dataclass(frozen=True)
class TrackAnalysis:
    """The grid and whole-track 3-band detail waveform ``/anlz`` serves the deck."""

    beats_ms: list[float]
    bands: dict[str, list[float]]  # low, mid, high, same length, 0..1, spanning duration_ms
    duration_ms: float


@dataclass(frozen=True)
class Region:
    """A grid-snapped slot region: whole beats from the track's own beat times."""

    in_ms: int
    out_ms: int
    beats: int
    native_bpm: float


@dataclass(frozen=True)
class SlotId:
    """The parts of a slot id ``<stable_id>:<source>:<in_ms>-<out_ms>``."""

    stable_id: str
    source: str
    in_ms: int
    out_ms: int


# ---------- grid snap ----------------------------------------------------------------


def usable_grid(beats_ms: Sequence[float]) -> bool:
    """True when the grid has at least two strictly increasing beats."""
    return len(beats_ms) >= 2 and all(b > a for a, b in itertools.pairwise(beats_ms))


def _nearest_beat(beats_ms: Sequence[float], t: float) -> int:
    i = bisect.bisect_left(beats_ms, t)
    if i == 0:
        return 0
    if i == len(beats_ms):
        return len(beats_ms) - 1
    return i if beats_ms[i] - t < t - beats_ms[i - 1] else i - 1


def snap_region(beats_ms: Sequence[float], in_ms: float, out_ms: float, beats: int | None = None) -> Region | None:
    """``in`` to the nearest grid beat, ``out`` to the beat ``beats`` later (nearest to out when not given)."""
    i = _nearest_beat(beats_ms, in_ms)
    n = beats if beats is not None else _nearest_beat(beats_ms, out_ms) - i
    if n < 1 or i + n >= len(beats_ms):
        return None
    a, b = beats_ms[i], beats_ms[i + n]
    return Region(in_ms=round(a), out_ms=round(b), beats=n, native_bpm=round(60_000.0 * n / (b - a), 3))


# ---------- peaks --------------------------------------------------------------------


def region_peaks(analysis: TrackAnalysis, in_ms: float, out_ms: float) -> dict[str, list[float]]:
    """PEAK_POINTS max-peaks per band over exactly [in_ms, out_ms]."""
    out: dict[str, list[float]] = {}
    for band, values in analysis.bands.items():
        n = len(values)
        if n == 0:
            raise ValueError(f"detail waveform band {band} is empty")
        scale = n / analysis.duration_ms
        span = (out_ms - in_ms) / PEAK_POINTS
        pts: list[float] = []
        for k in range(PEAK_POINTS):
            lo = min(n - 1, int((in_ms + k * span) * scale))
            hi = max(lo + 1, min(n, int((in_ms + (k + 1) * span) * scale + 0.999999)))
            v = max(values[lo:hi])
            if not 0.0 <= v <= 1.0:
                raise ValueError(f"detail waveform {band} value {v} is outside 0..1")
            pts.append(round(float(v), 4))
        out[band] = pts
    return out


# ---------- slot ids -----------------------------------------------------------------


def slot_id_of(stable_id: str, source: str, region: Region) -> str:
    """The stable slot id of a region: ``<stable_id>:<source>:<in_ms>-<out_ms>``."""
    return f"{stable_id}:{source}:{region.in_ms}-{region.out_ms}"


def parse_slot_id(slot_id: str) -> SlotId | None:
    """The parts of a slot id, or None when it is not one; stable ids may themselves hold ':'."""
    parts = slot_id.rsplit(":", 2)
    if len(parts) != 3:
        return None
    a, _, b = parts[2].partition("-")
    return SlotId(parts[0], parts[1], int(a), int(b)) if a.isdigit() and b.isdigit() else None


# ---------- key and ranking ----------------------------------------------------------


def _camelot(key: str | None) -> CamelotKey | None:
    if not key:
        return None
    try:
        return key_to_camelot(key)
    except ValueError:
        return None  # an unreadable key is an unknown key: it ranks last (UNKNOWN_KEY_DISTANCE)


def camelot_distance(a: str | None, b: str | None) -> int:
    """Same key 0, +-1 same letter 1, relative 2, else 3 + wheel steps; unknown last."""
    ka, kb = _camelot(a), _camelot(b)
    if ka is None or kb is None:
        return UNKNOWN_KEY_DISTANCE
    steps = wheel_steps(ka, kb)
    if ka.letter == kb.letter and steps <= 1:
        return steps
    if steps == 0:
        return 2
    return 3 + steps


def _title_key(s: Mapping[str, Any]) -> tuple[Any, ...]:
    return (s["title"].lower(), s["artist"].lower(), s["stable_id"], s["cue_letter"] or "~", s["in_ms"])


def crate_positions(crates: Sequence[Sequence[str]]) -> dict[str, int]:
    """Each track's position of the FIRST crate holding it, ``crates`` given in library order."""
    out: dict[str, int] = {}
    for position, members in enumerate(crates):
        for sid in members:
            out.setdefault(sid, position)
    return out


def rank(
    slots: Sequence[Mapping[str, Any]],
    sort: SlotSort,
    master_bpm: float | None,
    master_key: str | None,
    recency: Mapping[str, str],
    crate_of: Mapping[str, int],
) -> list[Mapping[str, Any]]:
    """Slots in ``sort`` order; every order ends on the title key, so pages are stable.

    ``crate_of`` is ``crate_positions`` of the library's crates (only ``crate`` reads it).
    ``color`` follows the rekordbox hot cue palette, which lists its colours in ascending
    ColorTableIndex (mimicked from rekordbox's colour picker and the PCO2 colour codes).
    """
    if sort == "compat" and master_bpm is not None:

        def compat(s: Mapping[str, Any]) -> tuple[Any, ...]:
            ratio = s["native_bpm"] / master_bpm
            outside = not (BPM_WINDOW[0] <= ratio <= BPM_WINDOW[1])
            return (outside, camelot_distance(s["key"], master_key), abs(s["native_bpm"] - master_bpm), *_title_key(s))

        return sorted(slots, key=compat)
    if sort == "recent":
        by_title = sorted(slots, key=_title_key)
        return sorted(by_title, key=lambda s: recency.get(s["slot_id"], ""), reverse=True)
    if sort == "crate":

        def crate(s: Mapping[str, Any]) -> tuple[Any, ...]:
            position = crate_of.get(s["stable_id"])
            return (position is None, position or 0, *_title_key(s))

        return sorted(slots, key=crate)
    if sort == "color":

        def color(s: Mapping[str, Any]) -> tuple[Any, ...]:
            index = s["color_index"]
            colourless = index is None or index == NO_CUE_COLOR
            return (colourless, 0 if colourless else index, *_title_key(s))

        return sorted(slots, key=color)
    if sort in ("track", "compat"):  # 'compat' with no playing master groups by track
        return sorted(slots, key=_title_key)
    raise ValueError(f"unknown sampler sort {sort!r}")


def matches_query(slot: Mapping[str, Any], q: str) -> bool:
    """True when ``q`` appears, case-folded, in the slot's name, title, artist or key."""
    needle = q.lower()
    return any(needle in (slot[f] or "").lower() for f in ("name", "title", "artist", "key"))
