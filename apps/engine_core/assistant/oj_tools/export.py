"""Read a playlist or a recorded set as a list: ``playlist_export_text`` and ``set_tracklist``.

``playlist_export_text`` is the tab-separated text the tree's "Export as text"
menu item downloads (``GET /api/v1/playlists/{id}/export.txt``): a header row,
then one row per track in playlist order. ``set_tracklist`` is the kept plays
of one recorded set from its portable document (``GET /api/sets/{id}/export``),
trimmed to :data:`TRACKLIST_FIELDS` per row; the document's events, transitions
and labels never reach the model. Both answers are capped (whole lines or rows
only) and say ``truncated`` past the cap. Nothing here uploads or shares: the
SoundCloud export sends data out, and the UI owns that.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure, ToolSpec
from apps.engine_core.assistant.core_tools import engine_json
from apps.engine_core.assistant.oj_tools._args import text_arg

# ----- configuration ------------------------------------------------------
PLAYLISTS_PATH: str = "/api/v1/playlists"
SETS_PATH: str = "/api/sets"

#: Characters of playlist text handed to the model; cut back to the last whole line.
MAX_TEXT_CHARS: int = 10_000
#: Tracklist rows handed to the model (a long night is about 40 to 60 plays).
MAX_TRACKLIST_ROWS: int = 150
#: What one tracklist row carries; ``hidden`` rows have artist and title null.
TRACKLIST_FIELDS: tuple[str, ...] = ("timestamp_s", "stable_id", "artist", "title", "hidden", "deck")
#: The parts of the set manifest the model needs to name the set.
MANIFEST_FIELDS: tuple[str, ...] = ("session_id", "started_at", "ended_at")


# ----- helpers ------------------------------------------------------------
def _segment(value: str) -> str:
    return quote(value, safe="")


def _whole_lines(text: str, cap: int) -> str:
    """``text`` cut to at most ``cap`` characters, ending on a line break when one fits."""
    head = text[:cap]
    newline = head.rfind("\n")
    return head[: newline + 1] if newline >= 0 else head


# ----- tools --------------------------------------------------------------
async def _playlist_export_text(ctx: ToolContext, args: dict[str, Any]) -> Any:
    playlist_id = str(text_arg(args, "playlist_id", required=True))
    text = await engine_json(ctx, "GET", f"{PLAYLISTS_PATH}/{_segment(playlist_id)}/export.txt")
    if not isinstance(text, str):
        raise ToolFailure("engine_error", f"export.txt for {playlist_id} answered {type(text).__name__}, not text")
    kept = _whole_lines(text, MAX_TEXT_CHARS) if len(text) > MAX_TEXT_CHARS else text
    return {
        "playlist_id": playlist_id,
        "text": kept,
        "tracks": max(text.count("\n") - 1, 0),
        "truncated": len(kept) < len(text),
        "total_chars": len(text),
    }


async def _set_tracklist(ctx: ToolContext, args: dict[str, Any]) -> Any:
    session_id = str(text_arg(args, "session_id", required=True))
    document = await engine_json(ctx, "GET", f"{SETS_PATH}/{_segment(session_id)}/export")
    rows = document["tracklist"]
    manifest = document["manifest"]
    return {
        **{name: manifest.get(name) for name in MANIFEST_FIELDS},
        "tracklist": [{name: row.get(name) for name in TRACKLIST_FIELDS} for row in rows[:MAX_TRACKLIST_ROWS]],
        "total": len(rows),
        "truncated": len(rows) > MAX_TRACKLIST_ROWS,
    }


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="playlist_export_text",
        description=(
            "A playlist as tab-separated text, ready to copy: a header row (#, Title, Artist, BPM, "
            "Key, Time), then one row per track in playlist order. Answers {playlist_id, text, "
            f"tracks, truncated, total_chars}}; text past {MAX_TEXT_CHARS} characters is cut at a "
            "whole row and truncated is true. An unknown playlist fails engine_error naming 404. "
            "CLI: opendj playlist export <playlist_id> --format txt; "
            "MCP: app_state with /api/v1/playlists/{playlist_id}/export.txt."
        ),
        parameters={
            "type": "object",
            "properties": {"playlist_id": {"type": "string", "description": "The playlist's id."}},
            "required": ["playlist_id"],
        },
        gate="read",
        run=_playlist_export_text,
    ),
    ToolSpec(
        name="set_tracklist",
        description=(
            "The tracklist of one recorded set (a recorder session, not a playlist set): every kept "
            "play in order. Answers {session_id, started_at, ended_at, tracklist, total, truncated}; "
            "each row has timestamp_s (seconds into the set), stable_id, artist, title, hidden "
            "(artist and title null when the track is hidden from tracklists) and deck. Past "
            f"{MAX_TRACKLIST_ROWS} rows the list is cut and truncated is true. An unknown session "
            "fails engine_error naming 404; one still recording fails naming 409. "
            "CLI: opendj set export <session_id>; MCP: app_state with /api/sets/{session_id}/export."
        ),
        parameters={
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "The recorded set's session id."}},
            "required": ["session_id"],
        },
        gate="read",
        run=_set_tracklist,
    ),
)
