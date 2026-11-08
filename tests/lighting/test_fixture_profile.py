"""LIGHT-9980: Open DJ fixture profiles load, validate and name every error by JSON path.

[if] a valid 7-channel RGB par profile is loaded [then] offsets 0..6 carry intensity, red, green, blue, strobe, macro, generic, [else stop].
[if] pan_fine names no coarse channel [then] loading fails naming the channel index, [else stop].
[if] two ranges overlap on one channel [then] loading fails naming both ranges, [else stop].
[if] an unknown capability appears [then] loading fails listing the allowed capabilities, [else stop].
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from apps.lighting.fixtures import CAPABILITIES, FixtureProfile, ProfileError, load_profile
from apps.lighting.fixtures import profile as profile_module

pytestmark = [pytest.mark.requirement("LIGHT-9980")]

SCHEMA = Path(profile_module.__file__).with_name("profile.schema.json")

RGB_PAR_7: dict[str, Any] = {
    "id": "test-rgb-par-7",
    "name": "Test RGB Par",
    "kind": "par",
    "modes": [
        {
            "name": "7ch",
            "channels": [
                {"capability": "intensity"},
                {"capability": "red"},
                {"capability": "green"},
                {"capability": "blue"},
                {
                    "capability": "strobe",
                    "ranges": [
                        {"from": 0, "to": 9, "meaning": "off"},
                        {"from": 10, "to": 255, "meaning": "slow..fast"},
                    ],
                },
                {"capability": "macro"},
                {"capability": "generic"},
            ],
        }
    ],
}

MOVER_16BIT: dict[str, Any] = {
    "id": "test-mover",
    "name": "Test Mover",
    "kind": "moving_head",
    "modes": [
        {
            "name": "5ch",
            "channels": [
                {"capability": "pan"},
                {"capability": "pan_fine", "fine_of": 0},
                {"capability": "tilt"},
                {"capability": "tilt_fine", "fine_of": 2},
                {"capability": "pt_speed"},
            ],
        }
    ],
}


def _write(tmp_path: Path, data: object, name: str = "profile.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _errors_of(tmp_path: Path, data: object) -> list[str]:
    with pytest.raises(ProfileError) as caught:
        load_profile(_write(tmp_path, data))
    return list(caught.value.errors)


def test_a_valid_rgb_par_exposes_its_seven_channels_by_offset(tmp_path: Path) -> None:
    """[if] the 7-channel par loads [then] its offsets 0..6 carry the expected capabilities, [else stop]."""
    profile = load_profile(_write(tmp_path, RGB_PAR_7))
    assert isinstance(profile, FixtureProfile)
    assert (profile.id, profile.name, profile.kind) == ("test-rgb-par-7", "Test RGB Par", "par")
    mode = profile.mode("7ch")
    assert mode.capabilities == ("intensity", "red", "green", "blue", "strobe", "macro", "generic")
    strobe = mode.channels[4]
    assert [(r.start, r.end, r.meaning) for r in strobe.ranges] == [(0, 9, "off"), (10, 255, "slow..fast")]


def test_a_16_bit_pan_tilt_pair_loads() -> None:
    mode = profile_module.parse_profile(MOVER_16BIT).modes[0]
    assert [c.fine_of for c in mode.channels] == [None, 0, None, 2, None]


def test_pan_fine_without_a_coarse_channel_names_the_channel_index(tmp_path: Path) -> None:
    """[if] pan_fine has no fine_of [then] the error names its channel index, [else stop]."""
    data = copy.deepcopy(MOVER_16BIT)
    del data["modes"][0]["channels"][1]["fine_of"]
    errors = _errors_of(tmp_path, data)
    assert errors == ["$.modes[0].channels[1].fine_of: channel 1 is pan_fine but names no coarse pan channel"]


def test_pan_fine_pointing_at_a_non_pan_channel_names_both(tmp_path: Path) -> None:
    data = copy.deepcopy(MOVER_16BIT)
    data["modes"][0]["channels"][1]["fine_of"] = 2
    errors = _errors_of(tmp_path, data)
    assert errors == [
        "$.modes[0].channels[1].fine_of: channel 1 is pan_fine but channel 2 is tilt, not pan",
        "$.modes[0].channels[3].fine_of: channels 1 and 3 both refine channel 2",
    ]


@pytest.mark.parametrize("target", [1, 9, -1])
def test_fine_of_must_name_another_channel_of_the_mode(tmp_path: Path, target: int) -> None:
    data = copy.deepcopy(MOVER_16BIT)
    data["modes"][0]["channels"][1]["fine_of"] = target
    assert _errors_of(tmp_path, data) == [
        f"$.modes[0].channels[1].fine_of: channel 1 fine_of {target} is not another channel of this mode"
    ]


def test_overlapping_ranges_name_both_ranges(tmp_path: Path) -> None:
    """[if] two ranges on one channel overlap [then] the error names both, [else stop]."""
    data = copy.deepcopy(RGB_PAR_7)
    data["modes"][0]["channels"][4]["ranges"] = [
        {"from": 0, "to": 20, "meaning": "off"},
        {"from": 10, "to": 255, "meaning": "slow..fast"},
    ]
    assert _errors_of(tmp_path, data) == ["$.modes[0].channels[4].ranges: ranges[0] 0..20 overlaps ranges[1] 10..255"]


def test_touching_ranges_do_not_overlap() -> None:
    data = copy.deepcopy(RGB_PAR_7)
    data["modes"][0]["channels"][4]["ranges"] = [
        {"from": 0, "to": 9, "meaning": "off"},
        {"from": 10, "to": 10, "meaning": "slowest"},
    ]
    profile_module.parse_profile(data)


def test_an_unknown_capability_lists_the_allowed_ones(tmp_path: Path) -> None:
    """[if] a capability is unknown [then] the error lists every allowed capability, [else stop]."""
    data = copy.deepcopy(RGB_PAR_7)
    data["modes"][0]["channels"][2]["capability"] = "lime"
    errors = _errors_of(tmp_path, data)
    assert len(errors) == 1
    assert errors[0].startswith("$.modes[0].channels[2].capability: unknown capability 'lime'; allowed: ")
    assert errors[0].endswith(", ".join(CAPABILITIES))


def test_every_problem_is_reported_at_once_by_json_path(tmp_path: Path) -> None:
    data: dict[str, Any] = {
        "id": "",
        "name": "x",
        "kind": "par",
        "colour": "red",
        "modes": [
            {"name": "a", "channels": [{"capability": "red", "ranges": [{"from": 300, "to": 2, "meaning": "x"}]}]},
            {"name": "a", "channels": [{"capability": "blue"}]},
        ],
    }
    errors = _errors_of(tmp_path, data)
    assert errors == [
        "$.colour: unknown key; allowed: ['id', 'kind', 'modes', 'name']",
        "$.id: must be a non-empty string",
        "$.modes[0].channels[0].ranges[0].from: must be an integer 0..255, got 300",
        "$.modes[1].name: duplicate mode name 'a'",
    ]


def test_invalid_json_is_a_profile_error_naming_the_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ProfileError, match=r"broken\.json"):
        load_profile(path)


def test_the_json_schema_documents_the_same_capabilities_and_keys() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert tuple(schema["$defs"]["channel"]["properties"]["capability"]["enum"]) == CAPABILITIES
    assert set(schema["properties"]) == set(profile_module._PROFILE_KEYS)
    assert set(schema["$defs"]["mode"]["properties"]) == set(profile_module._MODE_KEYS)
    assert set(schema["$defs"]["channel"]["properties"]) == set(profile_module._CHANNEL_KEYS)
    assert set(schema["$defs"]["range"]["properties"]) == set(profile_module._RANGE_KEYS)
