"""The Tag List: one ordered prep list of up to 100 tracks (rekordbox Tag List parity, issue #10028).

rekordbox keeps a temporary Tag List: add tracks from any row, browse it, Save as
playlist, Clear. Open DJ keeps the same list in ``state.db`` so it survives a restart.

Storage: the state layer's ``events`` table, ``kind = 'taglist_snapshot'``, one row
holding the whole ordered list as ``{"stable_ids": [...]}``. Each write replaces the
snapshot in one transaction (delete old rows, insert the new one), so no schema
migration is needed and the latest row is the list. Like the play log the table is
machine local. A track deleted from the library drops out on read, and the next write
prunes it.

The caller owns the transaction (``BEGIN IMMEDIATE``) for every write, as for
:mod:`apps.shared.state.play_log`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

TAGLIST_KIND = "taglist_snapshot"

#: rekordbox's Tag List cap (mimicked).
TAGLIST_CAP = 100

_SQL_CHUNK = 500


class TagListFullError(ValueError):
    """Adding these tracks would take the Tag List past :data:`TAGLIST_CAP`."""

    def __init__(self, current: int, adding: int) -> None:
        super().__init__(
            f"the Tag List holds at most {TAGLIST_CAP} tracks; it has {current} and "
            f"{adding} more would make {current + adding}"
        )
        self.current = current
        self.adding = adding


class UnknownTracksError(ValueError):
    """Some stable_ids name no live track."""

    def __init__(self, stable_ids: Sequence[str]) -> None:
        super().__init__(f"unknown or deleted tracks ({len(stable_ids)}): {list(stable_ids)[:5]}")
        self.stable_ids = tuple(stable_ids)


@dataclass(frozen=True)
class TagListAdd:
    """Result of an add: the ids newly appended and the list after it."""

    added: tuple[str, ...]
    items: tuple[str, ...]


def appended(current: Sequence[str], new: Sequence[str], cap: int = TAGLIST_CAP) -> tuple[str, ...]:
    """``current`` plus the ids of ``new`` not already in it, in order; raises past ``cap``."""
    seen = set(current)
    extra: list[str] = []
    for stable_id in new:
        if stable_id not in seen:
            seen.add(stable_id)
            extra.append(stable_id)
    if len(current) + len(extra) > cap:
        raise TagListFullError(len(current), len(extra))
    return (*current, *extra)


def _stored_ids(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute(
        "SELECT payload_json FROM events WHERE kind = ? ORDER BY id DESC LIMIT 1", (TAGLIST_KIND,)
    ).fetchone()
    if row is None:
        return []
    return [str(sid) for sid in json.loads(row[0])["stable_ids"]]


def _live(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> set[str]:
    unique = list(dict.fromkeys(stable_ids))
    live: set[str] = set()
    for i in range(0, len(unique), _SQL_CHUNK):
        chunk = unique[i : i + _SQL_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        live.update(
            str(sid)
            for (sid,) in conn.execute(
                f"SELECT stable_id FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({placeholders})",
                chunk,
            )
        )
    return live


def _write(conn: sqlite3.Connection, stable_ids: Sequence[str], *, ts: str, actor: str) -> None:
    conn.execute("DELETE FROM events WHERE kind = ?", (TAGLIST_KIND,))
    conn.execute(
        "INSERT INTO events(ts, kind, stable_id, payload_json, actor) VALUES (?, ?, NULL, ?, ?)",
        (ts, TAGLIST_KIND, json.dumps({"stable_ids": list(stable_ids)}, separators=(",", ":")), actor),
    )


def read_taglist(conn: sqlite3.Connection) -> list[str]:
    """The Tag List in order, live tracks only (a track deleted from the library drops out)."""
    stored = _stored_ids(conn)
    live = _live(conn, stored)
    return [sid for sid in stored if sid in live]


def add_tracks(conn: sqlite3.Connection, stable_ids: Sequence[str], *, ts: str, actor: str) -> TagListAdd:
    """Append live tracks not already listed; all or nothing past the cap or on an unknown id."""
    live = _live(conn, stable_ids)
    unknown = [sid for sid in dict.fromkeys(stable_ids) if sid not in live]
    if unknown:
        raise UnknownTracksError(unknown)
    current = read_taglist(conn)
    items = appended(current, stable_ids)
    if list(items) != _stored_ids(conn):
        _write(conn, items, ts=ts, actor=actor)
    return TagListAdd(added=items[len(current) :], items=items)


def remove_track(conn: sqlite3.Connection, stable_id: str, *, ts: str, actor: str) -> bool:
    """Drop one track from the Tag List; False when it is not listed."""
    current = read_taglist(conn)
    if stable_id not in current:
        return False
    _write(conn, [sid for sid in current if sid != stable_id], ts=ts, actor=actor)
    return True


def clear_taglist(conn: sqlite3.Connection) -> int:
    """Empty the Tag List; returns how many live tracks it held."""
    count = len(read_taglist(conn))
    conn.execute("DELETE FROM events WHERE kind = ?", (TAGLIST_KIND,))
    return count


__all__ = [
    "TAGLIST_CAP",
    "TAGLIST_KIND",
    "TagListAdd",
    "TagListFullError",
    "UnknownTracksError",
    "add_tracks",
    "appended",
    "clear_taglist",
    "read_taglist",
    "remove_track",
]
