"""TRANS-01: ``opendj state`` prints the mirror transition field.

[if] the UI mirror carries transition.state [then] the CLI prints
    ``transition: {state}``
[if] an old page omits the field [then] the CLI prints ``transition: absent``
"""

from __future__ import annotations

from apps.opendj_cli.__main__ import _state_text
from tests.opendj_cli.conftest import blank_mirror


def test_state_text_prints_transition_state() -> None:
    mirror = blank_mirror()
    mirror["transition"] = {"state": "transitioning", "incoming_deck": 2, "outgoing_deck": 1}
    text = _state_text(mirror)
    assert "transition: transitioning" in text.splitlines()


def test_state_text_prints_absent_when_mirror_has_no_transition() -> None:
    text = _state_text(blank_mirror())
    assert "transition: absent" in text.splitlines()
