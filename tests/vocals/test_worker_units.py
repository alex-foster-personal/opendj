"""Unit tests for the demucs worker's PURE region maths (no torch needed).

The worker is a PEP 723 standalone script (torch/demucs live only in its
uv-managed env, never the repo venv), so it is imported here by PATH and
the import itself must not pull torch - that split is a tested contract.

Regression one-liners:
  - if importing vocal_region_worker pulls torch into the repo venv then broken
  - if hysteresis doesn't enter at >=0.10 and exit at <0.05 then broken
  - if regions separated by < 1.5 s aren't merged then broken
  - if regions shorter than 1.0 s survive then broken
  - if confidence isn't clip(2.5 x max ratio in region, 1.0) then broken
  - if a silent mix frame doesn't yield ratio 0 (never a divide) then broken
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("CAT-05")

_WORKER_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "vocal_region_worker.py"
)


def _import_worker():
    spec = importlib.util.spec_from_file_location(
        "vocal_region_worker", _WORKER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = _import_worker()


def test_import_is_torch_free() -> None:
    assert "torch" not in sys.modules, (
        "importing the worker module must not require torch - the pure "
        "region maths is the unit-testable half of the PEP 723 split"
    )
    assert "demucs" not in sys.modules


# ----- envelope_to_regions (verbatim SPIKE-B2 params) ----------------------------

def _env(values: list[float], hop_s: float = 0.5) -> list[tuple[float, float]]:
    return [(i * hop_s, v) for i, v in enumerate(values)]


def test_hysteresis_enters_at_on_and_exits_below_off() -> None:
    # 0.09 < on(0.10) never enters; 0.10 enters; stays in at 0.06 (>= off
    # 0.05 keeps the region open); exits at 0.04.
    values = [0.09, 0.10, 0.06, 0.06, 0.06, 0.04, 0.0, 0.0]
    regions = worker.envelope_to_regions(
        _env(values), on=worker.ON_RATIO, off=worker.OFF_RATIO
    )
    assert regions == [(0.5, 2.5)], regions


def test_sub_merge_gap_runs_fuse() -> None:
    # two runs separated by 1.0 s (< 1.5 s merge gap) fuse into one
    values = [0.2] * 4 + [0.0] * 2 + [0.2] * 4
    regions = worker.envelope_to_regions(
        _env(values), on=worker.ON_RATIO, off=worker.OFF_RATIO
    )
    assert len(regions) == 1
    assert regions[0] == (0.0, 4.5)


def test_super_merge_gap_runs_stay_apart() -> None:
    values = [0.2] * 4 + [0.0] * 4 + [0.2] * 4
    regions = worker.envelope_to_regions(
        _env(values), on=worker.ON_RATIO, off=worker.OFF_RATIO
    )
    assert len(regions) == 2


def test_short_regions_dropped() -> None:
    # a single 0.5 s hop above threshold is < 1.0 s min region
    values = [0.0, 0.2, 0.0, 0.0, 0.0, 0.0]
    assert worker.envelope_to_regions(
        _env(values), on=worker.ON_RATIO, off=worker.OFF_RATIO
    ) == []


def test_open_region_closes_at_envelope_end() -> None:
    values = [0.0, 0.2, 0.2, 0.2, 0.2]
    regions = worker.envelope_to_regions(
        _env(values), on=worker.ON_RATIO, off=worker.OFF_RATIO
    )
    assert regions == [(0.5, 2.0)]


# ----- ratio_envelope --------------------------------------------------------------

def test_ratio_envelope_silent_mix_is_zero_not_divide() -> None:
    ratio = worker.ratio_envelope([0.5, 0.5], [0.0, 1.0], hop_s=0.5)
    assert ratio == [(0.0, 0.0), (0.5, 0.5)]


def test_ratio_envelope_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        worker.ratio_envelope([0.1], [0.1, 0.2], hop_s=0.5)


# ----- region_confidence -------------------------------------------------------------

def test_confidence_is_gain_times_max_ratio_clipped() -> None:
    env = _env([0.2, 0.3, 0.1, 0.0])
    assert worker.region_confidence(env, 0.0, 1.5, 0.5) == pytest.approx(0.75)
    # 2.5 * 0.5 = 1.25 -> clipped to 1.0
    env_hot = _env([0.5, 0.5, 0.5])
    assert worker.region_confidence(env_hot, 0.0, 1.5, 0.5) == 1.0


def test_confidence_samples_at_least_first_hop() -> None:
    env = _env([0.2, 0.9])
    # zero-length region still samples index 0 (PoC max(int+1, ...) rule)
    assert worker.region_confidence(env, 0.0, 0.0, 0.5) == pytest.approx(0.5)


# ----- regions_payload ----------------------------------------------------------------

def test_regions_payload_coverage_and_fps() -> None:
    values = [0.2] * 8 + [0.0] * 8  # vocal until the exit frame at t=4.0
    payload = worker.regions_payload(_env(values), duration_s=8.0)
    assert payload["fps"] == 2.0
    assert len(payload["regions"]) == 1
    region = payload["regions"][0]
    assert region["start_s"] == 0.0
    assert region["end_s"] == 4.0
    assert region["confidence"] == pytest.approx(0.5)
    assert payload["coverage_pct"] == pytest.approx(50.0, abs=0.1)


def test_regions_payload_rejects_nonpositive_duration() -> None:
    with pytest.raises(ValueError, match="duration"):
        worker.regions_payload(_env([0.0]), duration_s=0.0)
