"""Plan and run the next transition: ``transition_plan``, ``transition_start``, ``transition_arm_next``, ``transition_cancel``.

``transition_plan`` is the app's own press (``POST /api/v1/dj/transition/plan``):
it plans out of one loaded deck into another from the live playhead and
answers the order the page would run, without running it or writing anything
(the route never renders and only reads the pair's saved plan), so its gate is
``read``. ``transition_start`` hands that ``{"plan": ...}`` order to
``POST /api/v1/commands``, the path MCP ``schedule_plan`` submits on.
``transition_arm_next`` and ``transition_cancel`` are the AutoPlay menu's bus
verbs ``auto_play_next_arm`` and ``auto_play_next_cancel``, checked here by the
page's own rules (``performance-ipc-guards.ts``) before anything is sent.

Unlike MCP ``schedule_plan``, start does not load the incoming track (the plan
was made between two loaded decks) and adds no master mute, as with the core
``command`` tool: the user approves each call and hears what they approved.
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import COMMANDS_PATH, engine_json, require_steps_succeeded
from apps.engine_core.assistant.oj_tools._args import int_arg, number_arg, text_arg
from apps.opendj_cli.catalog import TRANSITION_INTENT_VALUES

# ----- configuration ------------------------------------------------------
PLAN_PATH: str = "/api/v1/dj/transition/plan"
ARM_VERB: str = "auto_play_next_arm"
CANCEL_VERB: str = "auto_play_next_cancel"
DECKS: int = 4
#: What one plan answer carries for the model: the order to start, why, and the pick.
PLAN_FIELDS: tuple[str, ...] = ("order", "reason", "intent", "pick", "out_bar_now", "saved")
#: The longest from_ms the tools accept: a day of audio, far past any track.
MAX_FROM_MS: float = 86_400_000.0


# ----- helpers ------------------------------------------------------------
def _intent(args: dict[str, Any]) -> str | None:
    intent = text_arg(args, "intent")
    if intent is not None and intent not in TRANSITION_INTENT_VALUES:
        raise ToolFailure("invalid_arguments", f"intent must be one of {TRANSITION_INTENT_VALUES}, got {intent!r}")
    return intent


def _deck_pair(args: dict[str, Any], *, required: bool) -> dict[str, int]:
    """out_deck and in_deck together (the page refuses one alone), on different decks."""
    out_deck = int_arg(args, "out_deck", low=1, high=DECKS)
    in_deck = int_arg(args, "in_deck", low=1, high=DECKS)
    if out_deck is None and in_deck is None and not required:
        return {}
    if out_deck is None or in_deck is None:
        raise ToolFailure("invalid_arguments", "give out_deck and in_deck together")
    if out_deck == in_deck:
        raise ToolFailure("invalid_arguments", f"out_deck and in_deck are both {out_deck}")
    return {"out_deck": out_deck, "in_deck": in_deck}


def _given(**values: Any) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


async def _send(ctx: ToolContext, order: dict[str, Any]) -> Any:
    """One order to the open page; a step that did not run fails the call (command_failed).

    The answer is judged whole, then only its ``steps`` go to the model: the mirror delta can be large.
    """
    return {"steps": require_steps_succeeded(await engine_json(ctx, "POST", COMMANDS_PATH, order))["steps"]}


# ----- tools --------------------------------------------------------------
async def _transition_plan(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from_ms = number_arg(args, "from_ms", low=0.0, high=MAX_FROM_MS)
    body = {**_deck_pair(args, required=True), **_given(intent=_intent(args), from_ms=from_ms)}
    answer = await engine_json(ctx, "POST", PLAN_PATH, body)
    return {name: answer[name] for name in PLAN_FIELDS}


async def _transition_start(ctx: ToolContext, args: dict[str, Any]) -> Any:
    order = args.get("order")
    if not isinstance(order, dict) or set(order) != {"plan"} or not isinstance(order["plan"], dict):
        raise ToolFailure(
            "invalid_arguments", f'order must be the {{"plan": {{...}}}} transition_plan answered, got {order!r:.300}'
        )
    return await _send(ctx, order)


async def _transition_arm_next(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from_ms = number_arg(args, "from_ms", low=0.0, high=MAX_FROM_MS)
    command = {"type": ARM_VERB, **_given(intent=_intent(args)), **_deck_pair(args, required=False)}
    return await _send(ctx, {"single": {**command, **_given(from_ms=from_ms)}})


async def _transition_cancel(ctx: ToolContext, _args: dict[str, Any]) -> Any:
    return await _send(ctx, {"single": {"type": CANCEL_VERB}})


_DECK = {"type": "integer", "minimum": 1, "maximum": DECKS}
_INTENT = {"type": "string", "enum": list(TRANSITION_INTENT_VALUES), "description": "Default smooth."}
_FROM_MS = {"type": "number", "minimum": 0, "description": "Leave the outgoing track near this ms on its own clock."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="transition_plan",
        description=(
            "Plan a transition from out_deck into in_deck (both loaded) from the live playhead, "
            "without running it. Answers {order, reason, intent, pick, out_bar_now, saved}: pick is "
            "the style, bars and rationale; order null means no plan, and reason says why. Pass "
            "order to transition_start to run it. CLI: opendj api POST /api/v1/dj/transition/plan; "
            "MCP: propose_transition."
        ),
        parameters={
            "type": "object",
            "properties": {"out_deck": _DECK, "in_deck": _DECK, "intent": _INTENT, "from_ms": _FROM_MS},
            "required": ["out_deck", "in_deck"],
        },
        gate="read",
        run=_transition_plan,
    ),
    ToolSpec(
        name="transition_start",
        description=(
            "Run a planned transition: order is transition_plan's order, unchanged. The page arms "
            "it and fires each step on the outgoing deck's bars. The user approves each call. "
            "Twins (take the plan document, may load the track): CLI: opendj dj schedule; MCP: schedule_plan."
        ),
        parameters={
            "type": "object",
            "properties": {"order": {"type": "object", "description": 'The {"plan": {...}} from transition_plan.'}},
            "required": ["order"],
        },
        gate="confirm",
        run=_transition_start,
    ),
    ToolSpec(
        name="transition_arm_next",
        description=(
            "Arm AutoPlay's next transition: the page plans it from the live position when it is "
            "due. Optional intent; out_deck with in_deck (together) and from_ms plan out of that "
            "deck around that point. The user approves each call. "
            "CLI: opendj auto_play_next_arm [intent] [out_deck] [in_deck] [from_ms]; MCP: command."
        ),
        parameters={
            "type": "object",
            "properties": {"intent": _INTENT, "out_deck": _DECK, "in_deck": _DECK, "from_ms": _FROM_MS},
        },
        gate="confirm",
        run=_transition_arm_next,
    ),
    ToolSpec(
        name="transition_cancel",
        description=(
            "Cancel the armed next transition. The user approves each call. "
            "CLI: opendj auto_play_next_cancel; MCP: command."
        ),
        parameters={"type": "object", "properties": {}},
        gate="confirm",
        run=_transition_cancel,
    ),
)
