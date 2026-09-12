"""Waveform preview, detail, and tri-band lanes vs rekordbox ANLZ ground truth."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from apps.analysis_waveform.decode import BAND_NAMES
from apps.parity.figure import LaneFigure
from apps.parity.lanes import DENOMINATOR_NAME
from apps.parity.waveform_score import (
    detail_ungradable_bucket,
    preview_ungradable_bucket,
    score_detail_row,
    score_preview_row,
    score_triband_row,
    triband_ungradable_bucket,
)

_WAVEFORM_LANES = frozenset({"waveform_preview", "waveform_detail", "waveform_triband"})


def _finite_stat(values: list[float], reducer: Callable[[list[float]], float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return None
    return float(reducer(finite))


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


def _record_disposition(sid: str, disposition: str, accum: _WaveformAccum) -> None:
    if disposition == "no_own":
        accum.no_own_ids.append(sid)
    elif disposition == "failed_own":
        accum.failed_own_n += 1
    elif disposition == "agree":
        accum.agree_ids.append(sid)
    elif disposition == "disagree":
        accum.disagree_ids.append(sid)


_LANE_UNGRADABLE: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "waveform_preview": preview_ungradable_bucket,
    "waveform_detail": detail_ungradable_bucket,
    "waveform_triband": triband_ungradable_bucket,
}


def _score_preview_into(row: dict[str, Any], accum: _WaveformAccum) -> str:
    disposition, primary_r, secondary_r = score_preview_row(row)
    if primary_r is not None and math.isfinite(primary_r):
        accum.primary_rs.append(primary_r)
    if secondary_r is not None and math.isfinite(secondary_r):
        accum.secondary_rs.append(secondary_r)
    return disposition


def _score_detail_into(row: dict[str, Any], accum: _WaveformAccum) -> str:
    disposition, primary_r = score_detail_row(row)
    if primary_r is not None and math.isfinite(primary_r):
        accum.primary_rs.append(primary_r)
    return disposition


def _score_triband_into(row: dict[str, Any], accum: _WaveformAccum) -> str:
    disposition, band_rs, mean_r = score_triband_row(row)
    if mean_r is not None and math.isfinite(mean_r):
        accum.track_mean_rs.append(mean_r)
    for name, value in band_rs.items():
        if math.isfinite(value):
            accum.band_rs_by_name[name].append(value)
    return disposition


_LANE_SCORERS: dict[str, Callable[[dict[str, Any], _WaveformAccum], str]] = {
    "waveform_preview": _score_preview_into,
    "waveform_detail": _score_detail_into,
    "waveform_triband": _score_triband_into,
}


def score_waveform(lane: str, rows: list[dict[str, Any]], *, measured_at: str) -> LaneFigure:
    """Score one waveform lane against rekordbox ANLZ envelopes in the payload."""
    if lane not in _WAVEFORM_LANES:
        raise ValueError(f"not a waveform lane: {lane!r}")

    accum = _WaveformAccum()
    bucket_fn = _LANE_UNGRADABLE[lane]
    score_fn = _LANE_SCORERS[lane]
    for row in rows:
        sid = row["stable_id"]
        bucket = bucket_fn(row)
        if bucket is not None:
            accum.ungradable_ids.append(sid)
            accum.ungradable[bucket] = accum.ungradable.get(bucket, 0) + 1
            continue
        disposition = score_fn(row, accum)
        _record_disposition(sid, disposition, accum)

    denominator_n = len(rows) - len(accum.ungradable_ids)
    scored_n = len(accum.agree_ids) + len(accum.disagree_ids)
    if lane == "waveform_triband":
        band_median_r = {
            name: value
            for name in BAND_NAMES
            if (value := _finite_stat(accum.band_rs_by_name[name], np.median)) is not None
        }
        median_r = _finite_stat(accum.track_mean_rs, np.median)
        mean_r = _finite_stat(accum.track_mean_rs, np.mean)
        min_r = _finite_stat(accum.track_mean_rs, np.min)
    else:
        band_median_r = None
        median_r = _finite_stat(accum.primary_rs, np.median)
        mean_r = _finite_stat(accum.primary_rs, np.mean)
        min_r = _finite_stat(accum.primary_rs, np.min)

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
        median_r_secondary=_finite_stat(accum.secondary_rs, np.median),
    )
