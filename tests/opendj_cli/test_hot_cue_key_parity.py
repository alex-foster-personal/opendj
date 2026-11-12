"""#10354: keyboard hot cues A-H map to existing CLI deck verbs."""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import SLOT_VALUES
from apps.opendj_cli.verbs import parse_invocation


@pytest.mark.requirement("KEYB-11752")
def test_hot_cue_trigger_parses_deck_scoped() -> None:
    """[if] deck 1 slot C [then] hot_cue_trigger command, [else stop]."""
    assert parse_invocation(["deck", "1", "hot_cue_trigger", "C"]).command == {
        "type": "hot_cue_trigger",
        "deck": 1,
        "slot": "C",
    }


@pytest.mark.requirement("KEYB-11752")
def test_hot_cue_save_parses_revision_and_ms() -> None:
    """[if] save slot E at 1234ms [then] hot_cue_save with revision, [else stop]."""
    assert parse_invocation(["deck", "1", "hot_cue_save", "E", "1234", "rev-1"]).command == {
        "type": "hot_cue_save",
        "deck": 1,
        "slot": "E",
        "in_ms": 1234.0,
        "revision": "rev-1",
    }


@pytest.mark.requirement("KEYB-11752")
def test_hot_cue_clear_parses_slot_and_revision() -> None:
    """[if] clear slot C [then] hot_cue_clear with revision, [else stop]."""
    assert parse_invocation(["deck", "1", "hot_cue_clear", "C", "rev-1"]).command == {
        "type": "hot_cue_clear",
        "deck": 1,
        "slot": "C",
        "revision": "rev-1",
    }


def test_cli_slot_domain_matches_digit_keys_1_through_8() -> None:
    assert tuple("ABCDEFGH") == SLOT_VALUES
