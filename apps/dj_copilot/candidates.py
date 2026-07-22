"""Hard-ish candidate filtering for AI-01 (Phase 13 Plan 03)."""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.shared.harmonic import TrackFeature

from .session_context import PlayedTrack, SessionContext


@dataclass(slots=True)
class FilterTrace:
    dropped_bpm: int = 0
    dropped_artist: int = 0
    dropped_energy: int = 0
    dropped_current: int = 0
    dropped_notes: dict[str, list[str]] = field(default_factory=dict)

    def total_dropped(self) -> int:
        return (
            self.dropped_bpm
            + self.dropped_artist
            + self.dropped_energy
            + self.dropped_current
        )


def _recent_energy_slope(context: SessionContext) -> float:
    """Signed slope estimate over the context window. Zero when empty."""
    vals = [t.energy for t in context.recent if t.energy is not None]
    if len(vals) < 2:
        return 0.0
    return float(vals[-1]) - float(vals[0])


def filter_candidates(
    *,
    current_track: PlayedTrack | TrackFeature,
    library: list[TrackFeature],
    context: SessionContext,
    cooldown_tracks: int = 8,
    bpm_window_pct: float = 6.0,
    energy_reverse_cap: int = 2,
) -> tuple[list[TrackFeature], FilterTrace]:
    """Drop candidates outside BPM window / artist cooldown / energy slope."""
    trace = FilterTrace()
    bpm_cur = getattr(current_track, "bpm", None)
    current_stable_id = current_track.stable_id
    recent_artists_window: list[str | None] = [
        t.artist for t in context.recent[-cooldown_tracks:]
    ]
    slope = _recent_energy_slope(context)

    out: list[TrackFeature] = []
    for cand in library:
        if cand.stable_id == current_stable_id:
            trace.dropped_current += 1
            continue
        # BPM window. bpm <= 0 means "unknown" in real libraries (matches
        # apps.shared.harmonic.bpm_compatibility); skip the window check
        # instead of dividing by zero - ranking scores those 0.0 anyway.
        if bpm_cur is not None and cand.bpm is not None and bpm_cur > 0 and cand.bpm > 0:
            delta_pct = (max(bpm_cur, cand.bpm) / min(bpm_cur, cand.bpm) - 1.0) * 100.0
            if delta_pct > bpm_window_pct:
                trace.dropped_bpm += 1
                continue
        # Artist cooldown
        if cand.artist is not None and cand.artist in recent_artists_window:
            trace.dropped_artist += 1
            continue
        # Energy slope cap -- reject candidates that would reverse the
        # recent slope by more than ``energy_reverse_cap`` steps.
        cur_energy = getattr(current_track, "energy", None)
        if (
            cand.energy is not None
            and cur_energy is not None
            and abs(slope) > 0.5
        ):
            # Direction the slope is going.
            if slope > 0 and float(cand.energy) < float(cur_energy) - energy_reverse_cap:
                trace.dropped_energy += 1
                continue
            if slope < 0 and float(cand.energy) > float(cur_energy) + energy_reverse_cap:
                trace.dropped_energy += 1
                continue
        out.append(cand)
    return out, trace
