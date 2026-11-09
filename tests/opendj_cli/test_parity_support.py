"""Exercise the shared three-way agent parity pytest helper."""

from __future__ import annotations

import pytest

from apps.opendj_cli.mcp_deck_tools import register_deck_tools
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations
from tests.opendj_cli.parity_support import assert_three_way_parity, mcp_tool_command

_DECK_HTTP = {"single": {"type": "deck_move", "from": 2, "to": 3, "crossfade_beats": 4.0, "mode": "move"}}


@pytest.mark.requirement("FIXT-10642")
def test_deck_move_three_way_parity() -> None:
    """[if] deck_move is wired [then] verb, MCP and HTTP emit one command, [else stop]."""
    assert_three_way_parity(
        ["deck_move", "2", "3", "4", "move"],
        "deck_move",
        {"from_deck": 2, "to_deck": 3, "crossfade_beats": 4.0, "mode": "move"},
        _DECK_HTTP,
        register_fn=register_deck_tools,
    )


def _register_read_only(server: MCPServer, *, run_order) -> None:  # noqa: ANN001
    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def sample_status() -> dict[str, object]:
        return run_order({"single": {"type": "play", "deck": 1, "playing": True}})


@pytest.mark.requirement("FIXT-10642")
def test_read_only_tool_refuses_mutating_parity_check() -> None:
    """[if] a tool is read-only [then] mutating parity is refused, [else stop]."""
    with pytest.raises(AssertionError, match="read-only"):
        mcp_tool_command(_register_read_only, "sample_status", mutating=True)


@pytest.mark.requirement("FIXT-10642")
def test_mcp_invokes_tools_with_keyword_arguments() -> None:
    """[if] MCP calls deck_move [then] from_deck reaches the bus as from, [else stop]."""
    command = mcp_tool_command(register_deck_tools, "deck_move", from_deck=1, to_deck=4)
    assert command == {"type": "deck_move", "from": 1, "to": 4, "crossfade_beats": 8.0, "mode": "move"}


def _register_incomplete_deck(server: MCPServer, *, run_order) -> None:  # noqa: ANN001
    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    def deck_move(from_deck: int, to_deck: int) -> dict[str, object]:
        return run_order({"single": {"type": "deck_move", "from": from_deck, "to": to_deck}})


@pytest.mark.requirement("FIXT-10642")
def test_parity_diff_names_the_mismatched_key() -> None:
    """[if] MCP drops one bus key [then] parity names it, [else stop]."""
    with pytest.raises(AssertionError, match="crossfade_beats"):
        assert_three_way_parity(
            ["deck_move", "1", "4"],
            "deck_move",
            {"from_deck": 1, "to_deck": 4},
            _DECK_HTTP,
            register_fn=_register_incomplete_deck,
        )
