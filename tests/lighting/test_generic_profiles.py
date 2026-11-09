"""LIGHT-9886: built-in generic fixture profiles all load and stay generic.

[if] every JSON file in apps/lighting/fixtures/generic/ is loaded [then] all validate, [else stop].
[if] the moving head profile is loaded [then] pan_fine and tilt_fine pair with pan and tilt, [else stop].
[if] any profile id is duplicated [then] the test fails naming it, [else stop].
[if] a profile names a manufacturer [then] the test fails (generic only), [else stop].
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import apps.lighting.fixtures as fixtures_package
from apps.lighting.fixtures import FixtureProfile, ProfileError, load_profile, parse_profile

pytestmark = [pytest.mark.requirement("LIGHT-9886")]

GENERIC_DIR = Path(fixtures_package.__file__).with_name("generic")

EXPECTED_LAYOUTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "generic-dimmer": ("dimmer", ("intensity",)),
    "generic-rgb": ("rgb", ("red", "green", "blue")),
    "generic-rgbw": ("rgbw", ("red", "green", "blue", "white")),
    "generic-par-7": ("par", ("intensity", "red", "green", "blue", "strobe", "macro", "generic")),
    "generic-strobe-2": ("strobe", ("intensity", "strobe")),
    "generic-bar-8px-rgb": ("bar", ("red", "green", "blue") * 8),
    "generic-moving-head-14": (
        "moving_head",
        (
            "pan",
            "pan_fine",
            "tilt",
            "tilt_fine",
            "pt_speed",
            "intensity",
            "shutter",
            "red",
            "green",
            "blue",
            "white",
            "color_wheel",
            "gobo",
            "macro",
        ),
    ),
}


# ----- helpers -------------------------------------------------------------


def _profile_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.json"))


def _load_unique(folder: Path) -> dict[str, FixtureProfile]:
    profiles: dict[str, FixtureProfile] = {}
    sources: dict[str, Path] = {}
    for path in _profile_files(folder):
        profile = load_profile(path)
        if profile.id in profiles:
            raise AssertionError(f"duplicate profile id {profile.id!r} in {sources[profile.id].name} and {path.name}")
        profiles[profile.id] = profile
        sources[profile.id] = path
    return profiles


def _generic_problems(profile: FixtureProfile) -> list[str]:
    problems: list[str] = []
    if not profile.id.startswith("generic-"):
        problems.append(f"id {profile.id!r} must start with 'generic-'")
    if not profile.name.startswith("Generic "):
        problems.append(f"name {profile.name!r} must start with 'Generic '")
    return problems


def _raw_rgb() -> dict[str, Any]:
    return json.loads((GENERIC_DIR / "generic-rgb.json").read_text(encoding="utf-8"))


# ----- tests ---------------------------------------------------------------


def test_generic_folder_holds_exactly_the_seven_profiles() -> None:
    assert {path.stem for path in _profile_files(GENERIC_DIR)} == set(EXPECTED_LAYOUTS)


@pytest.mark.parametrize("path", _profile_files(GENERIC_DIR), ids=lambda p: p.stem)
def test_every_generic_profile_validates(path: Path) -> None:
    assert load_profile(path).id == path.stem


def test_each_generic_profile_has_its_documented_layout() -> None:
    for profile_id, profile in _load_unique(GENERIC_DIR).items():
        assert len(profile.modes) == 1, profile_id
        mode = profile.modes[0]
        assert (profile.kind, mode.capabilities) == EXPECTED_LAYOUTS[profile_id]
        assert mode.name == f"{len(mode.channels)}ch"


def test_moving_head_fine_channels_pair_with_pan_and_tilt() -> None:
    channels = load_profile(GENERIC_DIR / "generic-moving-head-14.json").modes[0].channels
    assert channels[0].capability == "pan"
    assert (channels[1].capability, channels[1].fine_of) == ("pan_fine", 0)
    assert channels[2].capability == "tilt"
    assert (channels[3].capability, channels[3].fine_of) == ("tilt_fine", 2)
    assert [i for i, ch in enumerate(channels) if ch.fine_of is not None] == [1, 3]


def test_generic_profile_ids_are_unique() -> None:
    assert len(_load_unique(GENERIC_DIR)) == len(EXPECTED_LAYOUTS)


def test_a_duplicated_id_fails_naming_it(tmp_path: Path) -> None:
    for name in ("a.json", "b.json"):
        shutil.copy(GENERIC_DIR / "generic-rgb.json", tmp_path / name)
    with pytest.raises(AssertionError, match=r"duplicate profile id 'generic-rgb' in a\.json and b\.json"):
        _load_unique(tmp_path)


def test_every_generic_profile_names_no_manufacturer() -> None:
    for profile in _load_unique(GENERIC_DIR).values():
        assert _generic_problems(profile) == []


def test_a_manufacturer_named_profile_fails() -> None:
    with pytest.raises(ProfileError) as excinfo:
        parse_profile({**_raw_rgb(), "manufacturer": "Acme"})
    assert any(error.startswith("$.manufacturer:") for error in excinfo.value.errors)

    problems = _generic_problems(parse_profile({**_raw_rgb(), "id": "acme-rgb-3", "name": "Acme RGB"}))
    assert len(problems) == 2
    assert problems[0].startswith("id ") and problems[1].startswith("name ")
