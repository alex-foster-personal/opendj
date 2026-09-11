"""Waveform preview, detail, and tri-band lanes vs rekordbox ANLZ ground truth."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from apps.analysis_waveform.bands import _downsample_max
from apps.analysis_waveform.decode import BAND_NAMES
from apps.analysis_waveform.score import POSITIVE_CONTROL_TOLERANCE, pearson
from apps.parity.figure import LaneFigure
from apps.parity.lanes import DENOMINATOR_NAME

_EXACT_TOLERANCE = POSITIVE_CONTROL_TOLERANCE


def _is_non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 2


def _has_full_triband(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(value.get(name), list) and len(value[name]) >= 2 for name in BAND_NAMES
    )


def _align_1d(ours: np.ndarray, truth: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Peak-max align two 1-D envelopes to the shorter width."""
    if ours.shape[0] == truth.shape[0]:
        return ours, truth
    if ours.shape[0] > truth.shape[0]:
        aligned = _downsample_max(ours.reshape(-1, 1), truth.shape[0]).reshape(-1)
        return aligned, truth
    aligned = _downsample_max(truth.reshape(-1, 1), ours.shape[0]).reshape(-1)
    return ours, aligned


def _score_mono_pair(own: list[float], rb: list[float]) -> float:
    ours = np.asarray(own, dtype=np.float64)
    truth = np.asarray(rb, dtype=np.float64)
    ours, truth = _align_1d(ours, truth)
    return pearson(ours, truth)


def _is_exact(r: float) -> bool:
    return math.isfinite(r) and abs(r - 1.0) <= _EXACT_TOLERANCE


def _median(values: list[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return None
    return float(np.median(finite))


def _mean(values: list[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return None
    return float(np.mean(finite))


def _min(values: list[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return None
    return float(np.min(finite))


def _preview_truth_tags(row: dict[str, Any]) -> tuple[list[float] | None, list[float] | None]:
    rb_pwav = row.get("rb_pwav")
    rb_pwv2 = row.get("rb_pwv2")
    primary = rb_pwav if _is_non_empty_list(rb_pwav) else None
    secondary = rb_pwv2 if _is_non_empty_list(rb_pwv2) else None
    if primary is None and secondary is not None:
        primary, secondary = secondary, None
    return primary, secondary


def _detail_truth_tags(
    row: dict[str, Any],
) -> tuple[list[float] | None, list[float] | None]:
    for tag_field in ("rb_pwv3", "rb_pwv4_luminance", "rb_pwv5"):
        value = row.get(tag_field)
        if _is_non_empty_list(value):
            return value, None
    return None, None


def _triband_truth_tags(
    row: dict[str, Any],
) -> tuple[dict[str, list[float]] | None, dict[str, list[float]] | None]:
    rb_pwv6 = row.get("rb_pwv6")
    rb_pwv7 = row.get("rb_pwv7")
    primary = rb_pwv6 if _has_full_triband(rb_pwv6) else None
    secondary = rb_pwv7 if _has_full_triband(rb_pwv7) else None
    if primary is None and secondary is not None:
        primary, secondary = secondary, None
    return primary, secondary


def _preview_gradable(row: dict[str, Any]) -> bool:
    primary, secondary = _preview_truth_tags(row)
    return primary is not None or secondary is not None


def _detail_gradable(row: dict[str, Any]) -> bool:
    if not row.get("rb_ext_readable", True):
        return False
    primary, _secondary = _detail_truth_tags(row)
    return primary is not None


def _triband_gradable(row: dict[str, Any]) -> bool:
    primary, secondary = _triband_truth_tags(row)
    return primary is not None or secondary is not None


def _preview_ungradable_bucket(row: dict[str, Any]) -> str | None:
    if _preview_gradable(row):
        return None
    return "missing_rb_preview"


def _detail_ungradable_bucket(row: dict[str, Any]) -> str | None:
    if not row.get("rb_ext_readable", True):
        return "unreadable_ext"
    if _detail_gradable(row):
        return None
    return "missing_rb_detail"


def _triband_ungradable_bucket(row: dict[str, Any]) -> str | None:
    if _triband_gradable(row):
        return None
    return "missing_rb_triband"


def _score_preview_row(
    row: dict[str, Any],
) -> tuple[str, float | None, float | None]:
    """Return disposition, primary r, secondary r."""
    own = row.get("own_preview")
    if own is None:
        return "no_own", None, None
    primary_truth, secondary_truth = _preview_truth_tags(row)
    if primary_truth is None:
        return "failed_own", None, None
    primary_r = _score_mono_pair(own, primary_truth)
    secondary_r: float | None = None
    tags_scored = [primary_r]
    if secondary_truth is not None:
        secondary_r = _score_mono_pair(own, secondary_truth)
        tags_scored.append(secondary_r)
    if not all(math.isfinite(value) for value in tags_scored):
        return "failed_own", primary_r, secondary_r
    if all(_is_exact(value) for value in tags_scored):
        return "agree", primary_r, secondary_r
    return "disagree", primary_r, secondary_r


def _score_detail_row(row: dict[str, Any]) -> tuple[str, float | None]:
    own = row.get("own_detail")
    if own is None:
        return "no_own", None
    primary_truth, _secondary = _detail_truth_tags(row)
    if primary_truth is None:
        return "failed_own", None
    primary_r = _score_mono_pair(own, primary_truth)
    if not math.isfinite(primary_r):
        return "failed_own", primary_r
    if _is_exact(primary_r):
        return "agree", primary_r
    return "disagree", primary_r


def _score_triband_row(
    row: dict[str, Any],
) -> tuple[str, dict[str, float], float | None]:
    own = row.get("own_triband")
    if own is None:
        return "no_own", {}, None
    if not _has_full_triband(own):
        return "failed_own", {}, None
    primary_truth, _secondary = _triband_truth_tags(row)
    if primary_truth is None:
        return "failed_own", {}, None
    band_rs: dict[str, float] = {}
    for name in BAND_NAMES:
        band_rs[name] = _score_mono_pair(own[name], primary_truth[name])
    if not all(math.isfinite(value) for value in band_rs.values()):
        return "failed_own", band_rs, None
    mean_r = float(np.mean(list(band_rs.values())))
    if all(_is_exact(value) for value in band_rs.values()):
        return "agree", band_rs, mean_r
    return "disagree", band_rs, mean_r


@dataclass
class _WaveformAccum:
    ungradable_ids: list[str] = field(default_factory=list)
    ungradable: dict[str, int] = field(default_factory=dict)
    no_own_ids: list[str] = field(default_factory=list)
    agree_ids: list[str] = field(default_factory=list)
    disagree_ids: list[str] = field(default_factory=list)
    failed_own_n: int = 0
    primary_rs: list[float] = field(default_factory=list)
    secondary_rs: list[float] = field(default_factory=list)
    track_mean_rs: list[float] = field(default_factory=list)
    band_rs_by_name: dict[str, list[float]] = field(
        default_factory=lambda: {name: [] for name in BAND_NAMES}
    )


def _accumulate_preview_row(row: dict[str, Any], accum: _WaveformAccum) -> None:
    sid = row["stable_id"]
    bucket = _preview_ungradable_bucket(row)
    if bucket is not None:
        accum.ungradable_ids.append(sid)
        accum.ungradable[bucket] = accum.ungradable.get(bucket, 0) + 1
        return
    disposition, primary_r, secondary_r = _score_preview_row(row)
    if primary_r is not None and math.isfinite(primary_r):
        accum.primary_rs.append(primary_r)
    if secondary_r is not None and math.isfinite(secondary_r):
        accum.secondary_rs.append(secondary_r)
    _record_disposition(sid, disposition, accum)


def _accumulate_detail_row(row: dict[str, Any], accum: _WaveformAccum) -> None:
    sid = row["stable_id"]
    bucket = _detail_ungradable_bucket(row)
    if bucket is not None:
        accum.ungradable_ids.append(sid)
        accum.ungradable[bucket] = accum.ungradable.get(bucket, 0) + 1
        return
    disposition, primary_r = _score_detail_row(row)
    if primary_r is not None and math.isfinite(primary_r):
        accum.primary_rs.append(primary_r)
    _record_disposition(sid, disposition, accum)


def _accumulate_triband_row(row: dict[str, Any], accum: _WaveformAccum) -> None:
    sid = row["stable_id"]
    bucket = _triband_ungradable_bucket(row)
    if bucket is not None:
        accum.ungradable_ids.append(sid)
        accum.ungradable[bucket] = accum.ungradable.get(bucket, 0) + 1
        return
    disposition, band_rs, mean_r = _score_triband_row(row)
    if mean_r is not None and math.isfinite(mean_r):
        accum.track_mean_rs.append(mean_r)
    for name, value in band_rs.items():
        if math.isfinite(value):
            accum.band_rs_by_name[name].append(value)
    _record_disposition(sid, disposition, accum)


def _record_disposition(sid: str, disposition: str, accum: _WaveformAccum) -> None:
    if disposition == "no_own":
        accum.no_own_ids.append(sid)
    elif disposition == "failed_own":
        accum.failed_own_n += 1
    elif disposition == "agree":
        accum.agree_ids.append(sid)
    elif disposition == "disagree":
        accum.disagree_ids.append(sid)


def score_waveform(lane: str, rows: list[dict[str, Any]], *, measured_at: str) -> LaneFigure:
    """Score one waveform lane against rekordbox ANLZ envelopes in the payload."""
    if lane not in {"waveform_preview", "waveform_detail", "waveform_triband"}:
        raise ValueError(f"not a waveform lane: {lane!r}")

    accum = _WaveformAccum()
    for row in rows:
        if lane == "waveform_preview":
            _accumulate_preview_row(row, accum)
        elif lane == "waveform_detail":
            _accumulate_detail_row(row, accum)
        else:
            _accumulate_triband_row(row, accum)

    denominator_n = len(rows) - len(accum.ungradable_ids)
    scored_n = len(accum.agree_ids) + len(accum.disagree_ids)
    band_median_r = None
    if lane == "waveform_triband":
        band_median_r = {
            name: value
            for name in BAND_NAMES
            if (value := _median(accum.band_rs_by_name[name])) is not None
        }
        median_r = _median(accum.track_mean_rs)
        mean_r = _mean(accum.track_mean_rs)
        min_r = _min(accum.track_mean_rs)
    else:
        median_r = _median(accum.primary_rs)
        mean_r = _mean(accum.primary_rs)
        min_r = _min(accum.primary_rs)

    median_r_secondary = _median(accum.secondary_rs) if accum.secondary_rs else None

    return LaneFigure(
        lane=lane,
        status="scored",
        measured_at=measured_at,
        denominator_name=DENOMINATOR_NAME[lane],
        denominator_n=denominator_n,
        scored_n=scored_n,
        exact_n=len(accum.agree_ids),
        failed_own_n=accum.failed_own_n,
        no_own_n=len(accum.no_own_ids),
        agree_ids=tuple(accum.agree_ids),
        disagree_ids=tuple(accum.disagree_ids),
        no_own_ids=tuple(accum.no_own_ids),
        ungradable_ids=tuple(accum.ungradable_ids),
        ungradable=accum.ungradable,
        median_r=median_r,
        mean_r=mean_r,
        min_r=min_r,
        band_median_r=band_median_r or None,
        median_r_secondary=median_r_secondary,
    )
