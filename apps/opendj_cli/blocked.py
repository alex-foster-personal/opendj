"""AGENT-8332/54: read a page's playing-deck refusal and say it plainly.

The page refuses to replace the track on a deck that is still sounding
(``assertDeckReplacementAllowed`` in
``apps/webui/frontend/src/lib/rb/audio-engine-guards.ts``) and reports that
step as ``{"status": "failed", "code": "deck_playing", "detail": {...}}``
(``agent-load-refusal.ts``). This module turns that into one summary the CLI
and the MCP ``command`` tool both return, including the exact override, so an
agent is told WHY nothing loaded and what to send if the user meant it.

A ``replace_playing`` load that went through answers
``{"status": "succeeded", "result": {"replaced_playing": {...}}}``, naming the
track it stopped; :func:`replaced_tracks` reads that back.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from apps.opendj_cli.client import MalformedResult

DECK_PLAYING = "deck_playing"
REPLACE_PLAYING_ARG = "replace_playing=true"

#: Said to every agent that meets the refusal (MCP tool text repeats it).
JUDGEMENT_NOTE = (
    "Replacing it stops music the room may be hearing. Use the override only when "
    "the user's request already implies replacing a playing deck (for example "
    "'load four decks now'); if that is unclear, check in with the user first."
)


def _detail(step: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = step.get("detail")
    if not isinstance(detail, Mapping) or not isinstance(detail.get("deck"), int):
        raise MalformedResult(f"a {DECK_PLAYING} step carried no deck detail: {step!r}")
    return detail


def override_cli(override: Mapping[str, Any] | None) -> str | None:
    """The CLI line that sends ``override``, or None when there is none."""
    if override is None:
        return None
    return f"opendj load {override['deck']} {override['stable_id']} {REPLACE_PLAYING_ARG}"


def format_position(position_ms: Any) -> str:
    if not isinstance(position_ms, (int, float)) or isinstance(position_ms, bool):
        return "an unknown position"
    seconds = int(position_ms // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def blocked_steps(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One summary per step the page refused as ``deck_playing``."""
    summaries: list[dict[str, Any]] = []
    for step in steps:
        if step.get("code") != DECK_PLAYING:
            continue
        detail = _detail(step)
        override = detail.get("override")
        summaries.append(
            {
                "reason": DECK_PLAYING,
                "deck": detail["deck"],
                "stable_id": detail.get("stable_id"),
                "title": detail.get("title"),
                "artist": detail.get("artist"),
                "position_ms": detail.get("position_ms"),
                "active_state": list(detail.get("active_state") or []),
                "override_order": None if override is None else {"single": dict(override)},
                "override_cli": override_cli(override),
                "note": JUDGEMENT_NOTE,
            }
        )
    return summaries


def replaced_tracks(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What each ``replace_playing`` load stopped, in step order."""
    replaced: list[dict[str, Any]] = []
    for step in steps:
        result = step.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("replaced_playing"), Mapping):
            replaced.append(dict(result["replaced_playing"]))
    return replaced


def blocked_line(summary: Mapping[str, Any]) -> str:
    """The human verdict for one refusal, ending with the override to send."""
    title = summary.get("title") or summary.get("stable_id") or "a track"
    line = (
        f'deck {summary["deck"]} is playing "{title}" at '
        f"{format_position(summary.get('position_ms'))} "
        f"({', '.join(summary['active_state']) or 'active'}); nothing was loaded."
    )
    if summary["override_cli"] is None:
        return f"{line} Pause deck {summary['deck']} first, then retry. {JUDGEMENT_NOTE}"
    return f"{line} To replace it anyway (stops the deck first): {summary['override_cli']}. {JUDGEMENT_NOTE}"


def replaced_line(replaced: Mapping[str, Any]) -> str:
    title = replaced.get("title") or replaced.get("stable_id") or "a track"
    return (
        f'replaced playing deck {replaced.get("deck")}: stopped "{title}" at '
        f"{format_position(replaced.get('position_ms'))}"
    )


def annotate_blocked(document: dict[str, Any]) -> dict[str, Any]:
    """The MCP ``command`` result, with ``verdict``/``blocked``/``replaced_playing`` added when they apply."""
    steps = document.get("steps")
    if not isinstance(steps, list):
        return document
    blocked = blocked_steps(steps)
    if blocked:
        document["verdict"] = "blocked"
        document["blocked"] = blocked
    replaced = replaced_tracks(steps)
    if replaced:
        document["replaced_playing"] = replaced
    return document
