"""Stem cache settings: the overridable knobs (STEM-39, STEM-43).

* [if] no settings file exists [then] the documented defaults load.
* [if] the settings file holds a bad or unknown value [then] loading raises.
* [if] one switch is saved [then] the other defaults are not pinned.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.cloud import stem_cache_budget as budget
from apps.cloud.stem_cache_budget import GIB, StemCacheSettings, StemCacheSettingsError
from tests.cloudsync.stem_cache_rig import disk_usage, enforce, make_bundle


@pytest.mark.requirement("STEM-43")
def test_settings_default_when_no_file_and_round_trip_when_saved(tmp_path: Path):
    """[if] no settings file exists [then] defaults load, and a saved override reads back, [else stop]."""
    assert budget.load_settings(tmp_path) == StemCacheSettings()
    saved = StemCacheSettings(floor_gib=50.0, floor_fraction=0.2, max_cache_gib=20.0)
    budget.save_settings(tmp_path, saved)
    assert budget.load_settings(tmp_path) == saved
    assert budget.floor_bytes(100 * GIB, saved) == 50 * GIB


@pytest.mark.requirement("STEM-43")
@pytest.mark.parametrize(
    "payload",
    [
        {"floor_gib": -1},
        {"floor_fraction": 1.0},
        {"floor_fraction": -0.1},
        {"max_cache_gib": -5},
        {"enforce_interval_s": 0},
        {"auto_evict": "yes"},
        {"floor_gb": 30},
    ],
)
def test_malformed_settings_fail_loud(tmp_path: Path, payload: dict[str, object]):
    """[if] the settings file holds a bad or unknown value [then] loading raises, never falls back to defaults, [else stop]."""
    path = budget.settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StemCacheSettingsError):
        budget.load_settings(tmp_path)


@pytest.mark.requirement("STEM-43")
def test_saved_floor_override_changes_what_enforcement_does(tmp_path: Path):
    """[if] the floor is lowered in settings [then] a volume that was low is healthy and nothing is evicted, [else stop]."""
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    index = {"old": make_bundle(stems_dir, "old", atime=1_000_000)}
    budget.save_settings(data_dir, StemCacheSettings(floor_gib=1.0, floor_fraction=0.0))

    report = enforce(stems_dir, data_dir, index=index, disk=disk_usage(free=2 * GIB))

    assert report.state == "healthy"
    assert (stems_dir / "old").exists()


@pytest.mark.requirement("STEM-39")
def test_saving_one_switch_does_not_pin_the_other_defaults(tmp_path: Path):
    """[if] only auto_evict is changed and saved [then] the file holds only auto_evict, so a later default floor still applies, [else stop]."""
    budget.save_settings(tmp_path, StemCacheSettings(auto_evict=False))

    stored = json.loads(budget.settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored == {"auto_evict": False}
    loaded = budget.load_settings(tmp_path)
    assert loaded == StemCacheSettings(auto_evict=False)
    assert loaded.floor_gib == budget.DEFAULT_FLOOR_GIB
    # Control: an explicit floor IS stored and wins over the default.
    budget.save_settings(tmp_path, StemCacheSettings(floor_gib=30.0, floor_fraction=0.10))
    assert json.loads(budget.settings_path(tmp_path).read_text(encoding="utf-8")) == {
        "floor_fraction": 0.10, "floor_gib": 30.0,
    }
    assert budget.floor_bytes(460 * GIB, budget.load_settings(tmp_path)) == 46 * GIB


@pytest.mark.requirement("STEM-39")
def test_a_settings_file_written_before_the_default_changed_keeps_its_floor(tmp_path: Path):
    """[if] a machine stored the old 30 GiB / 10% floor in full [then] it keeps that floor: a stored value is the user's, [else stop]."""
    path = budget.settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"auto_evict": False, "enforce_interval_s": 300.0, "floor_fraction": 0.1,
                    "floor_gib": 30.0, "max_cache_gib": None}),
        encoding="utf-8",
    )
    assert budget.floor_bytes(460 * GIB, budget.load_settings(tmp_path)) == 46 * GIB
