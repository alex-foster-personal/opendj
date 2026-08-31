"""Tests for :mod:`scripts.bench.kpi_derive`, the KPI derivation module.

Regression lines:
- if an entry without container_s is counted then the tracks total is inflated and broken
- if peak concurrency is emitted without container-stamped wall_spans then broken
- if peak is derived from publication mtimes it can exceed the container cap and is broken
- if a track duration cannot be resolved and the run does not fail then broken
- if the north-star figure stops equalling stem-minutes / wall x 10min then broken
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.bench import kpi_derive as mod




def _entry(duration_s: float, container_s: float, separate_s: float,
           load_s: float, wall_span: list[float] | None = None) -> dict:
    worker: dict = {
        "device": "cuda",
        "timings": {"load_s": load_s, "separate_s": separate_s, "container_s": container_s},
    }
    if wall_span is not None:
        worker["wall_span"] = wall_span
    return {"schema": 2, "duration_s": duration_s, "worker": worker}


def _write(cache_dir, stable_id: str, entry: dict, mtime: float) -> None:
    path = cache_dir / f"{stable_id}.json"
    path.write_text(json.dumps(entry))
    os.utime(path, (mtime, mtime))








def test_entries_without_container_s_are_excluded(tmp_path):
    _write(tmp_path, "a" * 40, _entry(120.0, 10.0, 6.0, 0.3), 1000.0)
    _write(tmp_path, "b" * 40, _entry(120.0, 10.0, 6.0, 0.3), 1010.0)
    stale = {"schema": 2, "duration_s": 120.0, "worker": {"timings": {"separate_s": 6.0}}}
    _write(tmp_path, "c" * 40, stale, 1005.0)

    derived = mod.derive_window("all", cache_dir=tmp_path)
    assert derived.tracks_measured == 2
    assert derived.container_s_total == 20.0
    # Still counted on disk: the cumulative cache total is not the measured total.
    assert derived.tracks_cached_total == 3


def test_peak_is_not_derivable_without_wall_spans(tmp_path):
    _write(tmp_path, "a" * 40, _entry(120.0, 10.0, 6.0, 0.3), 1000.0)
    _write(tmp_path, "b" * 40, _entry(120.0, 10.0, 6.0, 0.3), 1010.0)

    derived = mod.derive_window("all", cache_dir=tmp_path)
    assert derived.concurrency_basis == mod.BASIS_PUBLICATION
    assert derived.observed_peak_concurrency is None, "a guessed peak is worse than no peak"
    assert derived.observed_mean_concurrency == 2.0  # 20 container-s over a 10s window


def test_peak_is_swept_when_every_call_stamped_its_span(tmp_path):
    _write(tmp_path, "a" * 40, _entry(120.0, 10.0, 6.0, 0.3, [1000.0, 1010.0]), 1010.0)
    _write(tmp_path, "b" * 40, _entry(120.0, 10.0, 6.0, 0.3, [1005.0, 1015.0]), 1015.0)
    _write(tmp_path, "c" * 40, _entry(120.0, 10.0, 6.0, 0.3, [1020.0, 1030.0]), 1030.0)

    derived = mod.derive_window("all", cache_dir=tmp_path)
    assert derived.concurrency_basis == mod.BASIS_SPANS
    assert derived.observed_peak_concurrency == 2  # a and b overlap; c is alone


def test_a_call_ending_as_another_starts_is_not_an_overlap():
    assert mod.sweep_peak([(0.0, 10.0), (10.0, 20.0)]) == 1
    assert mod.sweep_peak([(0.0, 10.0), (9.99, 20.0)]) == 2


def test_sweep_agrees_with_the_farm_side_concurrency_module():
    """Two sweep lines exist (ledger side and farm side); drift between them is a bug."""
    from scripts.farm_concurrency import concurrency_profile

    spans = [(0.0, 10.0), (5.0, 15.0), (6.0, 7.0), (20.0, 30.0), (30.0, 40.0)]
    assert float(mod.sweep_peak(spans)) == concurrency_profile(spans)["peak"]


def test_unresolvable_duration_fails_loudly(tmp_path):
    entry = _entry(120.0, 10.0, 6.0, 0.3)
    del entry["duration_s"]
    _write(tmp_path, "d" * 40, entry, 1000.0)
    _write(tmp_path, "e" * 40, _entry(120.0, 10.0, 6.0, 0.3), 1010.0)

    with pytest.raises(SystemExit, match="duration unresolvable"):
        mod.derive_window("all", cache_dir=tmp_path)


def test_north_star_is_stem_minutes_per_ten_minutes_of_wall(tmp_path):
    # 2 tracks of 300s each = 10 stem-minutes, published over a 60s wall.
    _write(tmp_path, "a" * 40, _entry(300.0, 20.0, 12.0, 0.3), 1000.0)
    _write(tmp_path, "b" * 40, _entry(300.0, 20.0, 12.0, 0.3), 1060.0)

    derived = mod.derive_window("all", cache_dir=tmp_path)
    assert derived.stem_minutes_total == 10.0
    assert derived.stem_min_per_10min_wall == 100.0  # 10 min of audio per 1 min of wall


def test_cost_uses_the_named_l4_rate(tmp_path):
    _write(tmp_path, "a" * 40, _entry(120.0, 1800.0, 6.0, 0.3), 1000.0)
    _write(tmp_path, "b" * 40, _entry(120.0, 1800.0, 6.0, 0.3), 1060.0)

    derived = mod.derive_window("all", cache_dir=tmp_path)
    assert mod.L4_USD_PER_GPU_HOUR == 0.80
    assert derived.gpu_cost_total_usd == 0.80  # one full GPU-hour of container time
    assert derived.cost_per_track_usd == 0.40


def test_latest_window_cuts_at_a_run_gap(tmp_path):
    _write(tmp_path, "a" * 40, _entry(120.0, 10.0, 6.0, 0.3), 1000.0)
    _write(tmp_path, "b" * 40, _entry(120.0, 10.0, 6.0, 0.3), 1010.0)
    _write(tmp_path, "c" * 40, _entry(120.0, 10.0, 7.0, 0.3), 1000.0 + mod.RUN_GAP_S + 100)
    _write(tmp_path, "d" * 40, _entry(120.0, 10.0, 7.0, 0.3), 1000.0 + mod.RUN_GAP_S + 160)

    assert mod.derive_window("all", cache_dir=tmp_path).tracks_measured == 4
    latest = mod.derive_window("latest", cache_dir=tmp_path)
    assert latest.tracks_measured == 2
    assert latest.separate_p50_s == 7.0












