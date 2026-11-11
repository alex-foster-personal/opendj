"""DECKMOVE-01 move track to deck: one page function behind the CLI verb, HTTP and MCP.

[if] ``opendj deck_move 1 4`` parses [then ⛔ unless] it builds the bus command
     ``{"type": "deck_move", "from": 1, "to": 4}`` the page validator accepts.
[if] optional crossfade, mode and force are given [then ⛔ unless] they reach
     the command under the bus's own keys.
[if] the MCP server lists its tools [then ⛔ unless] ``deck_move`` is there,
     defaults to an 8-beat move, and runs a single AGENT-03 order carrying that command.
[if] a deck outside 1..4 or an unknown mode is given [then ⛔ unless] the CLI
     refuses before anything is sent.

-Claude
"""

from __future__ import annotations

import pytest

from apps.opendj_cli.mcp_deck_tools import deck_move_command, register_deck_tools
from apps.opendj_cli.verbs import InvocationError, parse_invocation
from tests.opendj_cli.parity_support import mcp_tool_command


def test_cli_verb_builds_the_bus_command() -> None:
    assert parse_invocation(["deck_move", "1", "4"]).command == {"type": "deck_move", "from": 1, "to": 4}


def test_cli_verb_carries_optional_fields() -> None:
    command = parse_invocation(["deck_move", "2", "3", "4", "copy", "true"]).command
    assert command == {"type": "deck_move", "from": 2, "to": 3, "crossfade_beats": 4.0, "mode": "copy", "force": True}


@pytest.mark.parametrize("tokens", [["deck_move", "1", "5"], ["deck_move", "1", "2", "2", "swap"]])
def test_cli_verb_refuses_bad_values(tokens: list[str]) -> None:
    with pytest.raises(InvocationError):
        parse_invocation(tokens)


def test_mcp_tool_runs_one_single_order_with_an_8_beat_default() -> None:
    assert mcp_tool_command(register_deck_tools, "deck_move", from_deck=1, to_deck=4) == {
        "type": "deck_move",
        "from": 1,
        "to": 4,
        "crossfade_beats": 8.0,
        "mode": "move",
    }


def test_force_only_sent_when_set() -> None:
    assert "force" not in deck_move_command(1, 2, 8.0, "move", False)
    assert deck_move_command(1, 2, 0.0, "copy", True)["force"] is True


def test_cli_instant_double_is_a_zero_beat_copy() -> None:
    assert parse_invocation(["deck_move", "2", "1", "0", "copy"]).command == {
        "type": "deck_move",
        "from": 2,
        "to": 1,
        "crossfade_beats": 0.0,
        "mode": "copy",
    }


def test_cli_browser_select_playlist_all_with_stable_id() -> None:
    assert parse_invocation(["browser_select_playlist", "all", "fixture:1"]).command == {
        "type": "browser_select_playlist",
        "playlist_id": "all",
        "stable_id": "fixture:1",
    }
