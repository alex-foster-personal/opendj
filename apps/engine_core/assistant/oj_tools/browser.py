"""Browse like a person: put a playlist on screen, open Related tracks, audition a track in the headphones.

Each tool is one existing bus verb (``browser_select_playlist``,
``browser_related``, ``browser_related_criteria``, ``preview_cue``,
``preview_stop`` in ``apps/opendj_cli/catalog.py``), built into an AGENT-03
single order by the CLI's own parser and posted to ``POST /api/v1/commands``.
So ``opendj preview_cue oj-001 0.5``, MCP ``command`` and OJ ``preview_play``
send the identical order. Each tool's arguments are its verb's catalog
arguments, by construction: the schema is built from ``VERBS[verb].args``
(required unless the catalog marks one optional), each value's JSON type is
checked here before any request, and the parser refuses a value outside the
verb's own range (``invalid_arguments``). The answer is judged whole by
``core_tools.require_steps_succeeded``, then cut to the order and its steps.
"""

from __future__ import annotations

from typing import Any

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolRun, ToolSpec
from apps.engine_core.assistant.core_tools import COMMANDS_PATH, engine_json, require_steps_succeeded
from apps.engine_core.assistant.oj_tools._args import text_arg
from apps.opendj_cli.catalog import VERBS
from apps.opendj_cli.kinds import Arg
from apps.opendj_cli.orders import single
from apps.opendj_cli.verbs import InvocationError, parse_invocation

# ----- configuration ------------------------------------------------------
#: Catalog argument kind -> JSON schema type the model sends for it.
JSON_TYPES: dict[str, str] = {
    "text": "string",
    "bool": "boolean",
    "int": "integer",
    "unit": "number",
    "number": "number",
}


# ----- argument checks ----------------------------------------------------
def _token(args: dict[str, Any], argument: Arg) -> str | None:
    """``key=value`` for the parser, or None when an optional argument is absent; a wrong JSON type is refused."""
    key, kind = argument.key, argument.kind
    value = args.get(key)
    if value is None:
        if argument.optional:
            return None
        raise ToolFailure("invalid_arguments", f"{key} is required: {argument.signature}")
    if kind == "text":
        return f"{key}={text_arg(args, key, required=True)}"
    if kind == "bool" and isinstance(value, bool):
        return f"{key}={str(value).lower()}"
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
        return f"{key}={value}"
    if kind in ("unit", "number") and isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{key}={value!r}"
    raise ToolFailure("invalid_arguments", f"{key} must be {JSON_TYPES[kind]} {argument.signature}, got {value!r}")


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


def _verb_run(verb: str) -> ToolRun:
    """A tool run that posts ``verb`` with whichever of its catalog arguments the call carries."""

    async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
        tokens = [verb]
        for argument in VERBS[verb].args:
            token = _token(args, argument)
            if token is not None:
                tokens.append(token)
        return await _post(ctx, _order(tokens))

    return run


# ----- schemas ------------------------------------------------------------
def _parameters(verb: str, notes: dict[str, str]) -> dict[str, Any]:
    """The JSON schema for ``verb``'s catalog arguments: one property each, required unless optional."""
    args = VERBS[verb].args
    properties = {
        argument.key: {
            "type": JSON_TYPES[argument.kind],
            "description": f"{notes.get(argument.key, argument.key)} CLI form {argument.signature}.",
        }
        for argument in args
    }
    return {
        "type": "object",
        "properties": properties,
        "required": [argument.key for argument in args if not argument.optional],
    }


_FAILS = "Fails no_performance_page when no page is open, command_failed when the page could not do it."


def _spec(name: str, verb: str, does: str, notes: dict[str, str] | None = None) -> ToolSpec:
    cli = " ".join(part for part in ("opendj", verb, VERBS[verb].usage()) if part)
    return ToolSpec(
        name=name,
        description=f"{does} {_FAILS} The user approves each call. CLI: {cli}; MCP: command.",
        parameters=_parameters(verb, notes or {}),
        gate="confirm",
        run=_verb_run(verb),
    )


TOOLS: tuple[ToolSpec, ...] = (
    _spec(
        "browser_related",
        "browser_related",
        "Show the library's Related tracks tab (active true) or leave it (active false).",
        {"active": "true shows the Related tab, false leaves it."},
    ),
    _spec(
        "browser_related_criteria",
        "browser_related_criteria",
        "Change which criteria the Related tracks tab matches on; only the criteria given change.",
        {"bpm_percent": "BPM window in percent.", "rating": "Match equal or higher rating."},
    ),
    _spec(
        "browser_show_playlist",
        "browser_select_playlist",
        "Put a playlist on screen in the library browser, optionally selecting and scrolling to one of its tracks.",
        {"playlist_id": "Playlist id (from playlist_list).", "stable_id": "Track stable id to select in it."},
    ),
    _spec(
        "preview_play",
        "preview_cue",
        "Audition a library track in the headphones (cue bus) from a point in it; loads no deck, moves no transport.",
        {
            "stable_id": "Track stable id.",
            "ratio": "Start point as a fraction of the track, 0 is the start, 0.5 the middle.",
            "bpm": "The track's own BPM, so the preview can tempo-match the master deck.",
        },
    ),
    _spec("preview_stop", "preview_stop", "Stop the headphone preview; safe to call when none is playing."),
)
