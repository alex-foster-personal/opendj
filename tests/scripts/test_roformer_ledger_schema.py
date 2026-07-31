"""Schema check for scripts/bench/roformer_ledger.json.

Cheap and deliberately narrow: it does not re-run any GPU work, it only
asserts the append-only ledger scripts/modal_roformer_spike.py writes stays
shaped the way the roformer-spike-plan promised (config_tag, per_track_sisdr,
median, wall_s, gpu_s, accepted, note on every run), so a future edit to the
writer cannot silently drop a field the plan's reporting step depends on.

Regression lines:
- if a run entry is missing one of the seven required keys then broken
- if per_track_sisdr is empty or has a non-numeric score then broken
- if median is not the median of per_track_sisdr's own values then broken
- if accepted is not a bool then broken
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest

LEDGER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bench" / "roformer_ledger.json"

REQUIRED_RUN_KEYS = {
    "config_tag",
    "per_track_sisdr",
    "median",
    "wall_s",
    "gpu_s",
    "accepted",
    "note",
}


def _load_runs() -> list[dict]:
    if not LEDGER_PATH.is_file():
        pytest.skip(f"{LEDGER_PATH} does not exist yet (no roformer run recorded)")
    data = json.loads(LEDGER_PATH.read_text())
    assert isinstance(data, dict) and "runs" in data, "ledger must be {'runs': [...]}"
    assert isinstance(data["runs"], list) and data["runs"], "ledger must have at least one run"
    return data["runs"]


def test_every_run_has_required_keys() -> None:
    for entry in _load_runs():
        missing = REQUIRED_RUN_KEYS - entry.keys()
        assert not missing, f"run {entry.get('config_tag')!r} missing keys: {missing}"


def test_per_track_sisdr_is_nonempty_numeric() -> None:
    for entry in _load_runs():
        per_track = entry["per_track_sisdr"]
        assert isinstance(per_track, dict) and per_track, (
            f"run {entry['config_tag']!r} has an empty per_track_sisdr"
        )
        for slug, score in per_track.items():
            assert isinstance(slug, str) and slug
            assert isinstance(score, (int, float)), f"{slug} score is not numeric: {score!r}"


def test_median_matches_per_track_scores() -> None:
    for entry in _load_runs():
        expected = round(statistics.median(entry["per_track_sisdr"].values()), 3)
        assert entry["median"] == pytest.approx(expected, abs=1e-6), (
            f"run {entry['config_tag']!r}: median {entry['median']} != "
            f"statistics.median(per_track_sisdr) {expected}"
        )


def test_accepted_is_bool() -> None:
    for entry in _load_runs():
        assert isinstance(entry["accepted"], bool), (
            f"run {entry['config_tag']!r}: accepted must be bool, got {type(entry['accepted'])}"
        )


def test_wall_and_gpu_seconds_are_nonnegative() -> None:
    for entry in _load_runs():
        assert entry["wall_s"] >= 0
        assert entry["gpu_s"] >= 0
