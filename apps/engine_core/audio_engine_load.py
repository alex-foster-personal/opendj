"""Turn a library track into an ``odj-audio`` ``load`` command.

Protocol v1 ``load`` takes a file path, because the engine does not own the
library (phase 20 decision D4). This module is the one place that resolves a
``stable_id`` for it, and it reuses what the page already uses rather than a
second copy:

- the file: ``rb_vendor.resolve_playable_audio``, the same pick
  ``GET /api/v1/tracks/{id}/audio`` streams;
- the beatgrid: the ``beatgrid`` block of ``GET /api/v1/tracks/{id}/anlz``,
  so the engine plays against the grid the waveform draws, and the
  rekordbox-or-own selection (PARITY-02, ``/api/v1/analysis/source``) carries
  over unchanged.

A track with no usable grid still loads (it plays; beat commands answer
``no_beatgrid``), and the response says why the grid is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EngineGrid:
    beats: list[dict[str, float | int]]
    bpm: float | None
    source: str | None
    #: Why ``beats`` is empty, when it is.
    missing_reason: str | None


def grid_from_anlz(beatgrid: dict[str, Any] | None) -> EngineGrid:
    """The anlz ``beatgrid`` block in the engine's ``[{n, time_ms, bpm}]`` shape.

    anlz beat times are SECONDS (``anlz-types.ts``); the engine wants ms.
    """
    if not beatgrid:
        return EngineGrid([], None, None, "the anlz payload has no beatgrid block")
    source = beatgrid.get("source")
    raw = beatgrid.get("beats") or []
    if not raw:
        reason = beatgrid.get("reason") or f"the {source} beatgrid has no beats"
        return EngineGrid([], None, source, str(reason))
    beats: list[dict[str, float | int]] = []
    last = float("-inf")
    for b in raw:
        t_ms = float(b["t"]) * 1000.0
        if t_ms <= last:
            # The engine refuses a grid whose times do not strictly increase;
            # say so here with the track's own numbers rather than passing
            # the engine's generic refusal back.
            raise ValueError(f"{source} beatgrid times do not increase at {t_ms:.3f} ms")
        last = t_ms
        beat: dict[str, float | int] = {"n": int(b["n"]), "time_ms": t_ms}
        # The analyzer's own tempo at this beat: a single beat gap jitters by
        # several percent, so the engine reports this one when it is there.
        beat_bpm = b.get("bpm")
        if beat_bpm is not None:
            beat["bpm"] = float(beat_bpm)
        beats.append(beat)
    bpm = beatgrid.get("bpm")
    if bpm is None:
        bpm = raw[0].get("bpm")
    return EngineGrid(beats, float(bpm) if bpm else None, source, None)


def load_command(deck: int, path: str, grid: EngineGrid) -> dict[str, Any]:
    cmd: dict[str, Any] = {"type": "load", "deck": deck, "path": path}
    if grid.beats:
        cmd["beatgrid"] = grid.beats
    if grid.bpm:
        cmd["bpm"] = grid.bpm
    return cmd


__all__ = ["EngineGrid", "grid_from_anlz", "load_command"]
