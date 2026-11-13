"""suggest_next -- AI-01 public API (Phase 13 Plan 03).

Composition:

    current -> filter_candidates -> Stage 1 rank -> Stage 2 rerank
            -> translate scores to rationale_tags -> top_n

The optional ``explain=True`` path is synchronous in Phase 13: it calls
the deterministic explainer inline. Missing key, BPM or energy terms are
named in ``unscored``, not neutralised. The documented event-bus contract
(``ai.explain_requested`` / ``ai.explain_response``) is shipped in
``(private path)`` for Phase 14 / Phase 17 consumers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from apps.genre_infer import findings
from apps.genre_infer import service as genre_service
from apps.shared.harmonic import TrackFeature, harmonic_missing_for_rank

from .candidates import filter_candidates, similar_library_candidates
from .explainer import explain as _explain
from .peak_pressure import PeakPlay, apply_pressure_prior, compute_peak_pressure
from .play_it import InsufficientDataError
from .rank_stage1 import ScoredCandidate, rank_stage1
from .rank_stage2 import rerank_with_pairings
from .session_context import PlayedTrack, SessionContext


@dataclass(slots=True)
class RankedSuggestion:
    stable_id: str
    score: float
    rationale_tags: list[str] = field(default_factory=list)
    rationale_numbers: dict[str, float] = field(default_factory=dict)
    explain_text: str | None = None
    candidate: TrackFeature | None = None
    unscored: tuple[str, ...] = ()


FindSimilarMode = Literal["generic", "specific", "niche"]

FIND_SIMILAR_PROVIDER_HARMONIC = "harmonic"


def find_similar_specific(
    *,
    current_stable_id: str,
    library: list[TrackFeature],
    limit: int,
) -> dict[str, Any]:
    """Specific find-similar: rank library neighbours by track_compatibility only."""
    current = next((t for t in library if t.stable_id == current_stable_id), None)
    if current is None:
        raise KeyError(f"track {current_stable_id} not in library")

    missing = harmonic_missing_for_rank(current)
    if missing["bpm"] or missing["key"]:
        raise InsufficientDataError(playlist_id=f"find-similar:{current_stable_id}", missing=missing)

    candidates = similar_library_candidates(current=current, library=library)
    ranked = rank_stage1(current_track=current, candidates=candidates, top_n=limit)
    return {
        "stable_id": current_stable_id,
        "mode": "specific",
        "provider": FIND_SIMILAR_PROVIDER_HARMONIC,
        "candidates": [
            {
                "stable_id": sc.stable_id,
                "score": sc.score,
                "rationale_numbers": dict(sc.rationale),
            }
            for sc in ranked
        ],
    }


def find_similar_tracks(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    mode: FindSimilarMode,
    limit: int = 20,
    library: list[TrackFeature] | None = None,
) -> dict[str, Any]:
    """find-similar-tracks for one track (parent #345).

    Generic ranks JEV genre-vector cosine neighbours. Specific ranks BPM/key/energy
    via :func:`track_compatibility` and never uses genre cosine.
    """
    if mode == "specific":
        if library is None:
            raise ValueError("library is required for specific find-similar mode")
        return find_similar_specific(current_stable_id=stable_id, library=library, limit=limit)
    if mode == "niche":
        raise findings.GenreInputError("find-similar mode 'niche' is not implemented yet")
    return genre_service.find_similar_tracks(conn, stable_id, mode=mode, limit=limit)


def _rationale_tags(sc: ScoredCandidate) -> list[str]:
    tags: list[str] = []
    r = sc.rationale
    if r.get("bpm", 0.0) >= 0.9:
        tags.append("bpm_match")
    elif r.get("bpm", 0.0) >= 0.6:
        tags.append("bpm_close")
    if r.get("camelot", 0.0) >= 0.9:
        tags.append("camelot_step_0")
    elif r.get("camelot", 0.0) >= 0.7:
        tags.append("camelot_step_1_2")
    if r.get("energy", 0.0) >= 0.9:
        tags.append("energy_match")
    for src in ("manual", "learned", "ai"):
        if r.get(f"pair_{src}"):
            tags.append(f"pair_{src}")
    if r.get("pressure_release"):
        tags.append("release_a_little")
    if "days_since_played" in r and r.get("history", 1.0) < 1.0:
        tags.append("played_recently")
    return tags


def _current_to_played(current: TrackFeature) -> PlayedTrack:
    return PlayedTrack(
        stable_id=current.stable_id,
        artist=current.artist,
        bpm=current.bpm,
        key_camelot=current.key_camelot,
        energy=current.energy,
        played_at=datetime.now(UTC),
    )


def suggest_next(
    *,
    conn: sqlite3.Connection,
    current_stable_id: str,
    library: list[TrackFeature],
    context: SessionContext | None = None,
    top_n: int = 10,
    explain: bool = False,
    history_weight: float = 0.0,
) -> list[RankedSuggestion]:
    """Return the top-N next-track candidates, deterministic order.

    ``current_stable_id`` must appear in ``library``; otherwise returns
    an empty list (and logs at WARNING at the stage-1 rank site).
    ``context`` defaults to an empty session when None.
    ``history_weight`` applies the recency term (0 keeps stage-1 byte-identical).
    """
    # Resolve current from the library.
    current: TrackFeature | None = next((t for t in library if t.stable_id == current_stable_id), None)
    if current is None:
        return []

    if context is None:
        context = SessionContext(recent=[], source="empty", captured_at=datetime.now(UTC))

    current_played = _current_to_played(current)

    filtered, _trace = filter_candidates(
        current_track=current_played,
        library=library,
        context=context,
    )
    stage1 = rank_stage1(
        current_track=current_played,
        candidates=filtered,
        top_n=40,
        history_weight=history_weight,
        now=context.captured_at,
    )
    stage2 = rerank_with_pairings(conn=conn, current_stable_id=current_stable_id, stage1=stage1)
    peak_plays = [
        PeakPlay(
            stable_id=p.stable_id,
            energy=p.energy,
            tags=p.tags,
        )
        for p in context.recent
    ]
    pressure = compute_peak_pressure(peak_plays)
    stage2 = apply_pressure_prior(stage2, pressure)
    top = stage2[:top_n]

    out: list[RankedSuggestion] = []
    for sc in top:
        tags = _rationale_tags(sc)
        if explain:
            cand_tf = sc.candidate or TrackFeature(sc.stable_id, None, None, None, None)
            explain_text = _explain(current, cand_tf, sc.rationale)
        else:
            explain_text = None
        out.append(
            RankedSuggestion(
                stable_id=sc.stable_id,
                score=sc.score,
                rationale_tags=tags,
                rationale_numbers=dict(sc.rationale),
                explain_text=explain_text,
                candidate=sc.candidate,
                unscored=sc.unscored,
            )
        )
    return out
