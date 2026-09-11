"""Phrase lane: rekordbox ANLZ PSSI vs own phrase analysis.

Compares four-field phrase objects (start_s, end_s, kind, mood) at the
0.001 s storage grain. Boundary F-measure bands at 0.5 s and 3.0 s and
kind accuracy are reporting bands, not a pass line.
"""

from __future__ import annotations

from typing import Any

from apps.parity.figure import LaneFigure
from apps.parity.lanes import DENOMINATOR_NAME

Phrase = dict[str, Any]


def _round3(value: float) -> float:
    return round(float(value), 3)


def _normalize_phrases(raw: list[Any] | None) -> list[Phrase]:
    if not raw:
        return []
    return [
        {
            "start_s": _round3(item["start_s"]),
            "end_s": _round3(item["end_s"]),
            "kind": int(item["kind"]),
            "mood": int(item["mood"]),
        }
        for item in raw
    ]


def _boundary_set(phrases: list[Phrase]) -> list[float]:
    if not phrases:
        return []
    bounds = [_round3(p["start_s"]) for p in phrases]
    bounds.append(_round3(phrases[-1]["end_s"]))
    return bounds


def _track_mood(phrases: list[Phrase]) -> int | None:
    if not phrases:
        return None
    return int(phrases[0]["mood"])


def _is_exact(rb: list[Phrase], own: list[Phrase]) -> bool:
    if len(rb) != len(own):
        return False
    for ref, est in zip(rb, own, strict=True):
        if (
            _round3(ref["start_s"]) != _round3(est["start_s"])
            or _round3(ref["end_s"]) != _round3(est["end_s"])
            or int(ref["kind"]) != int(est["kind"])
            or int(ref["mood"]) != int(est["mood"])
        ):
            return False
    return True


def _nearest_unused_index(
    target: float,
    values: list[float],
    used: set[int],
    window_s: float,
) -> int:
    best_idx = -1
    best_dist = window_s + 1.0
    for idx, value in enumerate(values):
        if idx in used:
            continue
        dist = abs(target - value)
        if dist <= window_s and dist < best_dist:
            best_dist = dist
            best_idx = idx
    return best_idx


def _greedy_boundary_f(ref_bounds: list[float], est_bounds: list[float], window_s: float) -> float:
    if not ref_bounds and not est_bounds:
        return 1.0
    if not ref_bounds or not est_bounds:
        return 0.0
    used_est: set[int] = set()
    matched = 0
    for ref in ref_bounds:
        best_idx = _nearest_unused_index(ref, est_bounds, used_est, window_s)
        if best_idx >= 0:
            used_est.add(best_idx)
            matched += 1
    n_ref = len(ref_bounds)
    n_est = len(est_bounds)
    precision = matched / n_est if n_est else 0.0
    recall = matched / n_ref if n_ref else 0.0
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def _kind_accuracy_at_half_second(rb: list[Phrase], own: list[Phrase]) -> float:
    rb_mood = _track_mood(rb)
    own_mood = _track_mood(own)
    if rb_mood is not None and own_mood is not None and rb_mood != own_mood:
        return 0.0
    ref_starts = [_round3(p["start_s"]) for p in rb]
    est_starts = [_round3(p["start_s"]) for p in own]
    if not ref_starts:
        return 0.0
    used_est: set[int] = set()
    kind_matches = 0
    start_matches = 0
    for ref_idx, ref_start in enumerate(ref_starts):
        best_idx = _nearest_unused_index(ref_start, est_starts, used_est, 0.5)
        if best_idx < 0:
            continue
        used_est.add(best_idx)
        start_matches += 1
        if int(rb[ref_idx]["kind"]) == int(own[best_idx]["kind"]):
            kind_matches += 1
    if start_matches == 0:
        return 0.0
    return kind_matches / start_matches


def _mood_disagreement(rb: list[Phrase], own: list[Phrase]) -> bool:
    rb_mood = _track_mood(rb)
    own_mood = _track_mood(own)
    return rb_mood is not None and own_mood is not None and rb_mood != own_mood


def _classify_phrase_row(
    row: dict[str, Any],
) -> tuple[str, str, list[Phrase], list[Phrase]] | None:
    sid = row["stable_id"]
    if not row.get("rb_pssi"):
        return ("ungradable", sid, [], [])
    if "own_phrases" not in row or row.get("own_phrases") is None:
        return ("no_own", sid, [], [])
    rb = _normalize_phrases(row.get("rb_phrases"))
    own = _normalize_phrases(row.get("own_phrases"))
    if _is_exact(rb, own):
        return ("agree", sid, rb, own)
    return ("disagree", sid, rb, own)


def score_phrase(rows: list[dict[str, Any]], *, measured_at: str) -> LaneFigure:
    """Score present rows that carry rekordbox PSSI."""
    ungradable_ids: list[str] = []
    no_own_ids: list[str] = []
    agree_ids: list[str] = []
    disagree_ids: list[str] = []
    boundary_f_0_5: list[float] = []
    boundary_f_3_0: list[float] = []
    label_acc: list[float] = []

    for row in rows:
        outcome = _classify_phrase_row(row)
        if outcome is None:
            continue
        kind, sid, rb, own = outcome
        if kind == "ungradable":
            ungradable_ids.append(sid)
            continue
        if kind == "no_own":
            no_own_ids.append(sid)
            continue
        if kind == "agree":
            agree_ids.append(sid)
        else:
            disagree_ids.append(sid)
        ref_bounds = _boundary_set(rb)
        est_bounds = _boundary_set(own)
        boundary_f_0_5.append(_greedy_boundary_f(ref_bounds, est_bounds, 0.5))
        boundary_f_3_0.append(_greedy_boundary_f(ref_bounds, est_bounds, 3.0))
        label_acc.append(_kind_accuracy_at_half_second(rb, own))

    denominator_n = len(rows) - len(ungradable_ids)
    scored_n = len(agree_ids) + len(disagree_ids)
    mean_f_0_5 = (sum(boundary_f_0_5) / len(boundary_f_0_5)) if boundary_f_0_5 else None
    mean_f_3_0 = (sum(boundary_f_3_0) / len(boundary_f_3_0)) if boundary_f_3_0 else None
    mean_label = (sum(label_acc) / len(label_acc)) if label_acc else None

    return LaneFigure(
        lane="phrase",
        status="scored",
        measured_at=measured_at,
        denominator_name=DENOMINATOR_NAME["phrase"],
        denominator_n=denominator_n,
        scored_n=scored_n,
        exact_n=len(agree_ids),
        boundary_f_0_5=mean_f_0_5,
        boundary_f_3_0=mean_f_3_0,
        label_acc=mean_label,
        no_own_n=len(no_own_ids),
        agree_ids=tuple(agree_ids),
        disagree_ids=tuple(disagree_ids),
        no_own_ids=tuple(no_own_ids),
        ungradable_ids=tuple(ungradable_ids),
        ungradable={"missing_pssi": len(ungradable_ids)},
    )
