"""Stage 1 ranker: pure track_compatibility (AI-01)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from apps.dj_copilot.history_term import days_since_played, history_term
from apps.shared.harmonic import (
    TRACK_TERMS,
    TrackFeature,
    track_compatibility,
    track_compatibility_terms,
)

from .session_context import PlayedTrack

_RATIONALE_NAMES = {"key": "camelot", "bpm": "bpm", "energy": "energy"}


@dataclass(slots=True)
class ScoredCandidate:
    stable_id: str
    score: float
    rationale: dict[str, float] = field(default_factory=dict)
    candidate: TrackFeature | None = None
    unscored: tuple[str, ...] = ()


def scored_candidate_order_key(sc: ScoredCandidate) -> tuple[int, float, str]:
    """Deterministic rank: more measured terms first (AGENT-33), then score DESC, then stable_id ASC."""
    return (len(sc.unscored), -sc.score, sc.stable_id)


def rank_stage1(
    *,
    current_track: PlayedTrack | TrackFeature,
    candidates: list[TrackFeature],
    top_n: int = 40,
    history_weight: float = 0.0,
    now: datetime | None = None,
) -> list[ScoredCandidate]:
    """Rank ``candidates`` by combined harmonic score; DESC score.

    Specific find-similar-tracks uses this ranker directly (no genre cosine).
    """
    if history_weight > 0 and now is None:
        raise ValueError("now is required when history_weight > 0")
    cur_key = getattr(current_track, "key_camelot", None)
    cur_bpm = getattr(current_track, "bpm", None)
    cur_energy = getattr(current_track, "energy", None)
    scored: list[ScoredCandidate] = []
    for cand in candidates:
        terms = track_compatibility_terms(
            current_key=cur_key,
            current_bpm=cur_bpm,
            current_rating=cur_energy,
            candidate_key=cand.key_camelot,
            candidate_bpm=cand.bpm,
            candidate_rating=cand.energy,
        )
        score = track_compatibility(
            current_key=cur_key,
            current_bpm=cur_bpm,
            current_rating=cur_energy,
            candidate_key=cand.key_camelot,
            candidate_bpm=cand.bpm,
            candidate_rating=cand.energy,
        )
        rationale = {_RATIONALE_NAMES[name]: value for name, value in terms.items()}
        unscored = tuple(name for name in TRACK_TERMS if name not in terms)
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
                unscored=unscored,
            )
        )
    scored.sort(key=scored_candidate_order_key)
    return scored[:top_n]
