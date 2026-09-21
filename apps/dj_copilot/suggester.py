"""suggest_next -- AI-01 public API (Phase 13 Plan 03).

Composition:

    current -> filter_candidates -> Stage 1 rank -> Stage 2 rerank
            -> translate scores to rationale_tags -> top_n

The optional ``explain=True`` path is synchronous in Phase 13: it calls
the stub explainer inline. The documented event-bus contract
(``ai.explain_requested`` / ``ai.explain_response``) is shipped in
``docs/dj_copilot_events.md`` for Phase 14 / Phase 17 consumers.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC

from apps.shared.harmonic import TrackFeature

from .candidates import filter_candidates
from .explainer_stub import explain as _stub_explain
from .peak_pressure import PeakPlay, apply_pressure_prior, compute_peak_pressure
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
    return tags


def _current_to_played(current: TrackFeature) -> PlayedTrack:
    from datetime import datetime

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
) -> list[RankedSuggestion]:
    """Return the top-N next-track candidates, deterministic order.

    ``current_stable_id`` must appear in ``library``; otherwise returns
    an empty list (and logs at WARNING at the stage-1 rank site).
    ``context`` defaults to an empty session when None.
    """
    # Resolve current from the library.
    current: TrackFeature | None = next(
        (t for t in library if t.stable_id == current_stable_id), None
    )
    if current is None:
        return []

    if context is None:
        from datetime import datetime

        context = SessionContext(
            recent=[], source="empty", captured_at=datetime.now(UTC)
        )

    current_played = _current_to_played(current)

    filtered, _trace = filter_candidates(
        current_track=current_played,
        library=library,
        context=context,
    )
    stage1 = rank_stage1(
        current_track=current_played, candidates=filtered, top_n=40
    )
    stage2 = rerank_with_pairings(
        conn=conn, current_stable_id=current_stable_id, stage1=stage1
    )
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
        explain_text = _stub_explain(sc) if explain else None
        out.append(
            RankedSuggestion(
                stable_id=sc.stable_id,
                score=sc.score,
                rationale_tags=tags,
                rationale_numbers=dict(sc.rationale),
                explain_text=explain_text,
                candidate=sc.candidate,
            )
        )
    return out
