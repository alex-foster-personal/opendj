"""Open Fixture Library JSON converts into an Open DJ fixture profile (#14518).

[if] a synthetic OFL RGBW par is imported [then] parse_profile accepts it and fine pairs and ranges map, unknown types raise, [else stop].
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from apps.lighting.fixtures import ProfileError, ValueRange, parse_profile
from apps.lighting.ofl_import import convert_ofl, import_ofl

pytestmark = [pytest.mark.requirement("LIGHT-14609")]

FIXTURE = Path(__file__).parent / "fixtures" / "ofl_generic_rgbw_par.json"


def _ofl() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_rgbw_par_converts_to_valid_profile() -> None:
    """[if] the synthetic OFL RGBW par is imported [then] parse_profile accepts it with both modes, [else stop]."""
    profile = import_ofl(FIXTURE)
    assert parse_profile(convert_ofl(_ofl())) == profile
    assert profile.id == "test-rgbw-par"
    assert profile.kind == "par"
    assert profile.mode("6ch").capabilities == ("intensity", "red", "green", "blue", "white", "strobe")


def test_fine_channel_becomes_coarse_fine_pair() -> None:
    """[if] a mode lists a fineChannelAliases entry [then] it becomes a fine_of pair on the coarse offset, [else stop]."""
    mode = import_ofl(FIXTURE).mode("7ch")
    assert mode.channels[1].capability == "intensity"
    assert mode.channels[1].fine_of == 0
    assert mode.channels[0].fine_of is None
    data = _ofl()
    data["availableChannels"]["Pan"] = {"fineChannelAliases": ["Pan fine"], "capability": {"type": "Pan"}}
    data["modes"][0]["channels"] += ["Pan", "Pan fine"]
    pan = parse_profile(convert_ofl(data)).mode("6ch").channels
    assert (pan[7].capability, pan[7].fine_of) == ("pan_fine", 6)


def test_capabilities_map_onto_value_ranges() -> None:
    """[if] a channel has an OFL capabilities list [then] each dmxRange becomes a profile value range, [else stop]."""
    strobe = import_ofl(FIXTURE).mode("6ch").channels[5]
    assert strobe.capability == "strobe"
    assert strobe.ranges == (
        ValueRange(0, 9, "NoFunction"),
        ValueRange(10, 249, "ShutterStrobe Strobe"),
        ValueRange(250, 255, "ShutterStrobe Open"),
    )


def test_unknown_capability_type_raises_with_path() -> None:
    """[if] an OFL capability type has no mapping [then] ProfileError names its JSON path, nothing dropped, [else stop]."""
    data = copy.deepcopy(_ofl())
    data["availableChannels"]["Strobe"]["capabilities"][1]["type"] = "Prism"
    with pytest.raises(ProfileError) as info:
        convert_ofl(data)
    assert any(
        e.startswith("$.availableChannels.Strobe.capabilities[1].type:") and "Prism" in e for e in info.value.errors
    )
