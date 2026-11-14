"""Drive the four transport deck operations through the shared command bus.

Each tool is the typed OJ twin of its existing CLI verb. The CLI parser builds
the AGENT-03 order, and the same per-step result check as the generic
``command`` tool decides whether the operation succeeded.
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import COMMANDS_PATH, engine_json, require_steps_succeeded
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg
from apps.opendj_cli.orders import single
from apps.opendj_cli.verbs import InvocationError, parse_invocation


def _order(tokens: list[str]) -> dict[str, Any]:
    """Build exactly the single order produced by the CLI for ``tokens``."""
    try:
        return single(parse_invocation(tokens))
    except (InvocationError, ValueError) as exc:
        raise ToolFailure("invalid_arguments", str(exc)) from exc


async def _post(ctx: ToolContext, order: dict[str, Any]) -> dict[str, Any]:
    """Post one order and reject any page-reported step failure."""
    result = require_steps_succeeded(await engine_json(ctx, "POST", COMMANDS_PATH, order))
    return {"order": order, "steps": result["steps"]}


def _deck(args: dict[str, Any]) -> int:
    deck = int_arg(args, "deck", low=1, high=4)
    if deck is None:
        raise ToolFailure("invalid_arguments", "deck is required")
    return deck


async def _deck_load(ctx: ToolContext, args: dict[str, Any]) -> Any:
    deck = _deck(args)
    stable_id = text_arg(args, "stable_id", required=True)
    assert stable_id is not None
    return await _post(ctx, _order(["load", str(deck), stable_id]))


async def _deck_play(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await _post(ctx, _order(["play", str(_deck(args))]))


async def _deck_pause(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await _post(ctx, _order(["pause", str(_deck(args))]))


async def _deck_unload(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return await _post(ctx, _order(["unload", str(_deck(args))]))


_DECK = {"type": "integer", "minimum": 1, "maximum": 4, "description": "Deck number 1 through 4."}


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="deck_load",
        description="Load a library track onto a deck. CLI: opendj load <deck> <stable_id>; MCP: command.",
        parameters={
            "type": "object",
            "properties": {"deck": _DECK, "stable_id": {"type": "string"}},
            "required": ["deck", "stable_id"],
        },
        gate="confirm",
        run=_deck_load,
    ),
    ToolSpec(
        name="deck_play",
        description="Start a deck. CLI: opendj play <deck>; MCP: command.",
        parameters={"type": "object", "properties": {"deck": _DECK}, "required": ["deck"]},
        gate="confirm",
        run=_deck_play,
    ),
    ToolSpec(
        name="deck_pause",
        description="Pause a deck. CLI: opendj pause <deck>; MCP: command.",
        parameters={"type": "object", "properties": {"deck": _DECK}, "required": ["deck"]},
        gate="confirm",
        run=_deck_pause,
    ),
    ToolSpec(
        name="deck_unload",
        description="Unload a deck. CLI: opendj unload <deck>; MCP: command.",
        parameters={"type": "object", "properties": {"deck": _DECK}, "required": ["deck"]},
        gate="confirm",
        run=_deck_unload,
    ),
)
