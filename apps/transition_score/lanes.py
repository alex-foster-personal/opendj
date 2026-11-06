"""Two-deck lanes for one try-er render: each deck's waveform and its knob curves on the render clock (TRYER-16).

Input: a candidate's kept render folder (``decks/deck<N>.wav``, written by ``odj-audio render --decks-out``), the
compiled render document (``compile_render_plan``) and the outgoing profile that clocks it. Output: per deck, peak
columns of what that deck actually played and the fader, low-EQ and filter curves the renderer applied, as
``(seconds, value)`` breakpoints from render time zero; the lines a DJ app draws over two stacked waveforms.

Times come from the document the renderer ran, never re-derived from the plan: an event at ``{"deck", "bar"}`` sounds
at the outgoing deck's bar on its own grid (``TrackProfile.time_of_bar``), minus the render's start. A knob the
document never touches keeps the deck's power-on value (``deck.rs`` ``Deck::new``).

Requirements:
  ✔︎ peaks are per-column maxima of |sample| over one deck's render, 0..1 of full scale, never renormalized
  ✔︎ set and ramp events become breakpoints at their render times; a knob untouched stays at its power-on value
  ✔︎ an in-flight ramp interrupted by a later set or ramp on the same knob is cut at that bar and the new move
      starts from the value then
  ✔︎ an event the curves cannot place (an unknown at, a ramp on another clock) raises LaneError, never skipped
Acceptance:
  [if] a deck render is missing [then ⛔️] LaneError naming the missing deckN.wav
  [if] a ramp is clocked on a deck other than the outgoing one [then ⛔️] LaneError
  [if] the document sets the incoming fader to 0 at bar 0 and ramps it to 1 over 8 bars [then] its curve has
      (t0, 0.0) and (t0 + 8 bars, 1.0)
  [if] an 8-bar fader ramp from 1.0 to 0.0 is interrupted at bar 4 by a set to 0.8 [then] its curve times are
      non-decreasing and the value at bar 4 is 0.5 before the jump to 0.8
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from apps.autodj.contract import RenderLayout, TrackProfile

#: Knobs drawn as lines, by the name the panel shows.
DRAWN_KNOBS: tuple[str, ...] = ("fader", "eq_low", "filter")
#: A deck's power-on knob values (deck.rs Deck::new): unity fader, centered EQ and filter.
POWER_ON: dict[str, float] = {"fader": 1.0, "eq_low": 0.5, "filter": 0.5}
MAX_COLUMNS: int = 4000


class LaneError(ValueError):
    """The render or its document cannot be drawn as lanes."""


@dataclass(frozen=True)
class DeckLane:
    deck: int
    peaks: list[float]
    curves: dict[str, list[tuple[float, float]]]
    #: (start_s, end_s) spans the deck plays, from the document's play commands; the lines mean nothing outside them.
    playing: list[tuple[float, float]]
    #: (render_s, n) for each beat the deck plays in its first span, n = 1 on a downbeat (the loaded beatgrid's own
    #: numbering), from the document's load, seek and tempo commands: the grid the renderer played, not re-estimated.
    beats: list[tuple[float, int]]


@dataclass(frozen=True)
class Lanes:
    duration_s: float
    decks: list[DeckLane]


# -----------------------------------------------------------------------------
# waveform
# -----------------------------------------------------------------------------
def _read_pcm16(path: Path) -> tuple[np.ndarray, int]:
    """Mono float samples in -1..1 and the sample rate; 16-bit PCM only (what the renderer writes)."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise LaneError(f"{path.name}: {8 * handle.getsampwidth()}-bit audio; the renderer writes 16-bit")
        channels, rate = handle.getnchannels(), handle.getframerate()
        raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    frames = raw.reshape(-1, channels).astype(np.float32) / 32768.0
    return np.abs(frames).max(axis=1), rate


def deck_peaks(path: Path, columns: int) -> tuple[list[float], float]:
    """Per-column peak of |sample| (0..1 of full scale) and the render's length in seconds."""
    if not 1 <= columns <= MAX_COLUMNS:
        raise LaneError(f"columns must be 1..{MAX_COLUMNS}, got {columns}")
    magnitude, rate = _read_pcm16(path)
    if magnitude.size == 0:
        raise LaneError(f"{path.name}: empty render")
    edges = np.linspace(0, magnitude.size, columns + 1).astype(int)
    peaks = [float(magnitude[a:b].max()) if b > a else 0.0 for a, b in pairwise(edges)]
    return [round(p, 4) for p in peaks], magnitude.size / rate


# -----------------------------------------------------------------------------
# transport and knob curves
# -----------------------------------------------------------------------------
def _knob_of(body: dict[str, Any]) -> str | None:
    """The drawn knob a cmd or ramp body moves, or None for one not drawn (play, tempo, trim, mid and high EQ)."""
    kind = body.get("type")
    if kind == "eq":
        return "eq_low" if body.get("band") == "low" else None
    if kind in ("fader", "filter"):
        return kind
    return None


def _event_bar(at: dict[str, Any], out_deck: int, start_out_bar: float) -> float:
    """The outgoing bar an event sounds at: render time zero is the render's first bar."""
    if at == {"ms": 0}:
        return start_out_bar
    if at.get("deck") == out_deck and "bar" in at:
        return float(at["bar"])
    raise LaneError(f"cannot place an event at {at!r} on the outgoing deck {out_deck}'s clock")


def playing_spans(
    document: dict[str, Any], out_deck: int, out_profile: TrackProfile, layout: RenderLayout, duration_s: float
) -> dict[int, list[tuple[float, float]]]:
    """Each deck's (start_s, end_s) play spans; a deck still playing at the end runs to ``duration_s``."""
    spans: dict[int, list[tuple[float, float]]] = {}
    since: dict[int, float] = {}
    for event in document["events"]:
        body = event.get("cmd")
        if body is None or body.get("type") != "play":
            continue
        deck = int(body["deck"])
        at = round(
            (out_profile.time_of_bar(_event_bar(event["at"], out_deck, layout.start_out_bar)) - layout.start_out_s)
            / layout.out_ratio,
            4,
        )
        if body["playing"] and deck not in since:
            since[deck] = at
        elif not body["playing"] and deck in since:
            spans.setdefault(deck, []).append((since.pop(deck), at))
    for deck, start in since.items():
        spans.setdefault(deck, []).append((start, round(duration_s, 4)))
    return spans


def _refuse_late_tempo(document: dict[str, Any]) -> None:
    """A tempo change after time zero would move every later beat, so it is refused rather than drawn wrong."""
    for event in document["events"]:
        body = event.get("cmd") or event.get("ramp") or {}
        if body.get("type") == "tempo" and ("ramp" in event or event["at"] != {"ms": 0}):
            raise LaneError(
                f"deck {body.get('deck')} changes tempo after the render starts: its beat grid is not drawn yet"
            )


def _time_zero_state(
    document: dict[str, Any],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, float], dict[int, float]]:
    """Each deck's loaded beatgrid, seek point and tempo ratio at render time zero; a later tempo change is refused."""
    _refuse_late_tempo(document)
    at_zero = [e["cmd"] for e in document["events"] if "cmd" in e and e["at"] == {"ms": 0}]

    def by_deck(kind: str, field: str) -> dict[int, Any]:
        return {int(cmd["deck"]): cmd[field] for cmd in at_zero if cmd.get("type") == kind}

    grids = by_deck("load", "beatgrid")
    seek_ms = {deck: float(ms) for deck, ms in by_deck("seek", "position_ms").items()}
    ratio = {deck: float(r) for deck, r in by_deck("tempo", "ratio").items()}
    return grids, seek_ms, ratio


def beat_grid(
    document: dict[str, Any], spans: dict[int, list[tuple[float, float]]]
) -> dict[int, list[tuple[float, int]]]:
    """Each deck's beats on the render clock over its FIRST play span: loaded beatgrid, seek point and tempo ratio.

    A tempo change after time zero would move every later beat, so it is refused rather than drawn wrong."""
    grids, seek_ms, ratio = _time_zero_state(document)
    beats: dict[int, list[tuple[float, int]]] = {}
    for deck, deck_spans in spans.items():
        if deck not in grids or deck not in seek_ms:
            raise LaneError(f"deck {deck} plays without a loaded beatgrid and seek at time zero")
        start, end = deck_spans[0]
        speed = ratio.get(deck, 1.0)  # deck.rs Deck::new: tempo 1.0 until a tempo command
        beats[deck] = [
            (round(at, 4), int(beat["n"]))
            for beat in grids[deck]
            if beat["time_ms"] >= seek_ms[deck]
            and (at := start + (float(beat["time_ms"]) - seek_ms[deck]) / 1000.0 / speed) <= end
        ]
    return beats


class _InFlightRamp(NamedTuple):
    start_bar: float
    start_val: float
    end_bar: float
    target: float


def knob_curves(
    document: dict[str, Any], out_deck: int, out_profile: TrackProfile, layout: RenderLayout
) -> dict[int, dict[str, list[tuple[float, float]]]]:
    """Breakpoints per deck and drawn knob from a compiled render document, in event order."""

    def seconds(bar: float) -> float:
        return round((out_profile.time_of_bar(bar) - layout.start_out_s) / layout.out_ratio, 4)

    current: dict[tuple[int, str], float] = {}
    in_flight: dict[tuple[int, str], _InFlightRamp] = {}
    curves: dict[int, dict[str, list[tuple[float, float]]]] = {}
    for event in document["events"]:
        body = event.get("cmd") or event.get("ramp")
        if body is None:
            raise LaneError(f"render event with neither cmd nor ramp: {event!r}")
        knob = _knob_of(body)
        if knob is None:
            continue
        deck = int(body["deck"])
        key = (deck, knob)
        bar = _event_bar(event["at"], out_deck, layout.start_out_bar)
        before = current.get(key, POWER_ON[knob])
        points = curves.setdefault(deck, {}).setdefault(knob, [(0.0, POWER_ON[knob])])
        if "cmd" in event:
            target, end_bar = float(body["value"]), bar
        else:
            over = body["over"]
            if over.get("deck") != out_deck or "bars" not in over:
                raise LaneError(f"ramp clocked on {over!r}, not the outgoing deck's bars")
            target, end_bar = float(body["to"]), bar + float(over["bars"])
        ramp = in_flight.get(key)
        if ramp is not None and bar < ramp.end_bar:
            span = ramp.end_bar - ramp.start_bar
            before = (
                ramp.start_val
                if span <= 0.0
                else ramp.start_val
                + (ramp.target - ramp.start_val) * (bar - ramp.start_bar) / span
            )
            before = round(before, 10)
            points.pop()
        elif ramp is not None and bar >= ramp.end_bar:
            in_flight.pop(key, None)
        points.extend([(seconds(bar), before), (seconds(end_bar), target)])
        current[key] = target
        if end_bar > bar:
            in_flight[key] = _InFlightRamp(bar, before, end_bar, target)
        else:
            in_flight.pop(key, None)
    return curves


def _loaded_decks(document: dict[str, Any]) -> list[int]:
    """The decks the document loads: the ones --decks-out writes as decks/deck<N>.wav."""
    return sorted(
        {
            int(event["cmd"]["deck"])
            for event in document["events"]
            if (cmd := event.get("cmd")) is not None and cmd.get("type") == "load"
        }
    )


def build_lanes(
    render_dir: Path,
    document: dict[str, Any],
    out_deck: int,
    out_profile: TrackProfile,
    layout: RenderLayout,
    columns: int,
) -> Lanes:
    """Both decks' peaks and curves for one kept render folder."""
    curves = knob_curves(document, out_deck, out_profile, layout)
    decks_dir = render_dir / "decks"
    decks = _loaded_decks(document)
    if not decks:
        raise LaneError(f"the render document loads no decks, so {render_dir.name}/decks holds nothing to draw")
    missing = [f"deck{deck}.wav" for deck in decks if not (decks_dir / f"deck{deck}.wav").is_file()]
    if missing:
        raise LaneError(f"deck renders missing under {render_dir.name}/decks: {', '.join(missing)}")
    lanes, duration = [], 0.0
    for deck in decks:
        peaks, duration = deck_peaks(render_dir / "decks" / f"deck{deck}.wav", columns)
        drawn = {knob: curves.get(deck, {}).get(knob, [(0.0, POWER_ON[knob])]) for knob in DRAWN_KNOBS}
        lanes.append(DeckLane(deck=deck, peaks=peaks, curves=drawn, playing=[], beats=[]))
    spans = playing_spans(document, out_deck, out_profile, layout, duration)
    grids = beat_grid(document, spans)
    lanes = [
        DeckLane(lane.deck, lane.peaks, lane.curves, spans.get(lane.deck, []), grids.get(lane.deck, []))
        for lane in lanes
    ]
    return Lanes(duration_s=round(duration, 4), decks=lanes)
