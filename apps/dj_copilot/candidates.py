"""Hard-ish candidate filtering for AI-01 (Phase 13 Plan 03)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.shared.harmonic import TrackFeature

from .session_context import PlayedTrack, SessionContext

# PLAY-16 parity: apps/webui/frontend/src/lib/rb/auto-play-chain.ts autoPlaySongIdentity
_TRACK_NUMBER_PREFIX = re.compile(r"^(?:\d{1,2}[AB]?\s*-\s*){1,2}", re.IGNORECASE)
_LEAD_ARTIST_SPLIT = re.compile(r"\s*[,;/&]\s*|\s+(?:feat\.?|ft\.?|x)\s+", re.IGNORECASE)


def recording_identity(title: str | None, artist: str | None) -> str | None:
    """Lower-cased lead artist plus normalized title; None when untitled."""
    if not isinstance(title, str):
        return None
    name = re.sub(r"\s+", " ", _TRACK_NUMBER_PREFIX.sub("", title).strip()).lower()
    if not name:
        return None
    lead = _LEAD_ARTIST_SPLIT.split(artist or "")[0].strip().lower()
    return f"{lead}|{name}"


@dataclass(slots=True)
class FilterTrace:
    dropped_bpm: int = 0
    dropped_artist: int = 0
    dropped_energy: int = 0
    dropped_current: int = 0
    dropped_played: int = 0
    dropped_notes: dict[str, list[str]] = field(default_factory=dict)

    def total_dropped(self) -> int:
        return (
            self.dropped_bpm
            + self.dropped_artist
            + self.dropped_energy
            + self.dropped_current
            + self.dropped_played
        )


def _recent_energy_slope(context: SessionContext) -> float:
    """Signed slope estimate over the context window. Zero when empty."""
    vals = [t.energy for t in context.recent if t.energy is not None]
    if len(vals) < 2:
        return 0.0
    return float(vals[-1]) - float(vals[0])


def _played_session_lookups(
    context: SessionContext,
) -> tuple[set[str], dict[str, str]]:
    played_ids = {t.stable_id for t in context.recent}
    played_identities: dict[str, str] = {}
    for t in context.recent:
        ident = recording_identity(t.title, t.artist)
        if ident is not None:
            played_identities[ident] = t.stable_id
    return played_ids, played_identities


def _drop_session_played(
    cand: TrackFeature,
    trace: FilterTrace,
    played_ids: set[str],
    played_identities: dict[str, str],
) -> bool:
    if cand.stable_id in played_ids:
        trace.dropped_played += 1
        trace.dropped_notes.setdefault(cand.stable_id, []).append(f"played: {cand.stable_id}")
        return True
    ident = recording_identity(cand.title, cand.artist)
    match = played_identities.get(ident) if ident is not None else None
    if match is not None:
        trace.dropped_played += 1
        trace.dropped_notes.setdefault(cand.stable_id, []).append(f"same recording as played: {match}")
        return True
    return False


def _drop_bpm_window(
    cand: TrackFeature,
    bpm_cur: float | None,
    bpm_window_pct: float,
    trace: FilterTrace,
) -> bool:
    if bpm_cur is None or cand.bpm is None or bpm_cur <= 0 or cand.bpm <= 0:
        return False
    delta_pct = (max(bpm_cur, cand.bpm) / min(bpm_cur, cand.bpm) - 1.0) * 100.0
    if delta_pct > bpm_window_pct:
        trace.dropped_bpm += 1
        return True
    return False


def _drop_energy_reverse(
    cand: TrackFeature,
    current_track: PlayedTrack | TrackFeature,
    slope: float,
    energy_reverse_cap: int,
    trace: FilterTrace,
) -> bool:
    cur_energy = getattr(current_track, "energy", None)
    if cand.energy is None or cur_energy is None or abs(slope) <= 0.5:
        return False
    if slope > 0 and float(cand.energy) < float(cur_energy) - energy_reverse_cap:
        trace.dropped_energy += 1
        return True
    if slope < 0 and float(cand.energy) > float(cur_energy) + energy_reverse_cap:
        trace.dropped_energy += 1
        return True
    return False


def filter_candidates(
    *,
    current_track: PlayedTrack | TrackFeature,
    library: list[TrackFeature],
    context: SessionContext,
    cooldown_tracks: int = 8,
    bpm_window_pct: float = 6.0,
    energy_reverse_cap: int = 2,
) -> tuple[list[TrackFeature], FilterTrace]:
    """Drop candidates outside BPM window / artist cooldown / energy slope / session plays."""
    trace = FilterTrace()
    bpm_cur = getattr(current_track, "bpm", None)
    current_stable_id = current_track.stable_id
    recent_artists_window: list[str | None] = [t.artist for t in context.recent[-cooldown_tracks:]]
    slope = _recent_energy_slope(context)
    played_ids, played_identities = _played_session_lookups(context)

    out: list[TrackFeature] = []
    for cand in library:
        if cand.stable_id == current_stable_id:
            trace.dropped_current += 1
            continue
        if _drop_session_played(cand, trace, played_ids, played_identities):
            continue
        if _drop_bpm_window(cand, bpm_cur, bpm_window_pct, trace):
            continue
        if cand.artist is not None and cand.artist in recent_artists_window:
            trace.dropped_artist += 1
            continue
        if _drop_energy_reverse(cand, current_track, slope, energy_reverse_cap, trace):
            continue
        out.append(cand)
    return out, trace


def similar_library_candidates(
    *,
    current: TrackFeature,
    library: list[TrackFeature],
) -> list[TrackFeature]:
    """Candidates for Specific find-similar: full library minus the reference track."""
    return [t for t in library if t.stable_id != current.stable_id]
