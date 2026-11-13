"""Mix a deck's loaded stems: mute, solo and per-stem level.

Each tool is one existing bus verb (``stem_mute``, ``stem_solo``,
``stem_gain`` in ``apps/opendj_cli/catalog.py``), built into an AGENT-03
single order by the CLI's own parser and posted to ``POST /api/v1/commands``.
So ``opendj stem_mute 1 vocal true``, MCP ``command`` and OJ ``stem_mute``
send the identical order ("drop the vocals on deck 1"). Decks and stems are
checked here against ``DECK_VALUES`` and ``STEM_VALUES`` before any request;
the parser refuses a level outside 0..1 (``invalid_arguments``). Generating
stems is ``stems.py``'s job; these tools only mix stems a deck already holds.
The answer is judged whole by ``core_tools.require_steps_succeeded``, then cut
to the order and its steps.
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolRun, ToolSpec
from apps.engine_core.assistant.core_tools import COMMANDS_PATH, engine_json, require_steps_succeeded
from apps.engine_core.assistant.oj_tools._args import int_arg
from apps.opendj_cli.catalog import STEM_VALUES, VERBS
from apps.opendj_cli.kinds import DECK_VALUES
from apps.opendj_cli.orders import single
from apps.opendj_cli.verbs import InvocationError, parse_invocation

# ----- configuration ------------------------------------------------------
LOW_DECK: int = min(DECK_VALUES)
HIGH_DECK: int = max(DECK_VALUES)


# ----- argument checks ----------------------------------------------------
def _deck(args: dict[str, Any]) -> int:
    deck = int_arg(args, "deck", low=LOW_DECK, high=HIGH_DECK)
    if deck is None:
        raise ToolFailure("invalid_arguments", f"deck is required: a whole number from {LOW_DECK} to {HIGH_DECK}")
    return deck


def _stem(args: dict[str, Any]) -> str:
    stem = args.get("stem")
    if stem not in STEM_VALUES:
        raise ToolFailure("invalid_arguments", f"stem must be one of {'|'.join(STEM_VALUES)}, got {stem!r}")
    return stem


def _enabled(args: dict[str, Any]) -> bool:
    enabled = args.get("enabled")
    if not isinstance(enabled, bool):
        raise ToolFailure("invalid_arguments", f"enabled is required: true or false, got {enabled!r}")
    return enabled


def _value(args: dict[str, Any]) -> float | int:
    value = args.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolFailure("invalid_arguments", f"value is required: a number, got {value!r}")
    return value


# ----- order helpers ------------------------------------------------------
def _order(tokens: list[str]) -> dict[str, Any]:
    """The AGENT-03 single order the CLI parser builds for ``tokens`` (verb first, then key=value arguments)."""
    try:
        return single(parse_invocation(tokens))
    except (InvocationError, ValueError) as exc:
        raise ToolFailure("invalid_arguments", str(exc)) from exc


async def _post(ctx: ToolContext, order: dict[str, Any]) -> dict[str, Any]:
    result = require_steps_succeeded(await engine_json(ctx, "POST", COMMANDS_PATH, order))
    return {"order": order, "steps": result["steps"]}


def _toggle_verb(verb: str, flag: str) -> ToolRun:
    """A tool run that posts ``verb`` for one deck and stem, sending ``enabled`` as the verb's ``flag`` argument."""

    async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
        tokens = [verb, f"deck={_deck(args)}", f"stem={_stem(args)}", f"{flag}={str(_enabled(args)).lower()}"]
        return await _post(ctx, _order(tokens))

    return run


async def _run_stem_level(ctx: ToolContext, args: dict[str, Any]) -> Any:
    tokens = ["stem_gain", f"deck={_deck(args)}", f"stem={_stem(args)}", f"value={_value(args)!r}"]
    return await _post(ctx, _order(tokens))


# ----- schemas ------------------------------------------------------------
_DECK_PARAM = {
    "type": "integer",
    "minimum": LOW_DECK,
    "maximum": HIGH_DECK,
    "description": f"Deck number, {LOW_DECK} to {HIGH_DECK}.",
}
_STEM_PARAM = {"type": "string", "enum": list(STEM_VALUES), "description": "Which loaded stem."}
_FAILS = (
    "The deck must already hold stems (stems_generate makes them). "
    "Fails no_performance_page when no page is open, command_failed when the page could not do it."
)


def _spec(name: str, verb: str, does: str, properties: dict[str, Any], run: ToolRun) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=(
            f"{does} {_FAILS} The user approves each call. CLI: opendj {verb} {VERBS[verb].usage()}; MCP: command."
        ),
        parameters={"type": "object", "properties": properties, "required": list(properties)},
        gate="confirm",
        run=run,
    )


TOOLS: tuple[ToolSpec, ...] = (
    _spec(
        "stem_level",
        "stem_gain",
        "Set one stem's level on a deck: 0 is silent, 0.5 is unity (the default), 1 is boosted.",
        {
            "deck": _DECK_PARAM,
            "stem": _STEM_PARAM,
            "value": {"type": "number", "minimum": 0, "maximum": 1, "description": "Stem level from 0 to 1."},
        },
        _run_stem_level,
    ),
    _spec(
        "stem_mute",
        "stem_mute",
        "Mute or unmute one stem on a deck (for example drop the vocals on deck 1: stem vocal, enabled true).",
        {
            "deck": _DECK_PARAM,
            "stem": _STEM_PARAM,
            "enabled": {"type": "boolean", "description": "true mutes the stem, false brings it back."},
        },
        _toggle_verb("stem_mute", "muted"),
    ),
    _spec(
        "stem_solo",
        "stem_solo",
        "Solo or unsolo one stem on a deck; while any stem is soloed only soloed stems play.",
        {
            "deck": _DECK_PARAM,
            "stem": _STEM_PARAM,
            "enabled": {"type": "boolean", "description": "true solos the stem, false clears its solo."},
        },
        _toggle_verb("stem_solo", "solo"),
    ),
)
