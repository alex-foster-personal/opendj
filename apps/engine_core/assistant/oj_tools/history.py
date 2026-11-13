"""What the user has played: ``play_history`` over ``GET /api/play-analytics``.

The route reads the set event store (``sets.db``) and answers totals, every
recorded session and the top tracks by play count. The model gets the top
tracks only, each cut to :data:`ROW_FIELDS`, plus the summary counts, so a
long history answers "what did I play most" without flooding the context.
Ordering is the route's own: most plays first, ties broken by the latest play.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from apps.engine_core.assistant.agent_contract import ToolContext, ToolSpec
from apps.engine_core.assistant.core_tools import engine_json
from apps.engine_core.assistant.oj_tools._args import int_arg, number_arg

# ----- configuration ------------------------------------------------------
PLAY_ANALYTICS_PATH: str = "/api/play-analytics"

#: What one history row carries for the model (the route's ``top_tracks`` names).
ROW_FIELDS: tuple[str, ...] = ("stable_id", "title", "artist", "play_count", "last_played_at")
#: The summary counts kept; ``completed_duration_s`` is dropped.
SUMMARY_FIELDS: tuple[str, ...] = ("sessions", "plays", "unique_tracks", "plays_below_min_audible")
DEFAULT_LIMIT: int = 20
#: The route's own bound (``limit`` le=200).
MAX_LIMIT: int = 200
#: An hour of audible playback; no single play needs a higher "counts as played" bar.
MAX_MIN_AUDIBLE_S: float = 3600.0


# ----- tools --------------------------------------------------------------
async def _play_history(ctx: ToolContext, args: dict[str, Any]) -> Any:
    limit = int_arg(args, "limit", low=1, high=MAX_LIMIT, default=DEFAULT_LIMIT)
    min_audible_s = number_arg(args, "min_audible_s", low=0.0, high=MAX_MIN_AUDIBLE_S)
    query = {"limit": limit, **({"min_audible_s": min_audible_s} if min_audible_s is not None else {})}
    answer = await engine_json(ctx, "GET", f"{PLAY_ANALYTICS_PATH}?{urlencode(query)}")
    summary = {name: answer["summary"][name] for name in SUMMARY_FIELDS}
    items = [{name: row[name] for name in ROW_FIELDS} for row in answer["top_tracks"]]
    return {
        "min_audible_s": answer["filters"]["min_audible_s"],
        "summary": summary,
        "items": items,
        "more": summary["unique_tracks"] > len(items),
    }


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="play_history",
        description=(
            "What the user has played in recorded sets: the most played tracks first (ties by the "
            "latest play), each with stable_id, title, artist, play_count and last_played_at, plus "
            "summary counts of sessions, plays and unique tracks. A play counts once at least "
            "min_audible_s seconds of it were heard (engine default 60; 0 counts every load); "
            "plays imported from rekordbox or djay always count. Answers {min_audible_s, summary, "
            "items, more}. With no recorded set yet it fails engine_error naming 503. "
            "CLI: python -m apps.play_analytics --limit N --min-audible-s S; MCP: none yet."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
                "min_audible_s": {"type": "number", "minimum": 0, "maximum": MAX_MIN_AUDIBLE_S},
            },
        },
        gate="read",
        run=_play_history,
    ),
)
