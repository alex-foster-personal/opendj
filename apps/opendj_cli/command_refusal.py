"""#12280: the refusal a page recorded for its last performance command, as ``opendj state`` prints it.

The page publishes ``last_error`` (the human toast text) and ``last_error_code``
(its stable code, ``apps/webui/frontend/src/lib/rb/performance-refusal-code.ts``)
at the top of the UI mirror, and ``command_error`` / ``command_error_code`` per
deck. ``opendj state --json`` and the MCP ``app_state`` tool return those fields
verbatim; this module is the text form, so every surface reports the same code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _refusal_text(code: Any, message: Any) -> str:
    return "none" if message is None else f"{code} - {message}"


def command_refusal_lines(mirror: Mapping[str, Any]) -> list[str]:
    """The command refusal lines for ``opendj state``: global first, then each deck holding one."""
    if "last_error_code" not in mirror:
        # A page older than #12280 publishes no code, so say so instead of reading it as no refusal.
        return ["command refusal: absent"]
    lines = [f"command refusal: {_refusal_text(mirror['last_error_code'], mirror.get('last_error'))}"]
    decks = mirror.get("decks")
    if isinstance(decks, Mapping):
        for deck_id in sorted(decks, key=str):
            deck = decks[deck_id]
            if isinstance(deck, Mapping) and deck.get("command_error") is not None:
                text = _refusal_text(deck.get("command_error_code"), deck["command_error"])
                lines.append(f"deck {deck_id} command refusal: {text}")
    return lines
