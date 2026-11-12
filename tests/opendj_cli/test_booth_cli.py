"""CUEOUT-12100: the booth_* bus commands each have an opendj verb.

[if] booth_level 0.5 does not build {type: booth_level, value: 0.5} [then] agents cannot set booth gain, [else stop].
[if] booth_signal_mode builds a mode the wire guard rejects [then] the CLI and bus disagree, [else stop].
[if] booth_select <id> does not carry device_id [then] agents cannot pin the booth device, [else stop].
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import BOOTH_SIGNAL_MODE_VALUES
from apps.opendj_cli.verbs import InvocationError, parse_invocation

pytestmark = pytest.mark.requirement("CUEOUT-12100")


def test_booth_level_builds_the_bus_command() -> None:
    """[if] booth_level 0.5 builds another payload [then] the gain is lost, [else stop]."""
    assert parse_invocation(["booth_level", "0.5"]).command == {"type": "booth_level", "value": 0.5}


@pytest.mark.parametrize("raw", ["1.5", "-0.1", "loud"])
def test_booth_level_refuses_a_value_outside_zero_to_one(raw: str) -> None:
    """[if] booth_level <raw> parses [then] the unit range is not enforced, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["booth_level", raw])


def test_booth_signal_mode_values_match_the_wire_guard() -> None:
    """[if] the CLI enum drifts from assertBoothSignalMode [then] a verb builds a rejected command, [else stop]."""
    assert BOOTH_SIGNAL_MODE_VALUES == ("off", "follows_master", "independent")


@pytest.mark.parametrize("mode", list(BOOTH_SIGNAL_MODE_VALUES))
def test_booth_signal_mode_builds_the_bus_command(mode: str) -> None:
    """[if] booth_signal_mode <mode> builds another payload [then] the mode is unreachable, [else stop]."""
    assert parse_invocation(["booth_signal_mode", mode]).command == {"type": "booth_signal_mode", "mode": mode}


def test_booth_signal_mode_refuses_an_unknown_mode() -> None:
    """[if] booth_signal_mode loud parses [then] the enum is not enforced, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["booth_signal_mode", "loud"])


def test_booth_select_builds_the_bus_command() -> None:
    """[if] booth_select usb-2 drops the id [then] the booth device cannot be pinned, [else stop]."""
    assert parse_invocation(["booth_select", "usb-2"]).command == {"type": "booth_select", "device_id": "usb-2"}


def test_booth_select_refuses_an_empty_id() -> None:
    """[if] booth_select '' parses [then] it differs from headphone_booth_select, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["booth_select", ""])
