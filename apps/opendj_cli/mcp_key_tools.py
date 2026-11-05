"""MCP KEY SYNC tools (KEYSYNC-01): ``deck_key_sync`` and ``deck_key_options``.

Both are thin shapes over the AGENT-03 bus, so the deck header's KEY SYNC
button, its chevron ladder, ``opendj key_sync`` / ``opendj key_shift`` and
``POST /api/v1/commands`` all reach the same engine functions
(``setKeySync`` / ``setKeyShift`` in ``audio-engine.svelte.ts``).

- ``deck_key_sync {deck, target, mode}`` arms KEY SYNC onto ``target`` (a deck,
  or ``master``) in ``mode`` (``exact`` default, ``relative``, ``compatible``).
  With ``shift`` it instead puts the deck at that absolute shift, which is
  what a ladder row does (``shift: 0`` is the one-tap reset).
- ``deck_key_options {deck, target}`` reads the page's own ladder
  (``decks.<deck>.key_sync.options.<target>`` on the UI mirror, computed by
  ``keyShiftLadder`` in ``camelot.ts``), never a second Python copy of it.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
KEY_SYNC_MODES: tuple[str, ...] = ("exact", "relative", "compatible")
DECKS: tuple[int, ...] = (1, 2, 3, 4)


def key_sync_order(deck: int, target: int | str, mode: str, shift: int | None) -> dict[str, Any]:
    """The bus order one ``deck_key_sync`` call sends; refuses a bad argument before sending."""
    if deck not in DECKS:
        raise ValueError(f"deck must be 1..4, got {deck!r}")
    if shift is not None:
        if not isinstance(shift, int) or isinstance(shift, bool) or not -12 <= shift <= 12:
            raise ValueError(f"shift must be an integer -12..12, got {shift!r}")
        return {"type": "key_shift", "deck": deck, "semitones": shift}
    if mode not in KEY_SYNC_MODES:
        raise ValueError(f"mode must be one of {', '.join(KEY_SYNC_MODES)}, got {mode!r}")
    if target != "master" and target not in DECKS:
        raise ValueError(f"target must be a deck 1..4 or 'master', got {target!r}")
    if target == deck:
        raise ValueError(f"deck {deck} cannot key sync to itself")
    return {"type": "key_sync", "deck": deck, "enabled": True, "mode": mode, "target": target}


def key_options_from_mirror(mirror: dict[str, Any], deck: int, target: int | str) -> list[dict[str, Any]]:
    """The ladder rows for ``deck`` against ``target`` from the UI mirror (``decks.<n>.key_sync.options``)."""
    decks = mirror.get("decks")
    if not isinstance(decks, dict):
        raise TypeError("UI mirror has no decks")
    if target == "master":
        target = mirror.get("master_deck")
        if target is None:
            raise ValueError("no MASTER deck is elected; name a target deck")
    key_sync = (decks.get(str(deck)) or {}).get("key_sync")
    if not isinstance(key_sync, dict):
        raise TypeError(f"deck {deck} has no key_sync on the mirror (an older page build?)")
    rows = key_sync["options"].get(str(target))
    if rows is None:
        raise ValueError(f"no ladder for deck {deck} against deck {target}: both need a loaded track with a key")
    return list(rows)


def register_key_tools(
    server: MCPServer,
    *,
    post_order: Callable[[dict[str, Any]], dict[str, Any]],
    read_mirror: Callable[[], dict[str, Any]],
    tool_error: Callable[[dict[str, Any]], ToolError],
) -> None:
    @server.tool(annotations=_MUTATING)
    def deck_key_sync(
        deck: int, target: int | str = "master", mode: str = "exact", shift: int | None = None
    ) -> dict[str, Any]:
        """KEY SYNC a deck onto another deck's key (KEYSYNC-01; Serato Pitch 'n Time style).

        ``mode`` exact (default) transposes onto the target's key by the shortest
        path, -6..+6 (the tritone tie is +6); relative lands on the target's
        Camelot number in this deck's own mode; compatible is the Pioneer family
        match. Key lock (Master Tempo) is turned on; tempo and beat phase are
        untouched. ``shift`` (-12..12) instead applies that absolute shift, as a
        ``deck_key_options`` row does; ``shift: 0`` resets to the original key.
        Read ``decks.<deck>.key_sync`` (status, shift_semitones) on the mirror after.
        """
        try:
            order = key_sync_order(deck, target, mode, shift)
        except ValueError as error:
            raise tool_error({"error": "usage", "message": str(error)}) from error
        return post_order(order)

    @server.tool(annotations=_READ_ONLY)
    def deck_key_options(deck: int, target: int | str = "master") -> dict[str, Any]:
        """The KEY SYNC ladder: every shift +6..-6 for ``deck`` against ``target``.

        Rows are ``{shift, key, pattern, desirable}``: the key the deck lands on and,
        when it mixes with the target's audible key, the Mixed In Key Camelot
        pattern (exact, relative, +1 energy boost, -1, +2 energy, +7 dominant,
        +5 subdominant, mood shift A<->B). Apply a row with ``deck_key_sync``
        ``shift``.
        """
        try:
            rows = key_options_from_mirror(read_mirror(), deck, target)
        except (TypeError, ValueError) as error:
            raise tool_error({"error": "unavailable", "message": str(error)}) from error
        return {"deck": deck, "target": target, "options": rows}
