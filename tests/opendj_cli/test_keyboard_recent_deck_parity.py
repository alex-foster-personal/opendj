"""KEYB-11383 agent parity: the C, V and K chords send the same bus commands an
agent reaches with `opendj deck <n> cue|beat_sync|master_tempo` or by POSTing
the command to /api/v1/commands (test_bus_parity.py pins verb-to-bus agreement).

Regression lines:
- if `opendj deck 2 cue` does not build {"type": "cue", "deck": 2} then broken
- if beat_sync or master_tempo drop the enabled flag the keyboard sends then broken
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import parse_invocation


def test_deck_cue_builds_the_keyboard_cue_command() -> None:
    assert parse_invocation(["deck", "2", "cue"]).command == {"type": "cue", "deck": 2}


@pytest.mark.parametrize("verb", ["beat_sync", "master_tempo"])
@pytest.mark.parametrize(("token", "enabled"), [("true", True), ("false", False)])
def test_deck_toggles_build_the_keyboard_toggle_command(verb: str, token: str, enabled: bool) -> None:
    assert parse_invocation(["deck", "2", verb, token]).command == {
        "type": verb,
        "deck": 2,
        "enabled": enabled,
    }
