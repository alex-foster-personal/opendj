"""Stage 1 ranker: pure track_compatibility (AI-01)."""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.shared.harmonic import (
    TrackFeature,
    bpm_compatibility,
    camelot_compatibility,
    energy_compatibility,
    track_compatibility,
)

from .session_context import PlayedTrack


@dataclass(slots=True)
class ScoredCandidate:
    stable_id: str
    score: float
    rationale: dict[str, float] = field(default_factory=dict)
    candidate: TrackFeature | None = None


def rank_stage1(
    *,
    current_track: PlayedTrack | TrackFeature,
    candidates: list[TrackFeature],
    top_n: int = 40,
) -> list[ScoredCandidate]:
    """Rank ``candidates`` by combined harmonic score; DESC score."""
    cur_key = getattr(current_track, "key_camelot", None)
    cur_bpm = getattr(current_track, "bpm", 0.0) or 0.0
    cur_energy = getattr(current_track, "energy", 0) or 0
    scored: list[ScoredCandidate] = []
    for cand in candidates:
        score = track_compatibility(
            current_key=cur_key,
            current_bpm=cur_bpm,
            current_rating=cur_energy,
            candidate_key=cand.key_camelot,
            candidate_bpm=cand.bpm or 0.0,
            candidate_rating=cand.energy or 0,
        )
        key_sub = (
            camelot_compatibility(cur_key, cand.key_camelot)
            if cur_key and cand.key_camelot
            else 0.5
        )
        bpm_sub = bpm_compatibility(cur_bpm, cand.bpm)
        eng_sub = energy_compatibility(cur_energy, cand.energy or 0)
        rationale = {"camelot": key_sub, "bpm": bpm_sub, "energy": eng_sub}
        scored.append(
            ScoredCandidate(
                stable_id=cand.stable_id,
                score=round(score, 6),
                rationale=rationale,
                candidate=cand,
            )
        )
    # Deterministic: score DESC, stable_id ASC.
    scored.sort(key=lambda s: (-s.score, s.stable_id))
    return scored[:top_n]
