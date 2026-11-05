"""Rough played length of a track selection: how long it lasts when mixed (LIBUX played length).

One estimate shared by ``POST /api/v1/tracks/played-length``, ``opendj track
played-length`` and the MCP tool ``tracks_played_length``. The library footer
runs a TypeScript mirror (``apps/webui/frontend/src/lib/rb/played-length.ts``);
``tests/fixtures/played_length_cases.json`` pins both to the same answers.

Per track, in order:

* mix points (``mix_in_s`` and ``mix_out_s``, out after in): ``mix_out - mix_in``.
* a BPM: the duration minus a 32-bar overlap at that tempo, never under half
  the duration. 32 bars is the common blend length in DJ practice.
* otherwise: 75% of the duration.

A track with no duration counts as ``unknown`` and adds nothing. Pure
arithmetic, no I/O: the caller supplies whatever the listing already carries
and nothing here triggers analysis.

-Claude
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Literal

OVERLAP_BARS = 32
BEATS_PER_BAR = 4
MIN_PLAYED_FRACTION = 0.5
NO_BPM_FRACTION = 0.75

Source = Literal["mix_points", "rule", "unknown"]


@dataclass(frozen=True)
class PlayedLengthInput:
    duration_s: float | None
    bpm: float | None
    mix_in_s: float | None = None
    mix_out_s: float | None = None


@dataclass(frozen=True)
class PlayedLength:
    played_s: float
    total_s: float
    from_mix_points: int
    from_rule: int
    unknown: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def estimate_track(track: PlayedLengthInput) -> tuple[float, Source]:
    """Seconds this track is heard in a mix, and which rule said so."""
    duration = track.duration_s
    if duration is None or duration <= 0:
        return 0.0, "unknown"
    if track.mix_in_s is not None and track.mix_out_s is not None and track.mix_out_s > track.mix_in_s:
        return track.mix_out_s - track.mix_in_s, "mix_points"
    if track.bpm is not None and track.bpm > 0:
        overlap_s = OVERLAP_BARS * BEATS_PER_BAR * 60 / track.bpm
        return max(duration - overlap_s, MIN_PLAYED_FRACTION * duration), "rule"
    return NO_BPM_FRACTION * duration, "rule"


def estimate_played_length(tracks: Iterable[PlayedLengthInput]) -> PlayedLength:
    """Sum of ``estimate_track`` over the selection, with how each part was reached."""
    played = total = 0.0
    counts: dict[Source, int] = {"mix_points": 0, "rule": 0, "unknown": 0}
    for track in tracks:
        seconds, source = estimate_track(track)
        played += seconds
        total += track.duration_s if source != "unknown" and track.duration_s is not None else 0.0
        counts[source] += 1
    return PlayedLength(
        played_s=played,
        total_s=total,
        from_mix_points=counts["mix_points"],
        from_rule=counts["rule"],
        unknown=counts["unknown"],
    )


__all__ = ["PlayedLength", "PlayedLengthInput", "estimate_played_length", "estimate_track"]
