"""STEM-77 agent parity: the CLI can bind a separation to the track it means.

[if] ``opendj stem_separate <deck> <stable_id>`` is rejected [then ⛔] an agent
     racing a load cannot say which track it is paying to separate (Codex on
     #6819); the typed command already takes ``stable_id``.
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.verbs import InvocationError, parse_invocation


@pytest.mark.requirement("STEM-77")
def test_stem_separate_carries_the_named_track() -> None:
    """[if] stem_separate 1 track-a drops the id [then] the dispatcher cannot refuse a swapped deck, [else stop]."""
    assert parse_invocation(["stem_separate", "1", "track-a"]).command == {
        "type": "stem_separate",
        "deck": 1,
        "stable_id": "track-a",
    }


@pytest.mark.requirement("STEM-77")
def test_control_stem_separate_without_a_track_sends_none() -> None:
    """[if] the id stops being optional [then] existing callers break; omitted, the dispatcher binds it on receipt, [else stop]."""
    assert parse_invocation(["stem_separate", "1"]).command == {"type": "stem_separate", "deck": 1}


def test_stem_separate_rejects_an_extra_argument() -> None:
    """[if] a third argument parses [then] a typo could pass silently, [else stop]."""
    with pytest.raises(InvocationError):
        parse_invocation(["stem_separate", "1", "track-a", "extra"])
