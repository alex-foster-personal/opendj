"""Read and set one track's auto gain: ``track_gain`` and ``track_gain_offset_set``.

Both call the routes the deck and the track editor use
(``routes/auto_gain.py``): ``GET /api/v1/auto-gain/tracks/{id}`` is the offset
a deck applies at load and why, read from stored loudness analysis (nothing is
analysed on the way); ``PUT .../user-offset`` sets the user's own dB, which wins
over the analysed value, or clears it with null. The range is the route's own
(``auto_gain.OFFSET_MIN_DB..OFFSET_MAX_DB``), checked here before any request.
The value lands in the app's own state database only (rekordbox and the audio
files are not written).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.analysis_loudness.http import auto_gain
from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_json
from apps.engine_core.assistant.oj_tools._args import number_arg, text_arg

# ----- configuration ------------------------------------------------------
AUTO_GAIN_PATH: str = "/api/v1/auto-gain/tracks"
#: The route's own bounds for a user offset (``validate_user_offset_db``).
OFFSET_MIN_DB: float = auto_gain.OFFSET_MIN_DB
OFFSET_MAX_DB: float = auto_gain.OFFSET_MAX_DB
#: What one gain answer carries for the model.
GAIN_FIELDS: tuple[str, ...] = (
    "stable_id",
    "status",
    "enabled",
    "target_lufs",
    "integrated_lufs",
    "true_peak_dbtp",
    "analysed_offset_db",
    "user_offset_db",
    "effective_offset_db",
    "limited_by",
    "loudness_reason",
)


# ----- helpers ------------------------------------------------------------
def _gain_path(args: dict[str, Any]) -> str:
    stable_id = str(text_arg(args, "stable_id", required=True))
    return f"{AUTO_GAIN_PATH}/{quote(stable_id, safe='')}"


def _compact(answer: dict[str, Any]) -> dict[str, Any]:
    return {name: answer[name] for name in GAIN_FIELDS if name in answer}


def _offset_db(args: dict[str, Any]) -> float | None:
    """The required ``offset_db``: a number within the route's range, or None to clear."""
    if "offset_db" not in args:
        raise ToolFailure(
            "invalid_arguments", f"offset_db is required: dB from {OFFSET_MIN_DB:g} to {OFFSET_MAX_DB:g}, or null"
        )
    return number_arg(args, "offset_db", low=OFFSET_MIN_DB, high=OFFSET_MAX_DB)


# ----- tools --------------------------------------------------------------
async def _track_gain(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _compact(await engine_json(ctx, "GET", _gain_path(args)))


async def _track_gain_offset_set(ctx: ToolContext, args: dict[str, Any]) -> Any:
    path = _gain_path(args)
    offset_db = _offset_db(args)
    return _compact(await engine_json(ctx, "PUT", f"{path}/user-offset", {"offset_db": offset_db}))


_STABLE_ID = {"type": "string", "description": "The track's stable_id (from library_search)."}

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="track_gain",
        description=(
            "One track's auto gain: the dB a deck applies when it loads the track "
            "(effective_offset_db) and why (status analysed, user, not_analysed or off; "
            "measured integrated_lufs and true_peak_dbtp; the analysed and user offsets). "
            "Reads stored analysis only. An unknown id fails engine_error naming 404. "
            "CLI: opendj track auto-gain <stable_id>; MCP: app_state with "
            "/api/v1/auto-gain/tracks/{stable_id}."
        ),
        parameters={
            "type": "object",
            "properties": {"stable_id": _STABLE_ID},
            "required": ["stable_id"],
        },
        gate="read",
        run=_track_gain,
    ),
    ToolSpec(
        name="track_gain_offset_set",
        description=(
            f"Set the user's own gain offset for one track, {OFFSET_MIN_DB:g} to {OFFSET_MAX_DB:g} dB "
            "(e.g. 2 for 'this track is too quiet, bump it 2 dB'); it wins over the analysed "
            "offset at every load. offset_db null clears it so the analysed offset applies again. "
            "Answers the same fields as track_gain. "
            "CLI: opendj track auto-gain <stable_id> set <dB> | clear; MCP: library PUT "
            "/api/v1/auto-gain/tracks/{stable_id}/user-offset."
        ),
        parameters={
            "type": "object",
            "properties": {
                "stable_id": _STABLE_ID,
                "offset_db": {
                    "type": ["number", "null"],
                    "minimum": OFFSET_MIN_DB,
                    "maximum": OFFSET_MAX_DB,
                    "description": "dB to apply, or null to clear the user's value.",
                },
            },
            "required": ["stable_id", "offset_db"],
        },
        gate="confirm",
        run=_track_gain_offset_set,
    ),
)
