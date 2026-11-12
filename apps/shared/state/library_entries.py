"""Moving tracks and sections through a playlist or set, as one journaled write.

The rules live in the pure :mod:`.library_sections`; this holds the writes, on
:mod:`.library_org`'s journal. A set is a ``kind = 'set'`` playlist, and its
sections are owned as ``('set', playlist_id)``, so every path here resolves the
owner from the row rather than taking the caller's word for it.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from . import library_sections as layout
from .library_org import (
    OrgError,
    WriteResult,
    _live,
    _operation,
    _read,
    _store,
    _Unit,
    list_rows,
)
from .library_sections import Member, Token
from .order_key import PrecisionExhausted, allocate_keys, renumbered_keys
from .writer import StateWriter


def section_owner_kind(conn: sqlite3.Connection, playlist_id: str) -> str:
    """How sections of this list are owned: ``set`` for a set, else ``playlist``."""
    row = _read(conn, "playlists", {"playlist_id": playlist_id})
    return "set" if row is not None and row["kind"] == "set" else "playlist"


def list_sections(conn: sqlite3.Connection, playlist_id: str) -> list[dict[str, Any]]:
    """A list's sections, top to bottom."""
    rows = list_rows(
        conn, "playlist_section",
        {"owner_kind": section_owner_kind(conn, playlist_id), "owner_id": playlist_id},
    )
    return [dict(row) for row in layout.ordered_sections(rows)]


def _live_members(conn: sqlite3.Connection, playlist_id: str) -> list[dict[str, Any]]:
    """Live memberships in display order, each with the key its order sorts on."""
    cursor = conn.execute(
        "SELECT position, item_id, stable_id, "
        "COALESCE(order_key, printf('%08d', position)) AS sort_key "
        "FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL "
        "ORDER BY sort_key, position",
        (playlist_id,),
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]


def _token_id(member: Mapping[str, Any]) -> str:
    """A membership's id. Importer rows with no item_id go by slot."""
    return member["item_id"] or f"position:{member['position']}"


def _new_order_keys(
    members: list[dict[str, Any]], order: list[str], block: set[str]
) -> dict[str, str]:
    """Keys for the moved block alone, or for every row if the gap is too narrow."""
    keys = {_token_id(m): m["sort_key"] for m in members}
    first = next(index for index, token_id in enumerate(order) if token_id in block)
    left = keys[order[first - 1]] if first > 0 else None
    after = first + len(block)
    right = keys[order[after]] if after < len(order) else None
    try:
        return dict(zip(order[first:after], allocate_keys(left, right, len(block)), strict=True))
    except PrecisionExhausted:
        return dict(zip(order, renumbered_keys(len(order)), strict=True))


def _place_sections(unit: _Unit, positions: Mapping[str, int]) -> None:
    """Write each section's index. Dividers on one index read in ``created_at``
    order, so a drop between them moves that too: one microsecond after the
    divider above. One put per row, because undo checks each row's last state."""
    above: tuple[int, str] | None = None
    for section_id, position in positions.items():
        stamp = _live(unit.writer._conn, "playlist_section", {"id": section_id})["created_at"]
        if above is not None and above[0] == position and stamp <= above[1]:
            stamp = (datetime.fromisoformat(above[1]) + timedelta(microseconds=1)).isoformat()
        unit.put(
            "playlist_section", {"id": section_id},
            {"before_position": position, "created_at": stamp},
        )
        above = (position, stamp)


def _place_tracks(
    unit: _Unit,
    playlist_id: str,
    members: list[dict[str, Any]],
    order: list[str],
    block: set[str],
) -> None:
    if order == [_token_id(m) for m in members]:
        return
    slots = {_token_id(m): m["position"] for m in members}
    for token_id, order_key in _new_order_keys(members, order, block).items():
        unit.put(
            "playlist_memberships",
            {"playlist_id": playlist_id, "position": slots[token_id]},
            {"order_key": order_key},
        )


def _target(before_item_id: str | None, before_section_id: str | None) -> Token | None:
    if before_item_id is not None and before_section_id is not None:
        raise OrgError("VALIDATION", "name one target line, not two")
    if before_item_id is not None:
        return Token("track", before_item_id)
    if before_section_id is not None:
        return Token("section", before_section_id)
    return None


def move_entries(
    writer: StateWriter,
    session_id: str,
    playlist_id: str,
    *,
    item_ids: list[str],
    section_ids: list[str],
    before_item_id: str | None = None,
    before_section_id: str | None = None,
) -> WriteResult:
    """Move tracks and sections as one block, above the named line or to the end.

    Sections not in the block keep their place among the tracks not in it. One
    journaled transaction, so a drop and its dividers land or undo together.
    """
    if not item_ids and not section_ids:
        raise OrgError("VALIDATION", "nothing to move")
    before = _target(before_item_id, before_section_id)
    moved = {Token("track", i) for i in item_ids} | {Token("section", s) for s in section_ids}
    with _operation(writer, session_id, "entries.move") as unit:
        conn = writer._conn
        _live(conn, "playlists", {"playlist_id": playlist_id})
        members = _live_members(conn, playlist_id)
        tokens = layout.display_tokens(
            [_token_id(m) for m in members], list_sections(conn, playlist_id)
        )
        try:
            arranged = layout.move_tokens(tokens, moved, before)
        except KeyError as exc:
            raise OrgError("NOT_FOUND", f"{exc.args[0]} is not in playlist {playlist_id}") from exc
        _place_tracks(unit, playlist_id, members, layout.track_order(arranged), set(item_ids))
        _place_sections(unit, layout.section_positions(arranged))
        if unit.after:
            unit.bump_playlist(playlist_id)
        placed = list_sections(conn, playlist_id)
    return WriteResult({"playlist_id": playlist_id, "sections": placed}, unit.undo_id)


def live_member_order(conn: sqlite3.Connection, playlist_id: str) -> list[Member]:
    """The order to hand :func:`follow_membership_change`; empty with no sections,
    so a list that uses none never pays to read its members."""
    if not list_sections(conn, playlist_id):
        return []
    return [Member(_token_id(m), m["stable_id"]) for m in _live_members(conn, playlist_id)]


def follow_membership_change(
    writer: StateWriter, playlist_id: str, before: list[Member], *, keep_groups: bool = False
) -> None:
    """Keep dividers on their tracks after a write that knows nothing of sections.

    Not journaled, because the write that caused it is not. An earlier journaled
    section change then fails undo's content check, which refuses out loud.
    """
    now = writer._now_iso()
    with writer._tx() as conn:
        after = [Member(_token_id(m), m["stable_id"]) for m in _live_members(conn, playlist_id)]
        rows = {row["id"]: row for row in list_sections(conn, playlist_id)}
        regrouped = layout.regroup_positions(before, after, list(rows.values())) if keep_groups else None
        placed = regrouped if regrouped is not None else layout.follow_members(before, after, rows.values())
        for section_id, position in placed.items():
            if rows[section_id]["before_position"] != position:
                _store(
                    writer, "playlist_section", {"id": section_id},
                    {"before_position": position}, rows[section_id], now,
                )
