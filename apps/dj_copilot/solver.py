"""Greedy beam PLAY IT solver (PLAY-02). Deterministic; no randomness."""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from apps.shared.harmonic import (
    CAMELOT_STEP_BUDGET_COMPATIBLE,
    CAMELOT_STEP_BUDGET_HARDCUT,
    MAX_BPM_DIFF_PCT,
    TrackFeature,
    bpm_compatibility,
    camelot_compatibility,
    key_to_camelot,
)
from apps.shared.harmonic import (
    camelot_distance as _camelot_distance,
)

from .energy_curve import target_energy_at
from .set_goal import SetGoal

PinRole = Literal["opener", "peak", "closer"] | None

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
    pin_role: PinRole = None


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
    occurrence_order: tuple[int, ...]
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


# (score, stable_id, occurrence, track, bpm_relaxed), the sort key first.
_Scored = tuple[float, str, int, TrackFeature, bool]


def _advance(
    state: _BeamState,
    *,
    prev: TrackFeature,
    scored: _Scored,
    slot_idx: int,
    target_energy_slot: float,
    pin_role: PinRole,
    record_unmet: bool,
) -> _BeamState:
    """Extend `state` by one scored candidate, building its trace.

    `record_unmet` is False for a pinned slot, which keeps the parent's unmet
    constraints unchanged; free slots record bpm_window and camelot_hardcut.
    """
    score, _sid, occurrence, cand, relaxed = scored
    dist = _camelot_dist_or_none(prev.key_camelot, cand.key_camelot)
    delta = _bpm_delta_pct(prev.bpm, cand.bpm)
    trace = StepTrace(
        position=slot_idx,
        stable_id=cand.stable_id,
        score=score,
        camelot_distance=dist,
        bpm_delta_pct=delta,
        target_energy=target_energy_slot,
        actual_energy=float(cand.energy) if cand.energy is not None else 0.0,
        transition_hint=_infer_hint(prev, cand, relaxed),
        pin_role=pin_role,
    )
    unmet = list(state.unmet)
    if record_unmet and relaxed:
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
    if record_unmet and dist is not None and dist >= CAMELOT_STEP_BUDGET_HARDCUT:
        unmet.append(
            UnmetConstraint(
                kind="camelot_hardcut",
                position=slot_idx,
                detail={"distance": float(dist), "stable_id": cand.stable_id},
            )
        )
    return _BeamState(
        occurrence_order=(*state.occurrence_order, occurrence),
        score_sum=state.score_sum + score,
        traces=(*state.traces, trace),
        unmet=tuple(unmet),
        recent_artists=(*state.recent_artists, cand.artist),
    )


def _expand_free_slot(
    state: _BeamState,
    *,
    tracks: list[TrackFeature],
    slot_idx: int,
    target_energy_slot: float,
    beam_width: int,
    excluded: set[int] | frozenset[int] = frozenset(),
    role_by_occurrence: Mapping[int, PinRole] | None = None,
) -> list[_BeamState]:
    """Score every unchosen, unexcluded track and keep the top `beam_width`.

    Returns `[state]` unchanged when no candidate is left.
    """
    roles = role_by_occurrence or {}
    chosen = set(state.occurrence_order) | excluded
    prev = tracks[state.occurrence_order[-1]]
    recent_set = set(state.recent_artists[-ARTIST_REPEAT_COOLDOWN:])
    scored: list[_Scored] = []
    for occurrence, cand in enumerate(tracks):
        if occurrence in chosen:
            continue
        score, relaxed = _score_candidate_inline(
            prev=prev,
            cand=cand,
            target_energy_slot=target_energy_slot,
            recent_artists_set=recent_set,
        )
        scored.append((score, cand.stable_id, occurrence, cand, relaxed))
    if not scored:
        return [state]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [
        _advance(
            state,
            prev=prev,
            scored=entry,
            slot_idx=slot_idx,
            target_energy_slot=target_energy_slot,
            pin_role=roles.get(entry[2]),
            record_unmet=True,
        )
        for entry in scored[:beam_width]
    ]


def _advance_reserved_slot(
    state: _BeamState,
    *,
    tracks: list[TrackFeature],
    occurrence: int,
    pin_role: PinRole,
    slot_idx: int,
    target_energy_slot: float,
) -> _BeamState | None:
    """Place the pinned `occurrence` at `slot_idx`, or None if already used."""
    if occurrence in state.occurrence_order:
        return None
    prev = tracks[state.occurrence_order[-1]]
    cand = tracks[occurrence]
    score, relaxed = _score_candidate_inline(
        prev=prev,
        cand=cand,
        target_energy_slot=target_energy_slot,
        recent_artists_set=set(state.recent_artists[-ARTIST_REPEAT_COOLDOWN:]),
    )
    return _advance(
        state,
        prev=prev,
        scored=(score, cand.stable_id, occurrence, cand, relaxed),
        slot_idx=slot_idx,
        target_energy_slot=target_energy_slot,
        pin_role=pin_role,
        record_unmet=False,
    )


def _initial_beam(
    tracks: list[TrackFeature],
    goal: SetGoal,
    start_occurrence: int,
    pin_role: PinRole = None,
) -> _BeamState:
    start = tracks[start_occurrence]
    start_trace = StepTrace(
        position=0,
        stable_id=start.stable_id,
        score=1.0,
        camelot_distance=None,
        bpm_delta_pct=None,
        target_energy=target_energy_at(goal, 0.0),
        actual_energy=float(start.energy) if start.energy is not None else 0.0,
        transition_hint="start",
        pin_role=pin_role,
    )
    return _BeamState(
        occurrence_order=(start_occurrence,),
        score_sum=1.0,
        traces=(start_trace,),
        unmet=(),
        recent_artists=(start.artist,),
    )


def _prune(next_beams: list[_BeamState], beam_width: int) -> list[_BeamState]:
    next_beams.sort(key=lambda s: (-s.score_sum, s.occurrence_order))
    return next_beams[:beam_width]


def _best_beam(beams: list[_BeamState]) -> _BeamState:
    top_score = max(x.score_sum for x in beams)
    return min(
        [b for b in beams if b.score_sum == top_score],
        key=lambda s: s.occurrence_order,
    )


def _result(
    order: list[str], best: _BeamState, t0: float
) -> SolveResult:
    solve_ms = (time.perf_counter() - t0) * 1000.0
    return SolveResult(
        order=order,
        per_step_scores=[t.score for t in best.traces],
        per_step_trace=list(best.traces),
        constraints_unmet=list(best.unmet),
        solve_ms=solve_ms,
    )


def _best_start_occurrence(tracks: list[TrackFeature], goal: SetGoal) -> int:
    from .pinning import _opener_occurrence_rank_key

    def key(occurrence: tuple[int, TrackFeature]) -> tuple[float, int, str, int]:
        position, track = occurrence
        return _opener_occurrence_rank_key(
            position,
            track,
            floor_energy=goal.floor_energy,
            open_on_key=goal.open_on_key,
        )

    return min(enumerate(tracks), key=key)[0]


def suggest_order(
    *,
    tracks: list[TrackFeature],
    goal: SetGoal,
    beam_width: int = 8,
    seed: int = 0,
) -> SolveResult:
    """Greedy beam search. Deterministic. Same inputs -> identical order."""
    if not goal.peak_pins and not goal.opener_pins and goal.closer_pin is None:
        return _suggest_order_unpinned(
            tracks=tracks, goal=goal, beam_width=beam_width, seed=seed
        )
    return _suggest_order_pinned(
        tracks=tracks, goal=goal, beam_width=beam_width, seed=seed
    )


def _suggest_order_unpinned(
    *,
    tracks: list[TrackFeature],
    goal: SetGoal,
    beam_width: int = 8,
    seed: int = 0,
) -> SolveResult:
    """Greedy beam search without track pins."""
    del seed
    t0 = time.perf_counter()

    if not tracks:
        return SolveResult([], [], [], [], 0.0)

    start_occurrence = _best_start_occurrence(tracks, goal)
    beams: list[_BeamState] = [_initial_beam(tracks, goal, start_occurrence)]

    for slot_idx in range(1, len(tracks)):
        target_energy_slot = target_energy_at(goal, slot_idx * AVG_TRACK_MINUTES)
        next_beams: list[_BeamState] = []
        for state in beams:
            next_beams.extend(
                _expand_free_slot(
                    state,
                    tracks=tracks,
                    slot_idx=slot_idx,
                    target_energy_slot=target_energy_slot,
                    beam_width=beam_width,
                )
            )
        beams = _prune(next_beams, beam_width)
        if not beams:
            break

    best = _best_beam(beams)
    order = [tracks[occurrence].stable_id for occurrence in best.occurrence_order]
    return _result(order, best, t0)


def _suggest_order_pinned(
    *,
    tracks: list[TrackFeature],
    goal: SetGoal,
    beam_width: int = 8,
    seed: int = 0,
) -> SolveResult:
    """Greedy beam search honoring SET-04 track pins."""
    del seed
    from .pinning import (
        PinUnsatisfiableError,
        plan_reservations,
        start_occurrence_for_pinned,
    )

    t0 = time.perf_counter()
    if not tracks:
        return SolveResult([], [], [], [], 0.0)

    reservations = plan_reservations(tracks, goal)
    reserved_occurrences = {r.occurrence for r in reservations.values()}
    role_by_occurrence = {
        r.occurrence: r.role for r in reservations.values()
    }

    start_occurrence = start_occurrence_for_pinned(tracks, goal, reservations)
    if 0 in reservations and reservations[0].occurrence != start_occurrence:
        raise PinUnsatisfiableError("opener_unsatisfiable")

    beams: list[_BeamState] = [
        _initial_beam(
            tracks, goal, start_occurrence, role_by_occurrence.get(start_occurrence)
        )
    ]
    total = len(tracks)

    for slot_idx in range(1, total):
        target_energy_slot = target_energy_at(goal, slot_idx * AVG_TRACK_MINUTES)
        fixed = reservations.get(slot_idx)
        next_beams: list[_BeamState] = []
        for state in beams:
            if fixed is None:
                next_beams.extend(
                    _expand_free_slot(
                        state,
                        tracks=tracks,
                        slot_idx=slot_idx,
                        target_energy_slot=target_energy_slot,
                        beam_width=beam_width,
                        excluded=reserved_occurrences,
                        role_by_occurrence=role_by_occurrence,
                    )
                )
                continue
            placed = _advance_reserved_slot(
                state,
                tracks=tracks,
                occurrence=fixed.occurrence,
                pin_role=fixed.role,
                slot_idx=slot_idx,
                target_energy_slot=target_energy_slot,
            )
            if placed is not None:
                next_beams.append(placed)
        beams = _prune(next_beams, beam_width)
        if not beams:
            break

    if not beams:
        raise PinUnsatisfiableError("opener_unsatisfiable")

    best = _best_beam(beams)
    order = [tracks[occurrence].stable_id for occurrence in best.occurrence_order]
    _check_pins_honored(goal, order, total)
    return _result(order, best, t0)


def _check_pins_honored(goal: SetGoal, order: list[str], total: int) -> None:
    """Raise PinUnsatisfiableError when `order` breaks a SET-04 pin."""
    from .pinning import PinUnsatisfiableError, assign_peak_pin_slots

    if goal.opener_pins and order[0] not in goal.opener_pins:
        raise PinUnsatisfiableError("opener_unsatisfiable")
    if goal.closer_pin is not None and order[-1] != goal.closer_pin:
        raise PinUnsatisfiableError("closer_unsatisfiable")
    peak_slots = set(assign_peak_pin_slots(goal, total).keys())
    for slot, stable_id in enumerate(order):
        if stable_id in goal.peak_pins and slot not in peak_slots:
            raise PinUnsatisfiableError("peak_window_too_small")
