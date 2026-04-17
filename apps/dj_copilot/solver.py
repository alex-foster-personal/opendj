"""Greedy beam PLAY IT solver (PLAY-02). Deterministic; no randomness."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from apps.shared.harmonic import (
    CAMELOT_STEP_BUDGET_COMPATIBLE,
    CAMELOT_STEP_BUDGET_HARDCUT,
    MAX_BPM_DIFF_PCT,
    TrackFeature,
    bpm_compatibility,
    camelot_compatibility,
    camelot_distance as _camelot_distance,
    key_to_camelot,
)

from .energy_curve import target_energy_at
from .set_goal import SetGoal

W_KEY: float = 0.35
W_BPM: float = 0.35
W_ENERGY: float = 0.25
W_ARTIST: float = 0.05

ARTIST_REPEAT_COOLDOWN: int = 8
AVG_TRACK_MINUTES: float = 4.0
_BPM_RELAX_WINDOW_PCT: float = 10.0

UnmetKind = Literal["bpm_window", "camelot_hardcut", "artist_repeat", "energy_miss"]


@dataclass(slots=True)
class UnmetConstraint:
    kind: UnmetKind
    position: int
    detail: dict[str, float | str] = field(default_factory=dict)


@dataclass(slots=True)
class StepTrace:
    position: int
    stable_id: str
    score: float
    camelot_distance: int | None
    bpm_delta_pct: float | None
    target_energy: float
    actual_energy: float
    transition_hint: str


@dataclass(slots=True)
class SolveResult:
    order: list[str]
    per_step_scores: list[float]
    per_step_trace: list[StepTrace]
    constraints_unmet: list[UnmetConstraint]
    solve_ms: float


def _camelot_dist_or_none(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return _camelot_distance(key_to_camelot(a), key_to_camelot(b))
    except ValueError:
        return None


def _bpm_delta_pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return (max(a, b) / min(a, b) - 1.0) * 100.0


def _infer_hint(prev: TrackFeature, cand: TrackFeature, bpm_relaxed: bool) -> str:
    dist = _camelot_dist_or_none(prev.key_camelot, cand.key_camelot)
    if dist is not None and dist >= CAMELOT_STEP_BUDGET_HARDCUT:
        return "hardcut"
    if bpm_relaxed:
        return "bpm-stretch"
    if dist is not None and dist == 0:
        return "bpm-match"
    if dist is not None and dist <= CAMELOT_STEP_BUDGET_COMPATIBLE:
        return f"camelot-step-{dist}"
    return "mixed"


@dataclass(slots=True)
class _BeamState:
    order: tuple[str, ...]
    score_sum: float
    traces: tuple[StepTrace, ...]
    unmet: tuple[UnmetConstraint, ...]
    recent_artists: tuple[str | None, ...]


def _score_candidate_inline(
    *,
    prev: TrackFeature,
    cand: TrackFeature,
    target_energy_slot: float,
    recent_artists_set: set[str | None],
) -> tuple[float, bool]:
    tight = bpm_compatibility(prev.bpm, cand.bpm, max_diff_pct=MAX_BPM_DIFF_PCT)
    bpm_relaxed = False
    bpm_score = tight
    if tight == 0.0 and prev.bpm is not None and cand.bpm is not None:
        relaxed = bpm_compatibility(
            prev.bpm, cand.bpm, max_diff_pct=_BPM_RELAX_WINDOW_PCT
        )
        if relaxed > 0.0:
            bpm_score = relaxed * 0.5
            bpm_relaxed = True
    pk = prev.key_camelot
    ck = cand.key_camelot
    if not pk or not ck:
        key_score = 0.5
    else:
        try:
            key_score = camelot_compatibility(pk, ck)
        except ValueError:
            key_score = 0.5
    if cand.energy is None:
        energy_score = 0.5
    else:
        energy_score = max(
            0.0, 1.0 - abs(target_energy_slot - float(cand.energy)) / 9.0
        )
    if cand.artist is None:
        artist_score = 0.5
    elif cand.artist in recent_artists_set:
        artist_score = 0.0
    else:
        artist_score = 1.0
    composite = (
        W_KEY * key_score
        + W_BPM * bpm_score
        + W_ENERGY * energy_score
        + W_ARTIST * artist_score
    )
    return composite, bpm_relaxed


def _best_start(tracks: list[TrackFeature], goal: SetGoal) -> TrackFeature:
    target_energy = float(goal.floor_energy)
    open_key = goal.open_on_key

    def key(t: TrackFeature) -> tuple[float, int, str]:
        e_dist = (
            abs(float(t.energy) - target_energy)
            if t.energy is not None
            else 10.0
        )
        key_mismatch = 1
        if open_key is not None and t.key_camelot is not None:
            try:
                key_mismatch = int(
                    _camelot_distance(
                        key_to_camelot(open_key), key_to_camelot(t.key_camelot)
                    )
                )
            except ValueError:
                key_mismatch = 12
        return (e_dist, key_mismatch, t.stable_id)

    return min(tracks, key=key)


def suggest_order(
    *,
    tracks: list[TrackFeature],
    goal: SetGoal,
    beam_width: int = 8,
    seed: int = 0,
) -> SolveResult:
    """Greedy beam search. Deterministic. Same inputs -> identical order."""
    del seed
    t0 = time.perf_counter()

    if not tracks:
        return SolveResult([], [], [], [], 0.0)

    index: dict[str, TrackFeature] = {t.stable_id: t for t in tracks}

    start = _best_start(tracks, goal)
    start_trace = StepTrace(
        position=0,
        stable_id=start.stable_id,
        score=1.0,
        camelot_distance=None,
        bpm_delta_pct=None,
        target_energy=target_energy_at(goal, 0.0),
        actual_energy=float(start.energy) if start.energy is not None else 0.0,
        transition_hint="start",
    )
    init_beam = _BeamState(
        order=(start.stable_id,),
        score_sum=1.0,
        traces=(start_trace,),
        unmet=(),
        recent_artists=(start.artist,),
    )
    beams: list[_BeamState] = [init_beam]

    total = len(tracks)

    for slot_idx in range(1, total):
        target_energy_slot = target_energy_at(goal, slot_idx * AVG_TRACK_MINUTES)
        next_beams: list[_BeamState] = []
        for state in beams:
            chosen = set(state.order)
            prev = index[state.order[-1]]
            recent_set = set(state.recent_artists[-ARTIST_REPEAT_COOLDOWN:])
            scored: list[tuple[float, str, TrackFeature, bool]] = []
            for cand in tracks:
                if cand.stable_id in chosen:
                    continue
                score, relaxed = _score_candidate_inline(
                    prev=prev,
                    cand=cand,
                    target_energy_slot=target_energy_slot,
                    recent_artists_set=recent_set,
                )
                scored.append((score, cand.stable_id, cand, relaxed))
            if not scored:
                next_beams.append(state)
                continue
            scored.sort(key=lambda x: (-x[0], x[1]))
            for score, _sid, cand, relaxed in scored[:beam_width]:
                dist = _camelot_dist_or_none(prev.key_camelot, cand.key_camelot)
                delta = _bpm_delta_pct(prev.bpm, cand.bpm)
                hint = _infer_hint(prev, cand, relaxed)
                trace = StepTrace(
                    position=slot_idx,
                    stable_id=cand.stable_id,
                    score=score,
                    camelot_distance=dist,
                    bpm_delta_pct=delta,
                    target_energy=target_energy_slot,
                    actual_energy=(
                        float(cand.energy) if cand.energy is not None else 0.0
                    ),
                    transition_hint=hint,
                )
                unmet = list(state.unmet)
                if relaxed:
                    unmet.append(
                        UnmetConstraint(
                            kind="bpm_window",
                            position=slot_idx,
                            detail={
                                "delta_pct": delta if delta is not None else 0.0,
                                "stable_id": cand.stable_id,
                            },
                        )
                    )
                if dist is not None and dist >= CAMELOT_STEP_BUDGET_HARDCUT:
                    unmet.append(
                        UnmetConstraint(
                            kind="camelot_hardcut",
                            position=slot_idx,
                            detail={
                                "distance": float(dist),
                                "stable_id": cand.stable_id,
                            },
                        )
                    )
                next_beams.append(
                    _BeamState(
                        order=state.order + (cand.stable_id,),
                        score_sum=state.score_sum + score,
                        traces=state.traces + (trace,),
                        unmet=tuple(unmet),
                        recent_artists=state.recent_artists + (cand.artist,),
                    )
                )
        next_beams.sort(key=lambda s: (-s.score_sum, s.order))
        beams = next_beams[:beam_width]
        if not beams:
            break

    top_score = max(x.score_sum for x in beams)
    best = min(
        [b for b in beams if b.score_sum == top_score],
        key=lambda s: s.order,
    )

    solve_ms = (time.perf_counter() - t0) * 1000.0
    return SolveResult(
        order=list(best.order),
        per_step_scores=[t.score for t in best.traces],
        per_step_trace=list(best.traces),
        constraints_unmet=list(best.unmet),
        solve_ms=solve_ms,
    )
