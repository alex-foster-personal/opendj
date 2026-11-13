"""Transition memory for OJ: ``pair_transition_get``, ``pair_transition_save``, ``pair_transition_forget``.

One saved transition per ordered pair of tracks (``out`` into ``in``), most
recent wins, over ``/api/v1/autodj/pair-transition`` (MEMORY-02). Saving
replaces the pair's plan, so the next time that pair is mixed the saved plan
is the default; forgetting deletes it. A pair with nothing saved is a 404
``pair_transition_not_found``, which the tools surface as ``engine_error``
naming 404, as the registry contract asks.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
PAIR_PATH: str = "/api/v1/autodj/pair-transition"
#: The route's ``Source`` literal, pinned to it by the tests.
SOURCES: tuple[str, ...] = ("planner", "captured", "edited")
#: A plan OJ writes or changes for the user is an edit; ``planner`` is for a plan the planner proposed unchanged.
DEFAULT_SOURCE: str = "edited"
MIN_DECK: int = 1
MAX_DECK: int = 4


# ----- helpers ------------------------------------------------------------
def _pair_query(args: dict[str, Any]) -> dict[str, str]:
    """The ordered pair's ``out`` and ``in`` stable_ids, checked."""
    return {"out": str(text_arg(args, "out", required=True)), "in": str(text_arg(args, "in", required=True))}


def _deck_query(args: dict[str, Any]) -> dict[str, int]:
    """``out_deck`` and ``in_deck`` together or not at all; empty when absent."""
    out_deck = int_arg(args, "out_deck", low=MIN_DECK, high=MAX_DECK)
    in_deck = int_arg(args, "in_deck", low=MIN_DECK, high=MAX_DECK)
    if (out_deck is None) != (in_deck is None):
        raise ToolFailure("invalid_arguments", "out_deck and in_deck come together")
    return {} if out_deck is None or in_deck is None else {"out_deck": out_deck, "in_deck": in_deck}


def _save_body(args: dict[str, Any]) -> dict[str, Any]:
    plan = args.get("plan")
    if not isinstance(plan, dict):
        raise ToolFailure("invalid_arguments", f"plan must be a transition plan object, got {plan!r}"[:300])
    source = args.get("source", DEFAULT_SOURCE)
    if source not in SOURCES:
        raise ToolFailure("invalid_arguments", f"source must be one of {list(SOURCES)}, got {source!r}")
    style = text_arg(args, "style") or plan.get("style")
    if not isinstance(style, str) or not style.strip():
        raise ToolFailure("invalid_arguments", "give style, or a plan that names its own style")
    return {"plan": plan, "source": source, "style": style}


# ----- tools --------------------------------------------------------------
async def _pair_transition_get(ctx: ToolContext, args: dict[str, Any]) -> Any:
    query = {**_pair_query(args), **_deck_query(args)}
    return (await engine_call(ctx, "GET", f"{PAIR_PATH}?{urlencode(query)}")).body


async def _pair_transition_save(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return (await engine_call(ctx, "PUT", PAIR_PATH, _save_body(args))).body


async def _pair_transition_forget(ctx: ToolContext, args: dict[str, Any]) -> Any:
    pair = _pair_query(args)
    await engine_call(ctx, "DELETE", f"{PAIR_PATH}?{urlencode(pair)}")
    return {"forgotten": True, **pair}


_PAIR_PROPERTIES: dict[str, Any] = {
    "out": {"type": "string", "description": "stable_id of the outgoing track (the one playing first)."},
    "in": {"type": "string", "description": "stable_id of the incoming track."},
}
_DECK = {"type": "integer", "minimum": MIN_DECK, "maximum": MAX_DECK}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="pair_transition_get",
        description=(
            "The transition saved for one ordered pair of tracks (out into in): {plan, meta} with "
            "meta.source, style, times_played and dates. Give out_deck and in_deck together to get "
            "the plan moved onto the decks the pair is loaded on now. Nothing saved fails "
            "engine_error naming 404 (pair_transition_not_found). "
            "CLI: opendj dj pair-transition get --out X --in Y; MCP: app_state with "
            "/api/v1/autodj/pair-transition?out=X&in=Y."
        ),
        parameters={
            "type": "object",
            "properties": {**_PAIR_PROPERTIES, "out_deck": _DECK, "in_deck": _DECK},
            "required": ["out", "in"],
        },
        gate="read",
        run=_pair_transition_get,
    ),
    ToolSpec(
        name="pair_transition_save",
        description=(
            "Remember a transition plan for the pair it names (plan.out.track into plan.in.track), "
            "replacing any saved one, so that plan is the pair's default next time. Answers "
            "{plan, meta}. A document that is not a plan fails engine_error naming 422 "
            "(plan_invalid). source is edited unless the plan is the planner's own, unchanged "
            "(planner). style defaults to the plan's own. "
            "CLI: opendj dj pair-transition set PLAN.json --source edited; "
            "MCP: library with PUT /api/v1/autodj/pair-transition."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {"type": "object", "description": "A transition plan: out, in, style, events."},
                "source": {"type": "string", "enum": list(SOURCES), "default": DEFAULT_SOURCE},
                "style": {"type": "string", "description": "Style name; default the plan's own."},
            },
            "required": ["plan"],
        },
        gate="confirm",
        run=_pair_transition_save,
    ),
    ToolSpec(
        name="pair_transition_forget",
        description=(
            "Forget the transition saved for one ordered pair of tracks (out into in). Answers "
            "{forgotten, out, in}; nothing saved fails engine_error naming 404. "
            "CLI: opendj dj pair-transition clear --out X --in Y; "
            "MCP: library with DELETE /api/v1/autodj/pair-transition?out=X&in=Y (needs OPENDJ_MCP_ENABLE_DESTRUCTIVE=1)."
        ),
        parameters={"type": "object", "properties": _PAIR_PROPERTIES, "required": ["out", "in"]},
        gate="confirm",
        run=_pair_transition_forget,
    ),
)
