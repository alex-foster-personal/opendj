"""Journaled CRUD for non-destructive virtual track edits (LIBEDIT-9179)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

from . import library_org as org
from .order_key import PrecisionExhausted, between
from .writer import StateWriter


def _source(conn: sqlite3.Connection, source_track_id: str) -> dict[str, Any]:
    cursor = conn.execute(
        "SELECT * FROM tracks WHERE stable_id = ? AND deleted_at IS NULL",
        (source_track_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise org.OrgError("NOT_FOUND", f"source track {source_track_id!r} does not exist")
    return dict(zip((column[0] for column in cursor.description), tuple(row), strict=True))


def _checked_window(source: Mapping[str, Any], in_ms: object, out_ms: object) -> tuple[int, int]:
    if (
        isinstance(in_ms, bool)
        or isinstance(out_ms, bool)
        or not isinstance(in_ms, int)
        or not isinstance(out_ms, int)
        or in_ms < 0
        or in_ms >= out_ms
    ):
        raise org.OrgError("VALIDATION", "edit window must satisfy 0 <= in_ms < out_ms")
    duration = source.get("duration_ms")
    if not isinstance(duration, int) or duration < 0:
        raise org.OrgError("VALIDATION", "source duration is unknown")
    if out_ms > duration:
        raise org.OrgError("VALIDATION", "out_ms must not exceed source duration")
    return in_ms, out_ms


def _checked_fx(value: object) -> str:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise org.OrgError("VALIDATION", "fx must be a JSON object")
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise org.OrgError("VALIDATION", "fx must contain JSON values") from exc


def _display(row: Mapping[str, Any], duration_ms: int | None) -> dict[str, Any]:
    out = dict(row)
    try:
        parsed = json.loads(str(out["fx"]))
    except (TypeError, ValueError):
        parsed = {}
    out["fx"] = parsed if isinstance(parsed, dict) else {}
    out["valid"] = duration_ms is not None and int(out["out_ms"]) <= duration_ms
    return out


def _live_edit(conn: sqlite3.Connection, edit_id: str) -> dict[str, Any]:
    cursor = conn.execute("SELECT * FROM track_edit WHERE id = ? AND deleted_at IS NULL", (edit_id,))
    row = cursor.fetchone()
    if row is None:
        raise org.OrgError("NOT_FOUND", f"edit {edit_id!r} does not exist")
    return dict(zip((column[0] for column in cursor.description), tuple(row), strict=True))


def _insert_membership(
    unit: org._Unit,
    *,
    playlist_id: str,
    source_track_id: str,
    edit_id: str,
    after_item_id: str | None,
) -> None:
    conn = unit.writer._conn
    playlist = conn.execute(
        "SELECT 1 FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL",
        (playlist_id,),
    ).fetchone()
    if playlist is None:
        raise org.OrgError("NOT_FOUND", f"playlist {playlist_id!r} does not exist")
    if after_item_id is None:
        anchor = conn.execute(
            "SELECT item_id, COALESCE(order_key, printf('%08d', position)) "
            "FROM playlist_memberships WHERE playlist_id = ? AND stable_id = ? "
            "AND deleted_at IS NULL ORDER BY COALESCE(order_key, printf('%08d', position)), position "
            "LIMIT 1",
            (playlist_id, source_track_id),
        ).fetchone()
    else:
        anchor = conn.execute(
            "SELECT item_id, COALESCE(order_key, printf('%08d', position)) "
            "FROM playlist_memberships WHERE playlist_id = ? AND item_id = ? "
            "AND deleted_at IS NULL",
            (playlist_id, after_item_id),
        ).fetchone()
    if anchor is None:
        raise org.OrgError("NOT_FOUND", "source membership does not exist in that playlist")
    right = conn.execute(
        "SELECT COALESCE(order_key, printf('%08d', position)) "
        "FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL "
        "AND COALESCE(order_key, printf('%08d', position)) > ? "
        "ORDER BY COALESCE(order_key, printf('%08d', position)), position LIMIT 1",
        (playlist_id, anchor[1]),
    ).fetchone()
    try:
        order_key = between(str(anchor[1]), None if right is None else str(right[0]))
    except PrecisionExhausted as exc:
        raise org.OrgError("CONFLICT", "playlist order has no room after the source") from exc
    position = int(
        conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_memberships WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()[0]
    )
    unit.put(
        "playlist_memberships",
        {"playlist_id": playlist_id, "position": position},
        {
            "stable_id": source_track_id,
            "item_id": uuid.uuid4().hex,
            "order_key": order_key,
            "edit_id": edit_id,
        },
    )
    unit.bump_playlist(playlist_id)


def create_edit(  # noqa: PLR0913 - the edit command's persisted fields are deliberately explicit
    writer: StateWriter,
    session_id: str,
    *,
    source_track_id: str,
    label: str,
    in_ms: int,
    out_ms: int,
    fx: object = None,
    created_by: str,
    playlist_id: str | None = None,
    after_item_id: str | None = None,
) -> org.WriteResult:
    """Create metadata only; never write the source row or its audio file."""
    with org._operation(writer, session_id, "edit.create") as unit:
        source = _source(writer._conn, source_track_id)
        start, end = _checked_window(source, in_ms, out_ms)
        edit_id = uuid.uuid4().hex
        row = unit.put(
            "track_edit",
            {"id": edit_id},
            {
                "source_track_id": source_track_id,
                "label": label.strip(),
                "in_ms": start,
                "out_ms": end,
                "fx": _checked_fx(fx),
                "created_by": created_by,
            },
        )
        if playlist_id is not None:
            _insert_membership(
                unit,
                playlist_id=playlist_id,
                source_track_id=source_track_id,
                edit_id=edit_id,
                after_item_id=after_item_id,
            )
    return org.WriteResult(_display(row, int(source["duration_ms"])), unit.undo_id)


def update_edit(writer: StateWriter, session_id: str, edit_id: str, changes: Mapping[str, Any]) -> org.WriteResult:
    allowed = {"label", "in_ms", "out_ms", "fx"}
    unknown = set(changes) - allowed
    if unknown:
        raise org.OrgError("VALIDATION", f"unknown fields: {sorted(unknown)}")
    if not changes:
        raise org.OrgError("VALIDATION", "at least one edit field is required")
    with org._operation(writer, session_id, "edit.update") as unit:
        current = _live_edit(writer._conn, edit_id)
        source = _source(writer._conn, str(current["source_track_id"]))
        merged = {**current, **changes}
        start, end = _checked_window(source, merged["in_ms"], merged["out_ms"])
        checked = dict(changes)
        if "label" in checked:
            checked["label"] = str(checked["label"]).strip()
        if "in_ms" in checked:
            checked["in_ms"] = start
        if "out_ms" in checked:
            checked["out_ms"] = end
        if "fx" in checked:
            checked["fx"] = _checked_fx(checked["fx"])
        row = unit.put("track_edit", {"id": edit_id}, checked)
    return org.WriteResult(_display(row, int(source["duration_ms"])), unit.undo_id)


def delete_edit(writer: StateWriter, session_id: str, edit_id: str) -> str | None:
    """Tombstone the edit and its memberships; never mutate the source track."""
    with org._operation(writer, session_id, "edit.delete") as unit:
        _live_edit(writer._conn, edit_id)
        memberships = writer._conn.execute(
            "SELECT playlist_id, position FROM playlist_memberships WHERE edit_id = ? AND deleted_at IS NULL",
            (edit_id,),
        ).fetchall()
        unit.tombstone("track_edit", {"id": edit_id})
        bumped: set[str] = set()
        for playlist_id, position in memberships:
            unit.tombstone(
                "playlist_memberships",
                {"playlist_id": str(playlist_id), "position": int(position)},
            )
            bumped.add(str(playlist_id))
        for playlist_id in bumped:
            unit.bump_playlist(playlist_id)
    return unit.undo_id


def list_edits(conn: sqlite3.Connection, source_track_id: str | None = None) -> list[dict[str, Any]]:
    params: tuple[object, ...] = ()
    where = ""
    if source_track_id is not None:
        where = " AND e.source_track_id = ?"
        params = (source_track_id,)
    cursor = conn.execute(
        "SELECT e.*, t.duration_ms FROM track_edit e "
        "LEFT JOIN tracks t ON t.stable_id = e.source_track_id AND t.deleted_at IS NULL "
        f"WHERE e.deleted_at IS NULL{where} ORDER BY e.created_at, e.id",
        params,
    )
    names = [column[0] for column in cursor.description]
    rows: list[dict[str, Any]] = []
    for raw in cursor.fetchall():
        combined = dict(zip(names, tuple(raw), strict=True))
        duration = combined.pop("duration_ms")
        dur_int: int | None = None
        if duration is not None and not isinstance(duration, bool):
            try:
                dur_int = int(duration)
            except (TypeError, ValueError):
                dur_int = None
        rows.append(_display(combined, dur_int))
    return rows


__all__ = ["create_edit", "delete_edit", "list_edits", "update_edit"]
