"""Tracks with a dodgy beatgrid: ``beatgrid_flags`` and ``beatgrid_flag_dismiss``.

These wrap the beatgrid flag routes (``apps/webui/server/routes/beatgrid_flags.py``,
GRIDFLAG-03). The listing reads verdicts the scan already stored and never parses
a grid; there is deliberately no scan tool here, because a scan parses every
rekordbox analysis file (heavy compute the user starts from the app or the CLI).
Dismissing hides one track's flag (a user track field); restoring brings it back.
:data:`GRID_CLASSES` and :data:`AVAILABILITY` are the route's own query literals
(``GridClassFilter``, ``Availability``), pinned by a test, so a value outside them
fails ``invalid_arguments`` before any request instead of a 422.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_call, engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, text_arg

# ----- configuration ------------------------------------------------------
FLAGS_PATH: str = "/api/v1/beatgrid-flags"
#: The route's ``GridClassFilter`` literal, in its order; ``flagged`` is suspect plus variable_tempo.
GRID_CLASSES: tuple[str, ...] = ("flagged", "suspect", "variable_tempo", "unknown", "ok", "all")
#: The route's ``Availability`` literal: tracks with audio on this machine, or every live track.
AVAILABILITY: tuple[str, ...] = ("present", "all")
DEFAULT_GRID_CLASS: str = "flagged"
DEFAULT_AVAILABILITY: str = "present"
#: What one flag row carries for the model; the route's other numbers stay in the engine.
ROW_FIELDS: tuple[str, ...] = (
    "stable_id",
    "title",
    "artist",
    "grid_class",
    "reason",
    "dismissed",
    "message",
    "median_bpm",
    "uneven_interval_count",
    "worst_deviation_ms",
)
DEFAULT_LIMIT: int = 20
MAX_LIMIT: int = 100


# ----- helpers ------------------------------------------------------------
def _choice_arg(args: dict[str, Any], name: str, choices: tuple[str, ...], default: str) -> str:
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise ToolFailure("invalid_arguments", f"{name} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _bool_arg(args: dict[str, Any], name: str, *, required: bool) -> bool | None:
    value = args.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, bool):
        raise ToolFailure("invalid_arguments", f"{name} must be true or false, got {value!r}")
    return value


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in ROW_FIELDS if name in row}


# ----- tools --------------------------------------------------------------
async def _beatgrid_flags(ctx: ToolContext, args: dict[str, Any]) -> Any:
    limit = int_arg(args, "limit", low=1, high=MAX_LIMIT) or DEFAULT_LIMIT
    query = {
        "grid_class": _choice_arg(args, "grid_class", GRID_CLASSES, DEFAULT_GRID_CLASS),
        "include_dismissed": "true" if _bool_arg(args, "include_dismissed", required=False) else "false",
        "availability": _choice_arg(args, "availability", AVAILABILITY, DEFAULT_AVAILABILITY),
        # One row past the limit says whether more match, without a second call.
        "limit": limit + 1,
    }
    page = await engine_json(ctx, "GET", f"{FLAGS_PATH}?{urlencode(query)}")
    rows = page["items"]
    return {
        "denominator": page["denominator"],
        "tracks_in_scope": page["present"],
        "counts": page["counts"],
        "items": [_compact(row) for row in rows[:limit]],
        "more": len(rows) > limit,
    }


async def _beatgrid_flag_dismiss(ctx: ToolContext, args: dict[str, Any]) -> Any:
    stable_id = str(text_arg(args, "stable_id", required=True))
    dismissed = _bool_arg(args, "dismissed", required=True)
    path = f"{FLAGS_PATH}/{quote(stable_id, safe='')}/dismissed"
    return (await engine_call(ctx, "PUT", path, {"dismissed": dismissed})).body


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="beatgrid_flags",
        description=(
            "Tracks whose beatgrid looks wrong (Beat Sync may wander on them), from verdicts the "
            "last grid scan stored; this never scans. grid_class flagged (default) is suspect plus "
            "variable_tempo; dismissed flags are hidden unless include_dismissed. availability "
            "present (default) counts tracks with audio on this machine, all every track. Answers "
            "{denominator, tracks_in_scope, counts, items, more}; counts cover the whole scope; "
            "each item has stable_id, title, artist, grid_class, reason, dismissed, message, "
            "median_bpm, uneven_interval_count, worst_deviation_ms. "
            "CLI: opendj track grid-flags [grid_class] [include-dismissed] [all-tracks]; "
            "MCP: app_state with GET /api/v1/beatgrid-flags."
        ),
        parameters={
            "type": "object",
            "properties": {
                "grid_class": {"type": "string", "enum": list(GRID_CLASSES), "default": DEFAULT_GRID_CLASS},
                "include_dismissed": {"type": "boolean", "default": False},
                "availability": {"type": "string", "enum": list(AVAILABILITY), "default": DEFAULT_AVAILABILITY},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
            },
        },
        gate="read",
        run=_beatgrid_flags,
    ),
    ToolSpec(
        name="beatgrid_flag_dismiss",
        description=(
            "Hide one track's beatgrid flag (dismissed true) or bring it back (dismissed false), "
            "by stable_id from beatgrid_flags. Answers {stable_id, dismissed, etag}. An unknown id "
            "fails engine_error naming 404. "
            "CLI: opendj track grid-flag <stable_id> dismiss|restore; "
            "MCP: library with PUT /api/v1/beatgrid-flags/{stable_id}/dismissed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": {"type": "string", "description": "The track's stable_id."},
                "dismissed": {"type": "boolean", "description": "true hides the flag, false restores it."},
            },
            "required": ["stable_id", "dismissed"],
        },
        gate="confirm",
        run=_beatgrid_flag_dismiss,
    ),
)
