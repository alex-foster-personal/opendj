"""PLAY-03 per-track override write path."""
from __future__ import annotations

import sqlite3

from apps.shared.play_orders.events import emit_play_order_changed
from apps.shared.play_orders.validation import (
    validate_key_sync,
    validate_target_key,
    validate_target_tempo,
)

_UNSET = object()


def _lookup_parent(
    conn: sqlite3.Connection, play_order_id: int, position: int
) -> tuple[str, str]:
    row = conn.execute(
        "SELECT po.playlist_id, po.name "
        "FROM play_orders po "
        "JOIN play_order_entries poe ON poe.play_order_id = po.id "
        "WHERE po.id=? AND poe.position=?",
        (play_order_id, position),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"no play_order_entries row for "
            f"play_order_id={play_order_id}, position={position}"
        )
    return str(row[0]), str(row[1])


def set_override(
    conn: sqlite3.Connection,
    play_order_id: int,
    position: int,
    *,
    target_key: str | None | object = _UNSET,
    target_tempo: float | None | object = _UNSET,
    key_sync: bool | None | object = _UNSET,
    transition_hint: str | None | object = _UNSET,
) -> None:
    """Patch one or more override fields; at least one required."""
    changes: list[tuple[str, object]] = []
    if target_key is not _UNSET:
        validate_target_key(target_key)  # type: ignore[arg-type]
        changes.append(("target_key", target_key))
    if target_tempo is not _UNSET:
        validate_target_tempo(target_tempo)  # type: ignore[arg-type]
        changes.append(("target_tempo", target_tempo))
    if key_sync is not _UNSET:
        validate_key_sync(key_sync)  # type: ignore[arg-type]
        changes.append(
            ("key_sync", None if key_sync is None else int(bool(key_sync)))
        )
    if transition_hint is not _UNSET:
        if transition_hint is not None and not isinstance(transition_hint, str):
            raise ValueError(
                f"transition_hint must be str or None, got {transition_hint!r}"
            )
        changes.append(("transition_hint", transition_hint))

    if not changes:
        raise ValueError("set_override requires at least one field to change")

    playlist_id, name = _lookup_parent(conn, play_order_id, position)
    assignments = ", ".join(f"{col}=?" for col, _ in changes)
    params: list[object] = [val for _, val in changes]
    params.extend([play_order_id, position])
    conn.execute(
        f"UPDATE play_order_entries SET {assignments} "
        "WHERE play_order_id=? AND position=?",
        params,
    )
    emit_play_order_changed(
        conn, playlist_id=playlist_id, name=name, action="updated"
    )


def clear_override(
    conn: sqlite3.Connection,
    play_order_id: int,
    position: int,
) -> None:
    """Blank out every override field on one entry."""
    playlist_id, name = _lookup_parent(conn, play_order_id, position)
    conn.execute(
        "UPDATE play_order_entries SET target_key=NULL, target_tempo=NULL, "
        "key_sync=NULL, transition_hint=NULL "
        "WHERE play_order_id=? AND position=?",
        (play_order_id, position),
    )
    emit_play_order_changed(
        conn, playlist_id=playlist_id, name=name, action="updated"
    )
