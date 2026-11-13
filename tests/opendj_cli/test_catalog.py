"""Catalog verbs that mirror a page hotkey (issue #10263: ``section_jump``).

[if] ``opendj section_jump 1 next`` parses [then ⛔ unless] it builds the bus
     command ``{"type": "section_jump", "deck": 1, "direction": "next"}``, the
     same command Shift+] dispatches on the page.
[if] a direction outside next|previous, or a deck outside 1..4, is given
     [then ⛔ unless] the CLI refuses before anything is sent.
[if] ``opendj --list-verbs`` runs [then ⛔ unless] ``section_jump`` is listed.

-Claude
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.catalog import SECTION_JUMP_DIRECTION_VALUES, VERBS
from apps.opendj_cli.verbs import InvocationError, describe_verbs, parse_invocation


def test_section_jump_builds_the_bus_command() -> None:
    assert parse_invocation(["section_jump", "1", "next"]).command == {
        "type": "section_jump",
        "deck": 1,
        "direction": "next",
    }
    assert parse_invocation(["section_jump", "4", "previous"]).command == {
        "type": "section_jump",
        "deck": 4,
        "direction": "previous",
    }


@pytest.mark.parametrize(
    "tokens",
    [["section_jump", "1", "sideways"], ["section_jump", "5", "next"], ["section_jump", "1"]],
)
def test_section_jump_refuses_bad_values(tokens: list[str]) -> None:
    with pytest.raises(InvocationError):
        parse_invocation(tokens)


def test_section_jump_is_listed_with_both_directions() -> None:
    assert SECTION_JUMP_DIRECTION_VALUES == ("next", "previous")
    assert VERBS["section_jump"].command_type == "section_jump"
    assert any(row["verb"] == "section_jump" for row in describe_verbs()), "--list-verbs omits section_jump"
