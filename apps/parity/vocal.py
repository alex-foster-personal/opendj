"""Vocal lane: rekordbox PVDI regions vs own demucs regions (SPIKE-B2 IoU)."""

from __future__ import annotations

import math
from typing import Any

from apps.parity.figure import LaneFigure
from apps.parity.lanes import DENOMINATOR_NAME

_VOCAL_GRID_HZ = 10
_ONSET_MATCH_WINDOW_S = 5.0


def _region_endpoints(regions: Any) -> list[tuple[float, float]]:
    if not isinstance(regions, list):
        raise TypeError("vocal regions must be a list")
    out: list[tuple[float, float]] = []
    for item in regions:
        if not isinstance(item, dict):
            raise TypeError("each vocal region must be a dict")
        start = item.get("start_s")
        end = item.get("end_s")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise TypeError("vocal region start_s/end_s must be numbers")
        if end <= start:
            raise ValueError("vocal region end_s must be greater than start_s")
        out.append((float(start), float(end)))
    return out


def _frame_count(duration_s: float) -> int:
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    return max(1, math.ceil(duration_s * _VOCAL_GRID_HZ))


def _rasterize(regions: list[tuple[float, float]], *, frame_count: int) -> list[bool]:
    mask = [False] * frame_count
    for start_s, end_s in regions:
        start_frame = max(0, int(start_s * _VOCAL_GRID_HZ))
        end_frame = min(frame_count, math.ceil(end_s * _VOCAL_GRID_HZ))
        for frame in range(start_frame, end_frame):
            mask[frame] = True
    return mask


def vocal_iou(
    rb_regions: list[tuple[float, float]],
    own_regions: list[tuple[float, float]],
    *,
    duration_s: float,
) -> tuple[float, float, float]:
    """Return IoU, precision, recall on a 10 Hz frame grid."""
    frame_count = _frame_count(duration_s)
    rb_mask = _rasterize(rb_regions, frame_count=frame_count)
    own_mask = _rasterize(own_regions, frame_count=frame_count)
    intersection = sum(1 for rb, own in zip(rb_mask, own_mask, strict=True) if rb and own)
    rb_pos = sum(rb_mask)
    own_pos = sum(own_mask)
    union = sum(1 for rb, own in zip(rb_mask, own_mask, strict=True) if rb or own)
    iou = 1.0 if union == 0 else intersection / union
    precision = 1.0 if own_pos == 0 else intersection / own_pos
    recall = 1.0 if rb_pos == 0 else intersection / rb_pos
    return iou, precision, recall


def vocal_onset_mae(
    rb_regions: list[tuple[float, float]],
    own_regions: list[tuple[float, float]],
) -> tuple[float | None, int]:
    """Mean absolute onset error for rb starts matched within 5 s."""
    if not rb_regions:
        return None, 0
    if not own_regions:
        return None, 0
    own_starts = [start for start, _ in own_regions]
    errors: list[float] = []
    for rb_start, _ in rb_regions:
        nearest = min(own_starts, key=lambda own: abs(own - rb_start))
        if abs(nearest - rb_start) <= _ONSET_MATCH_WINDOW_S:
            errors.append(abs(nearest - rb_start))
    if not errors:
        return None, 0
    return sum(errors) / len(errors), len(errors)


def score_vocal(rows: list[dict[str, Any]], *, measured_at: str) -> LaneFigure:
    """Score present rows that carry the rekordbox PVDI fourcc."""
    ungradable_ids: list[str] = []
    no_own_ids: list[str] = []
    agree_ids: list[str] = []
    disagree_ids: list[str] = []
    failed_own_n = 0
    ious: list[float] = []
    precisions: list[float] = []
    recalls: list[float] = []

    for row in rows:
        sid = row["stable_id"]
        if not row.get("rb_pvdi"):
            ungradable_ids.append(sid)
            continue
        own_raw = row.get("own_vocal_regions")
        if own_raw is None:
            no_own_ids.append(sid)
            continue
        duration_s = row.get("duration_s")
        if not isinstance(duration_s, (int, float)) or isinstance(duration_s, bool):
            failed_own_n += 1
            continue
        rb_raw = row.get("rb_vocal_regions")
        if rb_raw is None:
            rb_raw = []
        try:
            rb_regions = _region_endpoints(rb_raw)
            own_regions = _region_endpoints(own_raw)
            iou, precision, recall = vocal_iou(
                rb_regions,
                own_regions,
                duration_s=float(duration_s),
            )
        except (TypeError, ValueError):
            failed_own_n += 1
            continue
        ious.append(iou)
        precisions.append(precision)
        recalls.append(recall)
        if iou == 1.0:
            agree_ids.append(sid)
        else:
            disagree_ids.append(sid)

    scored_n = len(agree_ids) + len(disagree_ids)
    iou_mean = sum(ious) / len(ious) if ious else None
    precision_mean = sum(precisions) / len(precisions) if precisions else None
    recall_mean = sum(recalls) / len(recalls) if recalls else None

    return LaneFigure(
        lane="vocal",
        status="scored",
        measured_at=measured_at,
        denominator_name=DENOMINATOR_NAME["vocal"],
        denominator_n=len(rows) - len(ungradable_ids),
        scored_n=scored_n,
        exact_n=len(agree_ids),
        no_own_n=len(no_own_ids),
        failed_own_n=failed_own_n,
        iou_mean=iou_mean,
        precision_mean=precision_mean,
        recall_mean=recall_mean,
        agree_ids=tuple(agree_ids),
        disagree_ids=tuple(disagree_ids),
        no_own_ids=tuple(no_own_ids),
        ungradable_ids=tuple(ungradable_ids),
        ungradable={"missing_pvdi": len(ungradable_ids)},
    )
