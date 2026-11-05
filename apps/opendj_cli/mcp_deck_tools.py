"""MCP deck tools: ``deck_move`` (DECKMOVE-01 move track to deck).

Split out of :mod:`apps.opendj_cli.mcp_server` (600-line ceiling). The tool
builds one AGENT-03 ``deck_move`` command and runs it through the server's own
``command`` tool, so MCP, HTTP (``POST /api/v1/commands``), ``opendj deck_move``
and the deck artwork menu reach the same page function (``moveDeck`` in
``apps/webui/frontend/src/lib/rb/deck-move.ts``).

The arguments are ``from_deck`` / ``to_deck`` rather than the bus's ``from`` /
``to``: ``from`` is a Python keyword, and the MCP SDK publishes a pydantic
alias in the schema but then calls the function with the alias as a keyword,
which fails (checked against the pinned mcp 2.x on Thu 5 Nov 2026).

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations

_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
DEFAULT_CROSSFADE_BEATS = 8.0


def deck_move_command(
    from_deck: int, to_deck: int, crossfade_beats: float, mode: str, force: bool
) -> dict[str, Any]:
    """The bus command, exactly as the page validator takes it."""
    command: dict[str, Any] = {
        "type": "deck_move",
        "from": from_deck,
        "to": to_deck,
        "crossfade_beats": crossfade_beats,
        "mode": mode,
    }
    if force:
        command["force"] = True
    return command


def register_deck_tools(server: MCPServer, *, run_order: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """Register ``deck_move`` on ``server``; ``run_order`` is the server's ``command`` path."""

    @server.tool(annotations=_DESTRUCTIVE)
    def deck_move(
        from_deck: int,
        to_deck: int,
        crossfade_beats: float = DEFAULT_CROSSFADE_BEATS,
        mode: Literal["move", "copy"] = "move",
        force: bool = False,
    ) -> dict[str, Any]:
        """Move deck ``from_deck``'s track onto deck ``to_deck`` (rekordbox/Serato "instant doubles", extended).

        Clones the track, the playhead (started on the audio clock at the
        source's exact position, phase-locked), the live loop (identical
        in/out, engaged), tempo, pitch range, key lock, key shift and KEY SYNC,
        quantize, sync settings, slip, TRIM, EQ, FILTER and COLOR FX, stem EQ
        mode and stem gains / mutes / solos. ``move`` then crossfades
        ``crossfade_beats`` (default 8; 0 = cut) from the source channel to the
        target (the target takes the source's crossfader side and fader level)
        and unloads the source; ``copy`` leaves the source playing. Refused on
        the same deck, an empty source, a playing target unless ``force``, and
        on the Rust (native) engine.
        """
        return run_order({"single": deck_move_command(from_deck, to_deck, crossfade_beats, mode, force)})


__all__ = ["DEFAULT_CROSSFADE_BEATS", "deck_move_command", "register_deck_tools"]
