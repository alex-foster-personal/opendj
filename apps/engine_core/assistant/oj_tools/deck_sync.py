"""Sync decks: tempo, beat sync, key sync, key shift and the MASTER deck.

Each tool is one existing bus verb (``tempo``, ``beat_sync``, ``key_sync``,
``key_shift``, ``master`` in ``apps/opendj_cli/catalog.py``), built into an
AGENT-03 single order by the CLI's own parser and posted to
``POST /api/v1/commands``. So ``opendj beat_sync 2 true``, MCP ``command`` and
OJ ``deck_beat_sync`` send the identical order. Decks and key sync modes are
checked here against ``DECK_VALUES`` and ``KEY_SYNC_MODE_VALUES`` before any
request (``invalid_arguments``); the page refuses a tempo ratio outside the
deck's pitch range (``command_failed``). The answer is judged whole by
``core_tools.require_steps_succeeded``, then cut to the order and its steps.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolRun, ToolSpec
from apps.engine_core.assistant.core_tools import COMMANDS_PATH, engine_json, require_steps_succeeded
from apps.engine_core.assistant.oj_tools._args import int_arg
from apps.opendj_cli.catalog import KEY_SYNC_MODE_VALUES, VERBS
from apps.opendj_cli.kinds import DECK_VALUES
from apps.opendj_cli.orders import single
from apps.opendj_cli.verbs import InvocationError, parse_invocation

# ----- configuration ------------------------------------------------------
LOW_DECK: int = min(DECK_VALUES)
HIGH_DECK: int = max(DECK_VALUES)
MASTER_TARGET: str = "master"
#: The ``key_shift`` verb's ``<semitones:-12..12>`` signature in the catalog (and the MCP ``deck_key_sync`` shift).
SHIFT_LOW: int = -12
SHIFT_HIGH: int = 12


# ----- argument checks ----------------------------------------------------
def _deck(args: dict[str, Any]) -> int:
    deck = int_arg(args, "deck", low=LOW_DECK, high=HIGH_DECK)
    if deck is None:
        raise ToolFailure("invalid_arguments", f"deck is required: a whole number from {LOW_DECK} to {HIGH_DECK}")
    return deck


def _enabled(args: dict[str, Any]) -> bool:
    enabled = args.get("enabled")
    if not isinstance(enabled, bool):
        raise ToolFailure("invalid_arguments", f"enabled is required: true or false, got {enabled!r}")
    return enabled


def _ratio(args: dict[str, Any]) -> float | int:
    ratio = args.get("ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not ratio > 0:
        raise ToolFailure("invalid_arguments", f"ratio is required: a playback ratio above 0 (1 is +0%), got {ratio!r}")
    return ratio


def _semitones(args: dict[str, Any]) -> int:
    semitones = int_arg(args, "semitones", low=SHIFT_LOW, high=SHIFT_HIGH)
    if semitones is None:
        raise ToolFailure(
            "invalid_arguments", f"semitones is required: a whole number from {SHIFT_LOW} to {SHIFT_HIGH}"
        )
    return semitones


def _mode(args: dict[str, Any]) -> str | None:
    mode = args.get("mode")
    if mode is not None and mode not in KEY_SYNC_MODE_VALUES:
        raise ToolFailure("invalid_arguments", f"mode must be one of {'|'.join(KEY_SYNC_MODE_VALUES)}, got {mode!r}")
    return mode


def _target(args: dict[str, Any]) -> int | str | None:
    target = args.get("target")
    if target is None or target == MASTER_TARGET:
        return target
    if isinstance(target, bool) or target not in DECK_VALUES:
        raise ToolFailure(
            "invalid_arguments", f"target must be a deck {LOW_DECK} to {HIGH_DECK} or {MASTER_TARGET!r}, got {target!r}"
        )
    if target == _deck(args):
        raise ToolFailure("invalid_arguments", f"deck {target} cannot key sync to itself")
    return target


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


def _verb_run(verb: str, tokens_for: Callable[[dict[str, Any]], list[str]]) -> ToolRun:
    """A tool run that posts ``verb`` for the deck plus the key=value tokens ``tokens_for`` checks and builds."""

    async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
        return await _post(ctx, _order([verb, f"deck={_deck(args)}", *tokens_for(args)]))

    return run


def _enabled_tokens(args: dict[str, Any]) -> list[str]:
    return [f"enabled={str(_enabled(args)).lower()}"]


def _key_sync_tokens(args: dict[str, Any]) -> list[str]:
    tokens = _enabled_tokens(args)
    mode, target = _mode(args), _target(args)
    if mode is not None:
        tokens.append(f"mode={mode}")
    if target is not None:
        tokens.append(f"target={target}")
    return tokens


# ----- schemas ------------------------------------------------------------
_DECK_PARAM = {
    "type": "integer",
    "minimum": LOW_DECK,
    "maximum": HIGH_DECK,
    "description": f"Deck number, {LOW_DECK} to {HIGH_DECK}.",
}
_ENABLED_PARAM = {"type": "boolean", "description": "true turns it on, false turns it off."}
_FAILS = "Fails no_performance_page when no page is open, command_failed when the page could not do it."


def _spec(
    name: str, verb: str, does: str, properties: dict[str, Any], run: ToolRun, *, optional: tuple[str, ...] = ()
) -> ToolSpec:
    mcp = "deck_key_sync or command" if verb.startswith("key_") else "command"
    return ToolSpec(
        name=name,
        description=(
            f"{does} {_FAILS} The user approves each call. CLI: opendj {verb} {VERBS[verb].usage()}; MCP: {mcp}."
        ),
        parameters={
            "type": "object",
            "properties": properties,
            "required": [key for key in properties if key not in optional],
        },
        gate="confirm",
        run=run,
    )


TOOLS: tuple[ToolSpec, ...] = (
    _spec(
        "deck_beat_sync",
        "beat_sync",
        "Turn BEAT SYNC on or off for a deck, locking its tempo and beats to the MASTER deck "
        "(for example sync deck 2 to deck 1: make deck 1 master, then beat sync deck 2 on).",
        {"deck": _DECK_PARAM, "enabled": _ENABLED_PARAM},
        _verb_run("beat_sync", _enabled_tokens),
    ),
    _spec(
        "deck_key_shift",
        "key_shift",
        "Put a deck at an absolute key shift in semitones (0 is the original key); turns key lock on and KEY SYNC off.",
        {
            "deck": _DECK_PARAM,
            "semitones": {
                "type": "integer",
                "minimum": SHIFT_LOW,
                "maximum": SHIFT_HIGH,
                "description": f"Absolute shift, {SHIFT_LOW} to {SHIFT_HIGH}; 0 resets to the original key.",
            },
        },
        _verb_run("key_shift", lambda args: [f"semitones={_semitones(args)}"]),
    ),
    _spec(
        "deck_key_sync",
        "key_sync",
        "Turn KEY SYNC on or off for a deck: transpose it onto the target's key (the MASTER deck unless a "
        "target is given) and turn key lock on.",
        {
            "deck": _DECK_PARAM,
            "enabled": _ENABLED_PARAM,
            "mode": {
                "type": "string",
                "enum": list(KEY_SYNC_MODE_VALUES),
                "description": "exact (default) lands on the target key by the shortest path; relative lands on "
                "its Camelot number in this deck's own mode; compatible is the Pioneer family match.",
            },
            "target": {
                "anyOf": [
                    {"type": "integer", "minimum": LOW_DECK, "maximum": HIGH_DECK},
                    {"type": "string", "enum": [MASTER_TARGET]},
                ],
                "description": f"The deck whose key to match, or {MASTER_TARGET!r} (default).",
            },
        },
        _verb_run("key_sync", _key_sync_tokens),
        optional=("mode", "target"),
    ),
    _spec(
        "deck_master",
        "master",
        "Make a deck the MASTER deck that beat sync and key sync follow.",
        {"deck": _DECK_PARAM},
        _verb_run("master", lambda _args: []),
    ),
    _spec(
        "deck_tempo",
        "tempo",
        "Set a deck's tempo as a playback ratio: 1 is the track's own tempo, 1.02 is +2%, 0.98 is -2%; the page "
        "refuses a ratio outside the deck's pitch range.",
        {
            "deck": _DECK_PARAM,
            "ratio": {"type": "number", "exclusiveMinimum": 0, "description": "Playback ratio, 1 is +0%."},
        },
        _verb_run("tempo", lambda args: [f"ratio={_ratio(args)!r}"]),
    ),
)
