"""Row-level waveform scoring helpers for PARITY-01."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from apps.analysis_waveform.bands import _downsample_max
from apps.analysis_waveform.decode import BAND_NAMES
from apps.analysis_waveform.score import POSITIVE_CONTROL_TOLERANCE, pearson

_EXACT_TOLERANCE = POSITIVE_CONTROL_TOLERANCE


def is_non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 2


def has_full_triband(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(value.get(name), list) and len(value[name]) >= 2 for name in BAND_NAMES
    )


def align_1d(ours: np.ndarray, truth: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Peak-max align two 1-D envelopes to the shorter width."""
    if ours.shape[0] == truth.shape[0]:
        return ours, truth
    if ours.shape[0] > truth.shape[0]:
        aligned = _downsample_max(ours.reshape(-1, 1), truth.shape[0]).reshape(-1)
        return aligned, truth
    aligned = _downsample_max(truth.reshape(-1, 1), ours.shape[0]).reshape(-1)
    return ours, aligned


def score_mono_pair(own: list[float], rb: list[float]) -> float:
    ours = np.asarray(own, dtype=np.float64)
    truth = np.asarray(rb, dtype=np.float64)
    ours, truth = align_1d(ours, truth)
    return pearson(ours, truth)


def is_exact(r: float) -> bool:
    return math.isfinite(r) and abs(r - 1.0) <= _EXACT_TOLERANCE


def preview_truth_tags(row: dict[str, Any]) -> tuple[list[float] | None, list[float] | None]:
    rb_pwav = row.get("rb_pwav")
    rb_pwv2 = row.get("rb_pwv2")
    primary = rb_pwav if is_non_empty_list(rb_pwav) else None
    secondary = rb_pwv2 if is_non_empty_list(rb_pwv2) else None
    if primary is None and secondary is not None:
        primary, secondary = secondary, None
    return primary, secondary


def detail_truth_tag(row: dict[str, Any]) -> list[float] | None:
    for tag_field in ("rb_pwv3", "rb_pwv4_luminance", "rb_pwv5"):
        value = row.get(tag_field)
        if is_non_empty_list(value):
            return value
    return None


def triband_truth_tags(
    row: dict[str, Any],
) -> tuple[dict[str, list[float]] | None, dict[str, list[float]] | None]:
    rb_pwv6 = row.get("rb_pwv6")
    rb_pwv7 = row.get("rb_pwv7")
    primary = rb_pwv6 if has_full_triband(rb_pwv6) else None
    secondary = rb_pwv7 if has_full_triband(rb_pwv7) else None
    if primary is None and secondary is not None:
        primary, secondary = secondary, None
    return primary, secondary


def preview_ungradable_bucket(row: dict[str, Any]) -> str | None:
    primary, secondary = preview_truth_tags(row)
    if primary is not None or secondary is not None:
        return None
    return "missing_rb_preview"


def detail_ungradable_bucket(row: dict[str, Any]) -> str | None:
    if not row.get("rb_ext_readable", True):
        return "unreadable_ext"
    if detail_truth_tag(row) is not None:
        return None
    return "missing_rb_detail"


def triband_ungradable_bucket(row: dict[str, Any]) -> str | None:
    primary, secondary = triband_truth_tags(row)
    if primary is not None or secondary is not None:
        return None
    return "missing_rb_triband"


def score_preview_row(row: dict[str, Any]) -> tuple[str, float | None, float | None]:
    own = row.get("own_preview")
    if own is None:
        return "no_own", None, None
    primary_truth, secondary_truth = preview_truth_tags(row)
    if primary_truth is None:
        return "failed_own", None, None
    primary_r = score_mono_pair(own, primary_truth)
    secondary_r: float | None = None
    tags_scored = [primary_r]
    if secondary_truth is not None:
        secondary_r = score_mono_pair(own, secondary_truth)
        tags_scored.append(secondary_r)
    if not all(math.isfinite(value) for value in tags_scored):
        return "failed_own", primary_r, secondary_r
    if all(is_exact(value) for value in tags_scored):
        return "agree", primary_r, secondary_r
    return "disagree", primary_r, secondary_r


def score_detail_row(row: dict[str, Any]) -> tuple[str, float | None]:
    own = row.get("own_detail")
    if own is None:
        return "no_own", None
    primary_truth = detail_truth_tag(row)
    if primary_truth is None:
        return "failed_own", None
    primary_r = score_mono_pair(own, primary_truth)
    if not math.isfinite(primary_r):
        return "failed_own", primary_r
    if is_exact(primary_r):
        return "agree", primary_r
    return "disagree", primary_r


def score_triband_row(
    row: dict[str, Any],
) -> tuple[str, dict[str, float], float | None]:
    own = row.get("own_triband")
    if own is None:
        return "no_own", {}, None
    if not has_full_triband(own):
        return "failed_own", {}, None
    primary_truth, _secondary = triband_truth_tags(row)
    if primary_truth is None:
        return "failed_own", {}, None
    band_rs = {name: score_mono_pair(own[name], primary_truth[name]) for name in BAND_NAMES}
    if not all(math.isfinite(value) for value in band_rs.values()):
        return "failed_own", band_rs, None
    mean_r = float(np.mean(list(band_rs.values())))
    if all(is_exact(value) for value in band_rs.values()):
        return "agree", band_rs, mean_r
    return "disagree", band_rs, mean_r
