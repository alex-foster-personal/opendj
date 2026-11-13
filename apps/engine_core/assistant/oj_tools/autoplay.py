"""Drive Auto DJ (AutoPlay): switch it, arm, decide now or cancel the next transition, and read its decision.

The four writes are existing bus verbs (``autoplay``, ``auto_play_next_arm``,
``auto_play_decide_now``, ``auto_play_next_cancel`` in
``apps/opendj_cli/catalog.py``), built into an AGENT-03 single order by the
CLI's own parser and posted to ``POST /api/v1/commands``, so ``opendj autoplay
false by_user=true``, MCP ``command`` and OJ ``autoplay_set`` send the
identical order. Each is ``confirm``-gated. PLAY-18 refuses AutoPlay off
unless ``by_user`` is true (a person asked): the user approves every
``autoplay_set`` call before it runs, so it always sends ``by_user=true``.

``autoplay_decision`` is ``read``-gated and only reads back what the page
published (``GET /api/v1/dj/transition/autoplay-decision``); it plans,
renders and analyses nothing. With no page that route answers 409
``no_performance_page`` in its own body, which surfaces as ``engine_error``
naming 409.
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolRun, ToolSpec
from apps.engine_core.assistant.core_tools import COMMANDS_PATH, engine_call, engine_json, require_steps_succeeded
from apps.engine_core.assistant.oj_tools._args import int_arg, number_arg
from apps.opendj_cli.catalog import TRANSITION_INTENT_VALUES, VERBS
from apps.opendj_cli.kinds import DECK_VALUES
from apps.opendj_cli.orders import single
from apps.opendj_cli.verbs import InvocationError, parse_invocation

# ----- configuration ------------------------------------------------------
DECISION_PATH: str = "/api/v1/dj/transition/autoplay-decision"
LOW_DECK: int = min(DECK_VALUES)
HIGH_DECK: int = max(DECK_VALUES)
#: ``from_ms`` is a position on the outgoing track: never negative, at most a day.
MAX_FROM_MS: float = 86_400_000.0


# ----- argument checks ----------------------------------------------------
def _enabled(args: dict[str, Any]) -> bool:
    enabled = args.get("enabled")
    if not isinstance(enabled, bool):
        raise ToolFailure("invalid_arguments", f"enabled is required: true or false, got {enabled!r}")
    return enabled


def _intent(args: dict[str, Any]) -> str | None:
    intent = args.get("intent")
    if intent is not None and intent not in TRANSITION_INTENT_VALUES:
        raise ToolFailure(
            "invalid_arguments", f"intent must be one of {'|'.join(TRANSITION_INTENT_VALUES)}, got {intent!r}"
        )
    return intent


def _deck(args: dict[str, Any], name: str) -> int | None:
    return int_arg(args, name, low=LOW_DECK, high=HIGH_DECK)


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


def _given(pairs: list[tuple[str, Any]]) -> list[str]:
    """``key=value`` tokens for the optional arguments that were given."""
    return [f"{key}={value}" for key, value in pairs if value is not None]


# ----- runs ---------------------------------------------------------------
async def _set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    enabled = str(_enabled(args)).lower()
    return await _post(ctx, _order(["autoplay", f"enabled={enabled}", "by_user=true"]))


async def _arm_next(ctx: ToolContext, args: dict[str, Any]) -> Any:
    optional = [
        ("intent", _intent(args)),
        ("out_deck", _deck(args, "out_deck")),
        ("in_deck", _deck(args, "in_deck")),
        ("from_ms", number_arg(args, "from_ms", low=0.0, high=MAX_FROM_MS)),
    ]
    return await _post(ctx, _order(["auto_play_next_arm", *_given(optional)]))


def _bare(verb: str) -> ToolRun:
    """A tool run that posts ``verb``, which takes no arguments."""

    async def run(ctx: ToolContext, _args: dict[str, Any]) -> Any:
        return await _post(ctx, _order([verb]))

    return run


async def _decision(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    return (await engine_call(ctx, "GET", DECISION_PATH)).body


# ----- schemas ------------------------------------------------------------
_FAILS = "Fails no_performance_page when no page is open, command_failed when the page could not do it."
_DECK_PARAM = {"type": "integer", "minimum": LOW_DECK, "maximum": HIGH_DECK}
_ARM_PARAMS: dict[str, Any] = {
    "intent": {"type": "string", "enum": list(TRANSITION_INTENT_VALUES), "description": "The transition's intent."},
    "out_deck": {**_DECK_PARAM, "description": "Plan out of this deck (with in_deck and from_ms; no fallback)."},
    "in_deck": {**_DECK_PARAM, "description": "Bring the next track in on this deck."},
    "from_ms": {"type": "number", "minimum": 0, "description": "Plan around this point (ms) on the outgoing track."},
}


def _spec(name: str, verb: str, does: str, properties: dict[str, Any], run: ToolRun, *, required: bool) -> ToolSpec:
    cli = " ".join(part for part in (f"opendj {verb}", VERBS[verb].usage()) if part)
    return ToolSpec(
        name=name,
        description=f"{does} {_FAILS} The user approves each call. CLI: {cli}; MCP: command.",
        parameters={"type": "object", "properties": properties, "required": list(properties) if required else []},
        gate="confirm",
        run=run,
    )


TOOLS: tuple[ToolSpec, ...] = (
    _spec(
        "autoplay_set",
        "autoplay",
        "Switch AutoPlay (Auto DJ) on or off; the order says a person asked (by_user=true), so off is not refused.",
        {"enabled": {"type": "boolean", "description": "true turns AutoPlay on, false turns it off."}},
        _set,
        required=True,
    ),
    _spec(
        "autoplay_arm_next",
        "auto_play_next_arm",
        "Arm the next AutoPlay transition, planned from the live position; the fixed loop runs when no plan fits.",
        _ARM_PARAMS,
        _arm_next,
        required=False,
    ),
    _spec(
        "autoplay_decide_now",
        "auto_play_decide_now",
        "Have AutoPlay decide its next transition now rather than at its target; read it with autoplay_decision.",
        {},
        _bare("auto_play_decide_now"),
        required=False,
    ),
    _spec(
        "autoplay_cancel_next",
        "auto_play_next_cancel",
        "Cancel the armed next AutoPlay transition.",
        {},
        _bare("auto_play_next_cancel"),
        required=False,
    ),
    ToolSpec(
        name="autoplay_decision",
        description=(
            "Read AutoPlay's next decision (its window, track and plan) as the page published it; null when it has "
            "none. Reads only. Fails engine_error naming 409 when no page is open. "
            "CLI: opendj dj autoplay-decision; MCP: get_mix_state (its autoplay_decision field)."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        gate="read",
        run=_decision,
    ),
)
