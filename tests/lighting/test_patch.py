"""LIGHT-9887: the lighting patch places fixtures on universes with address checks, groups and stage positions.

[if] two RGB pars overlap at address 1 and 3 on universe 1 [then] validation fails naming both, [else stop].
[if] a 14-channel mover starts at 500 [then] validation fails (runs past 512), [else stop].
[if] a patch is saved and reloaded [then] it is equal to the original, [else stop].
[if] the active patch is switched [then] the previous one is still on disk unchanged, [else stop].
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from apps.lighting.fixtures import FixtureProfile, parse_profile
from apps.lighting.patch import (
    Patch,
    PatchedFixture,
    PatchError,
    StagePosition,
    VenueStore,
    default_patch_path,
    load_store,
    save_store,
    switch_active,
    validate_patch,
    validate_store,
)
from apps.shared import paths

pytestmark = [pytest.mark.requirement("LIGHT-9887")]

PAR = parse_profile(
    {
        "id": "test-rgb-par-3",
        "name": "Test RGB Par",
        "kind": "par",
        "modes": [{"name": "3ch", "channels": [{"capability": c} for c in ("red", "green", "blue")]}],
    }
)
MOVER = parse_profile(
    {
        "id": "test-mover-14",
        "name": "Test Mover",
        "kind": "moving_head",
        "modes": [
            {
                "name": "14ch",
                "channels": [
                    {"capability": "pan"},
                    {"capability": "pan_fine", "fine_of": 0},
                    {"capability": "tilt"},
                    {"capability": "tilt_fine", "fine_of": 2},
                    *(
                        {"capability": c}
                        for c in (
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
                        )
                    ),
                ],
            }
        ],
    }
)
PROFILES: dict[str, FixtureProfile] = {PAR.id: PAR, MOVER.id: MOVER}


def _par(fixture_id: str, address: int, universe: int = 1, groups: tuple[str, ...] = ("pars",)) -> PatchedFixture:
    return PatchedFixture(fixture_id, PAR.id, "3ch", universe, address, groups, StagePosition(0.5, 0.5))


def _mover(fixture_id: str, address: int) -> PatchedFixture:
    return PatchedFixture(fixture_id, MOVER.id, "14ch", 1, address, ("movers",), StagePosition(0.125, 0.7))


def _store() -> VenueStore:
    club = Patch(
        "club",
        (
            _par("par-left-1", 1, groups=("left", "pars")),
            _par("par-right-1", 4, groups=("right", "pars")),
            _mover("mover-1", 20),
        ),
    )
    home = Patch("home", (_par("par-1", 1),))
    return VenueStore("club", (club, home))


# ----- acceptance ---------------------------------------------------------


def test_overlapping_pars_on_one_universe_fail_naming_both() -> None:
    with pytest.raises(PatchError) as info:
        validate_patch(Patch("club", (_par("par-a", 1), _par("par-b", 3))), PROFILES)
    assert len(info.value.errors) == 1
    assert "'par-a'" in info.value.errors[0] and "'par-b'" in info.value.errors[0]


def test_mover_starting_at_500_runs_past_512() -> None:
    with pytest.raises(PatchError) as info:
        validate_patch(Patch("club", (_mover("mover-1", 500),)), PROFILES)
    assert "'mover-1'" in str(info.value) and "513" in str(info.value) and "512" in str(info.value)
    validate_patch(Patch("club", (_mover("mover-1", 499),)), PROFILES)


def test_saved_patch_reloads_equal(tmp_path: Path) -> None:
    path = tmp_path / "lighting" / "patch.json"
    store = _store()
    save_store(path, store, PROFILES)
    assert load_store(path) == store


def test_switching_active_leaves_previous_patch_unchanged_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "lighting" / "patch.json"
    store = _store()
    save_store(path, store, PROFILES)
    club_json = json.loads(path.read_text())["patches"][0]
    switch_active(path, "home", PROFILES)
    reloaded = load_store(path)
    assert reloaded.active == "home"
    assert reloaded.patch("club") == store.patch("club")
    assert json.loads(path.read_text())["patches"][0] == club_json


# ----- validation surface -------------------------------------------------


def test_same_address_on_different_universes_and_adjacent_pars_validate() -> None:
    validate_patch(Patch("p", (_par("a", 1, universe=1), _par("b", 1, universe=2), _par("c", 4))), PROFILES)


def test_save_refuses_overlap_and_leaves_nothing_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "patch.json"
    with pytest.raises(PatchError):
        save_store(path, VenueStore("p", (Patch("p", (_par("a", 1), _par("b", 2))),)), PROFILES)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("store", "needle"),
    [
        (VenueStore("p", (Patch("p", (_par("a", 0),)),)), "address"),
        (VenueStore("p", (Patch("p", (_par("a", 513),)),)), "address"),
        (VenueStore("p", (Patch("p", (_par("a", 1, universe=0),)),)), "universe"),
        (VenueStore("p", (Patch("p", (replace(_par("a", 1), position=StagePosition(1.5, 0)),)),)), "position.x"),
        (VenueStore("p", (Patch("p", (_par("a", 1), _par("a", 10))),)), "duplicate id"),
        (VenueStore("p", (Patch("p", (replace(_par("a", 1), profile_id="nope"),)),)), "unknown profile"),
        (VenueStore("p", (Patch("p", (replace(_par("a", 1), mode="9ch"),)),)), "no mode"),
        (VenueStore("q", (Patch("p", ()),)), "active"),
    ],
)
def test_bad_fields_are_named(store: VenueStore, needle: str) -> None:
    with pytest.raises(PatchError) as info:
        validate_store(store, PROFILES)
    assert needle in str(info.value)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda d: d["patches"][0]["fixtures"][0].update(colour="red"), "$.patches[0].fixtures[0].colour"),
        (lambda d: d.update(version=2), "$.version"),
        (lambda d: d["patches"][0]["fixtures"][0].update(address="1"), "$.patches[0].fixtures[0].address"),
    ],
)
def test_load_rejects_bad_json_naming_the_path(tmp_path: Path, mutate, needle: str) -> None:
    path = tmp_path / "patch.json"
    save_store(path, _store(), PROFILES)
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    with pytest.raises(PatchError) as info:
        load_store(path)
    assert needle in str(info.value)


def test_load_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_store(tmp_path / "patch.json")


def test_default_patch_path_is_under_data_dir() -> None:
    assert default_patch_path() == paths.DATA_DIR / "lighting" / "patch.json"


def test_groups_select_fixtures() -> None:
    club = _store().patch("club")
    assert [f.id for f in club.group("pars")] == ["par-left-1", "par-right-1"]
    assert club.fixture("mover-1").address == 20
