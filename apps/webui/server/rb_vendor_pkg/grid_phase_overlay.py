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
import re
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from apps.analysis_beatgrid.mik_grid import GridChoice, choose_grid, read_mik_beatgrid, shifted_block
from apps.analysis_beatgrid.phase_refine import PhaseRefinement, click_envelope, decode_mono, refine_phase
from apps.shared import id3v2
from apps.shared.ffmpeg import FfmpegUnavailable, resolve_ffmpeg
from apps.shared.state import grid_edit, tempo_pick
from apps.shared.state.tempo_pick import TempoPick

from .own_beatgrid_overlay import _own_beatgrid_block, _own_beatgrid_lane_result, _set_dynamic_tempo_hint
from .own_lane_store import table_present

log = logging.getLogger(__name__)

CACHE_FILENAME = "grid-phase.json"
PLAYLIST_NAME_RE = re.compile(r"viva|oltf", re.IGNORECASE)
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


def matching_playlist_ids(conn: sqlite3.Connection) -> list[str]:
    """Live playlists whose name matches Viva or OLTF, in stable order."""
    rows = conn.execute(
        "SELECT playlist_id, name FROM playlists "
        "WHERE deleted_at IS NULL ORDER BY playlist_id"
    ).fetchall()
    return [row[0] for row in rows if PLAYLIST_NAME_RE.search(str(row[1] or ""))]


def member_ids(conn: sqlite3.Connection, playlist_ids: Iterable[str]) -> list[str]:
    """Deduped live members of matching playlists, in playlist/position order."""
    seen: dict[str, None] = {}
    for playlist_id in playlist_ids:
        if (
            conn.execute(
                "SELECT 1 FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL",
                (playlist_id,),
            ).fetchone()
            is None
        ):
            continue
        rows = conn.execute(
            "SELECT stable_id FROM playlist_memberships "
            "WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY position",
            (playlist_id,),
        ).fetchall()
        for (stable_id,) in rows:
            seen.setdefault(stable_id, None)
    return list(seen)


@dataclass(frozen=True)
class IdlePassResult:
    processed: tuple[str, ...]
    skipped: tuple[str, ...]
    yielded: bool
    remaining: tuple[str, ...]


def _default_own_payload(conn: sqlite3.Connection, stable_id: str) -> dict[str, Any] | None:
    result = _own_beatgrid_lane_result(conn, stable_id)
    if result is None or result.status != "ok":
        return None
    block, tempo_changes = _own_beatgrid_block(result, stable_id)
    return {"beatgrid": block, "tempo_changes": tempo_changes}


def idle_pass(
    *,
    conn_factory: Callable[[], sqlite3.Connection],
    state_db_path: Path,
    playing_fn: Callable[[], bool],
    payload_fn: Callable[[sqlite3.Connection, str], dict[str, Any] | None] | None = None,
    share: bool = False,
) -> IdlePassResult:
    """Warm ``grid-phase.json`` for live Viva/OLTF members.

    State reads finish before ``apply_grid_phase`` can decode audio. The gate is
    checked before every member so a deck load can stop a long idle pass.
    """
    conn = conn_factory()
    try:
        playlist_ids = matching_playlist_ids(conn)
        stable_ids = member_ids(conn, playlist_ids)
    finally:
        conn.close()

    processed: list[str] = []
    skipped: list[str] = []
    for index, stable_id in enumerate(stable_ids):
        if playing_fn():
            return IdlePassResult(tuple(processed), tuple(skipped), True, tuple(stable_ids[index:]))

        conn = conn_factory()
        try:
            payload = (payload_fn or _default_own_payload)(conn, stable_id)
            if payload is None:
                skipped.append(stable_id)
                continue
            source = (payload.get("beatgrid") or {}).get("source")
            stored = grid_edit.NO_EDITS
            if table_present(conn, "track_fields"):
                stored = grid_edit.read(conn, stable_id)
            user_edit_stored = (
                not stored.for_source(source).is_identity()
                if source in ("own", "rekordbox")
                else False
            )
            pick = tempo_pick.read(conn, stable_id)
        finally:
            conn.close()

        try:
            apply_grid_phase(
                payload,
                stable_id,
                share=share,
                user_edit_stored=user_edit_stored,
                state_db_path=state_db_path,
                tempo_pick=pick,
            )
        except (FfmpegUnavailable, RuntimeError, OSError) as exc:
            log.warning("grid phase idle precompute skipped %s: %s", stable_id, exc)
            skipped.append(stable_id)
        else:
            processed.append(stable_id)
    return IdlePassResult(tuple(processed), tuple(skipped), False, ())


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


__all__ = [
    "CACHE_FILENAME",
    "PLAYLIST_NAME_RE",
    "IdlePassResult",
    "apply_grid_phase",
    "idle_pass",
    "kick_refinement",
    "matching_playlist_ids",
    "member_ids",
]
