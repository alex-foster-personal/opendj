"""Bench-only mapping from S-KEY sliding-window JSON to bar log-emissions.

S-KEY's own LICENSE and README grant MIT to the code only; the checkpoint
weights ship without a license statement (see ``apps/analysis_key/skey_runner.py``
payload field). This module stays in the repo venv (no torch) and maps
per-window ``key_rekordbox_style`` labels onto the same HMM path as shipping
(:func:`segments.segment_from_log_emissions`). It is not wired into
``segment_audio`` or the own-key producer until weights are licensed and the
candidate wins the GiantSteps+ boundary-F comparison
(``(private path)`` section 5).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from apps.analysis_key import canon, profiles
from apps.analysis_key.segments import BarGrid, SegmentBlock, segment_from_log_emissions

WINDOW_S = 15.0
DEFAULT_HOP_S = 1.875
_MIN_PROB = 1e-12
_N_KEYS = profiles.N_KEYS


def window_bounds(
    duration_s: float,
    *,
    window_s: float = WINDOW_S,
    hop_s: float = DEFAULT_HOP_S,
) -> tuple[tuple[float, float], ...]:
    """Half-open ``[start, end)`` windows covering ``[0, duration_s)``.

    When ``duration_s < window_s``, the single window is ``[0, duration_s)``.
    """
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s}")
    if duration_s <= window_s:
        return ((0.0, duration_s),)
    bounds: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_s:
        end = min(start + window_s, duration_s)
        bounds.append((start, end))
        if end >= duration_s:
            break
        start += hop_s
    return tuple(bounds)


def _window_midpoint(start_s: float, end_s: float) -> float:
    return 0.5 * (start_s + end_s)


def _parse_key_index(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        key = canon.from_rekordbox_scale_name(raw)
    except ValueError:
        return None
    return profiles.index_for_key(key)


def _weights_for_bar_mid(
    parsed: list[tuple[Mapping[str, object], int]],
    mid: float,
    n_keys: int,
) -> np.ndarray:
    """Histogram weights over key indices for one bar midpoint."""
    containing: list[tuple[Mapping[str, object], int]] = []
    for row, idx in parsed:
        start_s = float(row["start_s"])
        end_s = float(row["end_s"])
        if start_s <= mid < end_s:
            containing.append((row, idx))

    if containing:
        weights = np.zeros(n_keys, dtype=np.float64)
        for _, idx in containing:
            weights[idx] += 1.0
        return weights
    if not parsed:
        return np.full(n_keys, 1.0 / n_keys, dtype=np.float64)

    best: list[tuple[Mapping[str, object], int]] = []
    best_dist = math.inf
    for row, idx in parsed:
        d = abs(
            mid - _window_midpoint(float(row["start_s"]), float(row["end_s"]))
        )
        if d < best_dist - 1e-15:
            best_dist = d
            best = [(row, idx)]
        elif abs(d - best_dist) <= 1e-15:
            best.append((row, idx))
    weights = np.zeros(n_keys, dtype=np.float64)
    for _, idx in best:
        weights[idx] += 1.0
    return weights


def windows_to_bar_log_emissions(
    windows: Sequence[Mapping[str, object]],
    grid: BarGrid,
    *,
    n_keys: int = _N_KEYS,
) -> np.ndarray:
    """Map window rows to ``(24, n_bars)`` log-probabilities per bar."""
    if n_keys != _N_KEYS:
        raise ValueError(f"n_keys must be {_N_KEYS}, got {n_keys}")
    n_bars = grid.n_bars
    parsed: list[tuple[Mapping[str, object], int]] = []
    for row in windows:
        raw = row.get("key_rekordbox_style")
        if not isinstance(raw, str):
            continue
        idx = _parse_key_index(raw)
        if idx is not None:
            parsed.append((row, idx))

    out = np.zeros((n_keys, n_bars), dtype=np.float64)
    for bar in range(n_bars):
        mid = _window_midpoint(grid.starts[bar], grid.ends[bar])
        weights = _weights_for_bar_mid(parsed, mid, n_keys)
        col = weights / (weights.sum() + _MIN_PROB)
        out[:, bar] = np.log(col + _MIN_PROB)
    return out


def segment_from_skey_windows(
    windows: Sequence[Mapping[str, object]],
    grid: BarGrid,
    *,
    duration_s: float,
) -> SegmentBlock:
    """Run the segmenter on bench S-KEY window labels (JSON shape)."""
    log_emissions = windows_to_bar_log_emissions(windows, grid)
    return segment_from_log_emissions(log_emissions, grid, duration_s=duration_s)


__all__ = [
    "DEFAULT_HOP_S",
    "WINDOW_S",
    "segment_from_skey_windows",
    "window_bounds",
    "windows_to_bar_log_emissions",
]
