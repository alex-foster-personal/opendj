"""LIGHT-02: pure beat clock that predicts each deck's next beats and downbeats.

There is no beat-tick event in the engine: it publishes 30 Hz position samples and beatgrids
arrive as ``[{n, time_ms, bpm}]``. ``predict_beats`` turns one ``DeckSample`` plus that grid into
the beats due inside a host-time lookahead window, each stamped with the host time it will be
heard. Standard library only; no I/O.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

BEATS_PER_BAR = 4
NS_PER_MS = 1_000_000

GridBeat = Mapping[str, float | int]


@dataclass(frozen=True)
class DeckSample:
    """One engine position sample for a deck."""

    deck: str
    position_ms: float
    rate: float
    playing: bool
    host_time_ns: int


@dataclass(frozen=True)
class BeatEvent:
    """A beat the deck will play, and the host time it will be heard."""

    deck: str
    beat_index: int
    bar_beat: int
    is_downbeat: bool
    bpm: float
    at_host_ns: int
    extrapolated: bool = False


# -----------------------------------------------------------------------------
# helpers


def _require_finite_number(value: float | int, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite, got {value!r}")
    return number


def _validate_sample(sample: DeckSample) -> None:
    _require_finite_number(sample.position_ms, "sample position_ms")
    _require_finite_number(sample.rate, "sample rate")
    _require_finite_number(sample.host_time_ns, "sample host_time_ns")


def _validate_grid(grid: Sequence[GridBeat]) -> None:
    last_time = float("-inf")
    for index, beat in enumerate(grid):
        time_ms = _require_finite_number(beat["time_ms"], f"grid beat {index} time_ms")
        bpm = _require_finite_number(beat["bpm"], f"grid beat {index} bpm")
        if bpm <= 0:
            raise ValueError(f"grid beat {index} bpm must be positive, got {bpm}")
        if time_ms <= last_time:
            if time_ms == last_time:
                raise ValueError(f"duplicate grid beat time at {time_ms} ms")
            raise ValueError("grid beat times must strictly increase")
        last_time = time_ms


def _host_ns_for(sample: DeckSample, beat_ms: float) -> int:
    return sample.host_time_ns + round((beat_ms - sample.position_ms) / sample.rate * NS_PER_MS)


def _event(sample: DeckSample, index: int, beat_ms: float, bar_beat: int, bpm: float, extrapolated: bool) -> BeatEvent:
    return BeatEvent(
        deck=sample.deck,
        beat_index=index,
        bar_beat=bar_beat,
        is_downbeat=bar_beat == 1,
        bpm=bpm * sample.rate,
        at_host_ns=_host_ns_for(sample, beat_ms),
        extrapolated=extrapolated,
    )


# -----------------------------------------------------------------------------
# public


def predict_beats(grid: Sequence[GridBeat], sample: DeckSample, lookahead_ms: float) -> list[BeatEvent]:
    """Beats with track time in ``[position, position + lookahead * rate)``, in order.

    ``lookahead_ms`` is host (wall-clock) time, so a faster deck covers more track time. Beats past
    the last grid entry continue at the last BPM and carry ``extrapolated=True``. A stopped deck,
    a non-positive rate or an empty grid yields nothing.
    """
    if not grid:
        return []
    _validate_grid(grid)
    if not sample.playing:
        return []
    _validate_sample(sample)
    if sample.rate <= 0:
        return []
    lookahead = _require_finite_number(lookahead_ms, "lookahead_ms")
    if lookahead <= 0:
        return []
    start_ms = sample.position_ms
    end_ms = start_ms + lookahead * sample.rate
    times = [float(beat["time_ms"]) for beat in grid]
    events: list[BeatEvent] = []
    first = bisect_left(times, start_ms)
    for index in range(first, len(grid)):
        if times[index] >= end_ms:
            return events
        beat = grid[index]
        events.append(_event(sample, index, times[index], int(beat["n"]), float(beat["bpm"]), False))
    last = grid[-1]
    last_bpm = float(last["bpm"])
    if last_bpm <= 0:
        raise ValueError(f"cannot extrapolate past the last grid beat: bpm is {last_bpm}")
    step_ms = 60_000.0 / last_bpm
    last_index = len(grid) - 1
    offset = 1
    if start_ms > times[-1]:
        offset = max(1, int((start_ms - times[-1]) // step_ms))
    while True:
        beat_ms = times[-1] + offset * step_ms
        if beat_ms >= end_ms:
            return events
        if beat_ms >= start_ms:
            bar_beat = (int(last["n"]) - 1 + offset) % BEATS_PER_BAR + 1
            events.append(_event(sample, last_index + offset, beat_ms, bar_beat, last_bpm, True))
        offset += 1
