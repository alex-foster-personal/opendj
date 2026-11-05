"""Serve the grid on the kick: Mixed In Key fallback, then kick phase refinement (GRID-MIK-01, GRID-PHASE-01).

Two pure policies, applied to the `/anlz` beatgrid block in this order:

1. `apps.analysis_beatgrid.mik_grid.choose_grid`: MIK's BeatGrid (ID3 GEOB of
   the file `GET /audio` plays) stands in for a missing or low-confidence own
   grid, or lends its phase to a confident one.
2. `apps.analysis_beatgrid.phase_refine.refine_phase`: the served own or MIK
   lines slide up to 30 ms onto the kick when the kick is clear. Measured on
   the audio itself, so it covers every track with no MIK data, and it wins
   over MIK's phase (they agree within 3 ms where MIK is clean).

``beatgrid.source`` keeps naming the lane (grid edits key on it). The payload
says what was served: ``grid_source`` (own, rekordbox, mik, none),
``grid_phase`` (fitted, mik, kick), ``grid_phase_shift_ms`` and
``grid_source_reason``; the beatgrid block carries ``grid_source`` too.
Where MIK's tempo and ours disagree (GRID-OCTAVE-02) the payload also carries
``bpm_disputed`` (true until the user picks one) and ``bpm_alternatives``
(``[{source, bpm}]``, ours first); the stored pick (``grid_tempo_pick``) is
passed in as ``tempo_pick``.

A stored user grid edit wins: it was made against the grid as served, so
moving that grid under it would apply the correction twice. Rekordbox grids
are left alone.

The kick measurement decodes the file (about 0.3 s at the lowest CPU
priority), so its result is remembered in ``grid-phase.json`` beside state.db,
keyed by path, size, mtime and the grid's own beat times.

-Claude
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from apps.analysis_beatgrid.mik_grid import GridChoice, choose_grid, read_mik_beatgrid, shifted_block
from apps.analysis_beatgrid.phase_refine import PhaseRefinement, click_envelope, decode_mono, refine_phase
from apps.shared import id3v2
from apps.shared.ffmpeg import FfmpegUnavailable, resolve_ffmpeg
from apps.shared.state.tempo_pick import TempoPick

from .own_beatgrid_overlay import _set_dynamic_tempo_hint

log = logging.getLogger(__name__)

CACHE_FILENAME = "grid-phase.json"
_lock = threading.Lock()


# ----------------------------------------------------------------- cache


def _cache_key(path: Path, beats: list[dict[str, Any]]) -> str:
    st = path.stat()
    grid = hashlib.sha1(json.dumps([round(float(b["t"]), 4) for b in beats]).encode()).hexdigest()[:16]
    return f"{path}|{st.st_size}|{st.st_mtime_ns}|{grid}"


def _cache_read(cache: Path) -> dict[str, Any]:
    return json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}


def _cache_write(cache: Path, key: str, value: PhaseRefinement) -> None:
    with _lock:
        entries = _cache_read(cache)
        entries[key] = asdict(value)
        fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=".grid-phase-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)
        os.replace(tmp, cache)


def kick_refinement(path: Path, beats: list[dict[str, Any]], cache: Path) -> PhaseRefinement:
    """The kick shift for ``beats`` on ``path``: remembered, else measured and remembered."""
    key = _cache_key(path, beats)
    hit = _cache_read(cache).get(key)
    if hit is not None:
        return PhaseRefinement(**hit)
    env, rate = click_envelope(decode_mono(path, resolve_ffmpeg()))
    result = refine_phase(env, rate, [float(b["t"]) for b in beats])
    _cache_write(cache, key, result)
    return result


# ----------------------------------------------------------------- overlay


def _stamp(
    payload: dict[str, Any], source: str, phase: str, shift_ms: float, reason: str, choice: GridChoice | None = None
) -> dict[str, Any]:
    payload["beatgrid"] = {**(payload.get("beatgrid") or {}), "grid_source": source}
    payload.update({"grid_source": source, "grid_phase": phase, "grid_phase_shift_ms": shift_ms, "grid_source_reason": reason})
    payload["bpm_disputed"] = choice is not None and choice.disputed
    payload["bpm_alternatives"] = [] if choice is None else [{"source": s, "bpm": b} for s, b in choice.alternatives]
    return payload


def _playable_path(stable_id: str, share: bool) -> Path | None:
    from apps.adapters.rekordbox.paths import resolve_playable_audio

    try:
        return Path(resolve_playable_audio(stable_id, share=share).path)
    except HTTPException:
        return None


def _mik_choice(payload: dict[str, Any], path: Path, tempo_pick: TempoPick | None) -> GridChoice:
    block = payload.get("beatgrid") or {}
    try:
        mik = read_mik_beatgrid(path)
    except (id3v2.Id3Error, OSError) as exc:
        mik = None
        log.warning("grid phase: Mixed In Key tag of %s unreadable: %s", path, exc)
    return choose_grid(
        block,
        mik,
        own_confidence=block.get("bpm_confidence"),
        has_tempo_changes=bool(payload.get("tempo_changes")),
        tempo_pick=tempo_pick,
    )


def apply_grid_phase(
    payload: dict[str, Any],
    stable_id: str,
    *,
    share: bool,
    user_edit_stored: bool,
    state_db_path: Path,
    tempo_pick: TempoPick | None = None,
) -> dict[str, Any]:
    """Apply MIK fallback and kick refinement to ``payload`` (returned; nested dicts replaced, never mutated)."""
    block = payload.get("beatgrid") or {}
    served = str(block.get("source")) if block.get("beats") else "none"
    if user_edit_stored:
        return _stamp(payload, served, "fitted", 0.0, "a grid edit is stored for this track; served as edited")
    path = _playable_path(stable_id, share)
    if path is None:
        return _stamp(payload, served, "fitted", 0.0, "no playable file to check the grid against")
    choice = _mik_choice(payload, path, tempo_pick)
    payload["beatgrid"] = choice.block
    if choice.replaced_by_mik:
        payload["tempo_changes"] = []
        _set_dynamic_tempo_hint(payload, present=False)
    beats = list(choice.block.get("beats") or [])
    if choice.source not in ("own", "own_disputed", "mik") or not beats:
        return _stamp(payload, choice.source, choice.phase, choice.phase_shift_ms, choice.reason, choice)
    # The kick is measured against the grid as fitted (or as MIK drew it),
    # never against the MIK-phased copy, so the two corrections never stack.
    fitted = block if choice.phase == "mik" else choice.block
    try:
        kick = kick_refinement(path, list(fitted["beats"]), state_db_path.parent / CACHE_FILENAME)
    except (FfmpegUnavailable, RuntimeError, OSError) as exc:
        return _stamp(payload, choice.source, choice.phase, choice.phase_shift_ms, f"{choice.reason}; kick not measured: {exc}", choice)
    if not kick.accepted:
        return _stamp(payload, choice.source, choice.phase, choice.phase_shift_ms, f"{choice.reason}; {kick.reason}", choice)
    payload["beatgrid"] = shifted_block(fitted, kick.shift_ms)
    return _stamp(payload, choice.source, "kick", kick.shift_ms, f"{choice.reason}; {kick.reason}", choice)


__all__ = ["CACHE_FILENAME", "apply_grid_phase", "kick_refinement"]
