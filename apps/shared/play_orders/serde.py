"""open-dj v0 PlayOrder round-trip.

The on-disk representation of a play-order mirrors open-dj v0
(``docs/open-dj-v0-strawman.md`` §4.6). Only the subset that is
relevant to ``PlayOrder`` + its entries lives here -- playlist-level
context (``playlist_id``) is included as a sibling key because the
open-dj schema references it for cross-document joins.

The document shape:

    {
      "schema_version": "0.1",
      "playlist_id": "...",
      "name": "...",
      "generated_by": null | "play-it" | "ai-01" | "manual",
      "goal_json": null | "...",
      "entries": [
        {
          "track_id":        "<stable_id>",
          "position":        0,
          "target_key":      null | "8A",
          "target_tempo":    null | 128.0,
          "key_sync":        null | true | false,
          "transition_hint": null | "..."
        }
      ]
    }
"""
from __future__ import annotations

from typing import Any

from .store import PlayOrder, PlayOrderEntry

OPENDJ_SCHEMA_VERSION: str = "0.1"


def _entry_to_opendj(entry: PlayOrderEntry) -> dict[str, Any]:
    return {
        "track_id": entry.stable_id,
        "position": entry.position,
        "target_key": entry.target_key,
        "target_tempo": entry.target_tempo,
        "key_sync": entry.key_sync,
        "transition_hint": entry.transition_hint,
    }


def _entry_from_opendj(data: dict[str, Any]) -> PlayOrderEntry:
    return PlayOrderEntry(
        stable_id=data["track_id"],
        position=int(data["position"]),
        target_key=data.get("target_key"),
        target_tempo=(
            None
            if data.get("target_tempo") is None
            else float(data["target_tempo"])
        ),
        key_sync=data.get("key_sync"),
        transition_hint=data.get("transition_hint"),
    )


def play_order_to_opendj(po: PlayOrder) -> dict[str, Any]:
    """Serialise a :class:`PlayOrder` to its open-dj v0 dict form."""
    return {
        "schema_version": OPENDJ_SCHEMA_VERSION,
        "playlist_id": po.playlist_id,
        "name": po.name,
        "generated_by": po.generated_by,
        "goal_json": po.goal_json,
        "entries": [_entry_to_opendj(e) for e in po.entries],
    }


def play_order_from_opendj(data: dict[str, Any]) -> PlayOrder:
    """Inverse of :func:`play_order_to_opendj`.

    Accepts a dict that *must* carry the required fields (``playlist_id``,
    ``name``, ``entries``); raises ``KeyError`` otherwise. Unknown keys
    are ignored so future extensions (the ``x_*`` convention from §7.4)
    do not break older readers.
    """
    for required in ("playlist_id", "name", "entries"):
        if required not in data:
            raise KeyError(
                f"open-dj PlayOrder missing required field {required!r}"
            )
    return PlayOrder(
        id=None,
        playlist_id=str(data["playlist_id"]),
        name=str(data["name"]),
        entries=[_entry_from_opendj(e) for e in data["entries"]],
        generated_by=data.get("generated_by"),
        goal_json=data.get("goal_json"),
    )
