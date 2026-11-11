"""Stage 1 ranker: pure track_compatibility (AI-01)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from apps.dj_copilot.history_term import days_since_played, history_term
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


def scored_candidate_order_key(sc: ScoredCandidate) -> tuple[float, str]:
    """Deterministic rank: score DESC, stable_id ASC."""
    return (-sc.score, sc.stable_id)


def rank_stage1(
    *,
    current_track: PlayedTrack | TrackFeature,
    candidates: list[TrackFeature],
    top_n: int = 40,
    history_weight: float = 0.0,
    now: datetime | None = None,
) -> list[ScoredCandidate]:
    """Rank ``candidates`` by combined harmonic score; DESC score."""
    if history_weight > 0 and now is None:
        raise ValueError("now is required when history_weight > 0")
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
        key_sub = camelot_compatibility(cur_key, cand.key_camelot) if cur_key and cand.key_camelot else 0.5
        bpm_sub = bpm_compatibility(cur_bpm, cand.bpm)
        eng_sub = energy_compatibility(cur_energy, cand.energy or 0)
        rationale = {"camelot": key_sub, "bpm": bpm_sub, "energy": eng_sub}
        if history_weight:
            assert now is not None
            term = history_term(cand, now)
            if term is not None:
                score -= history_weight * (1.0 - term)
                rationale["history"] = term
                dsp = days_since_played(cand, now)
                if dsp is not None and not cand.never_played:
                    rationale["days_since_played"] = round(dsp, 2)
        scored.append(
            ScoredCandidate(
                stable_id=cand.stable_id,
                score=round(score, 6),
                rationale=rationale,
                candidate=cand,
            )
        )
    scored.sort(key=scored_candidate_order_key)
    return scored[:top_n]
