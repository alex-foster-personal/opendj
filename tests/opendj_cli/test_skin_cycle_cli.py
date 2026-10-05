"""CHROME-12 agent parity: every skin the top-bar button cycles is reachable by CLI.

[if] ``opendj set_skin light ...`` is rejected [then ⛔] an agent cannot reach
     the light skin the button can.
[if] the CLI skin enum disagrees with the webui cycle order [then ⛔] one
     surface can set a skin the other cannot.
"""
from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import UI_SKIN_VALUES
from apps.opendj_cli.verbs import InvocationError, parse_invocation


@pytest.mark.requirement("CHROME-12")
@pytest.mark.parametrize("skin", ["default", "mono-dev", "light"])
def test_set_skin_reaches_every_cycled_skin(skin: str) -> None:
    """[if] set_skin <skin> builds no set_skin command [then] the cycle is UI-only, [else stop]."""
    assert parse_invocation(["set_skin", skin, "rekordbox", "auto"]).command == {
        "type": "set_skin", "ui_skin": skin, "wave_palette": "rekordbox", "wave_split_master": "auto",
    }


@pytest.mark.requirement("CHROME-12")
def test_cli_skin_values_match_the_webui_cycle_order() -> None:
    """[if] UI_SKIN_VALUES is not default, mono-dev, light [then] CLI and button disagree, [else stop]."""
    assert UI_SKIN_VALUES == ("default", "mono-dev", "light")


@pytest.mark.requirement("CHROME-12")
def test_set_skin_rejects_an_unknown_skin() -> None:
    """[if] set_skin neon parses [then] the enum is not enforced, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["set_skin", "neon", "rekordbox", "auto"])
