"""Open DJ's own play log: one ``events`` row per track played in Open DJ.

PLAYS-01. The library's play count used to be rekordbox ``DJPlayCount`` alone,
so a track played only in Open DJ read 0 forever. Open DJ already measures
what the room heard (``apps/webui/frontend/src/lib/sets/deck-audibility.ts``),
but the set recorder only banks it while REC is running. This log is the
always-on half: the browser posts one play per deck load once that load has
been audible for :data:`PLAY_THRESHOLD_S`, and the library adds these to the
imported count.

Storage is the state layer's ``events`` table, ``kind = 'track_played'``. Each
play is one row (insert only, never updated in place); a delete removes the
whole row so counts stay re-derivable. The table is
``machine_local`` in the CloudSync register, so each machine counts its own
plays until the event log syncs (a known limit, recorded in PLAYS-01).

``play_id`` makes a post idempotent: the browser retries a failed post, and a
retry that did land the first time must not count twice.

Imported plays (DJIMPORT-10066 Serato sessions, DJIMPORT-10069 Traktor
history): another app's history lands in the same table through
:func:`import_plays`, each row labeled with a ``source`` key and stamped with
the play's original time. They skip the audibility threshold: that app's own
"played" verdict is the one being imported. Rows the browser writes carry no
``source`` and read as :data:`OPENDJ_SOURCE`.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from apps.shared.state.sync_stamp import canonical_from

PLAY_KIND = "track_played"
OPENDJ_SOURCE = "opendj"

#: Audible seconds after which a deck load counts as a play. Same default as
#: the set analytics read filter (``apps.play_analytics.query
#: .DEFAULT_MIN_AUDIBLE_S``) and (private path),
#: so "played" means one thing across sets and the library. The browser
#: applies it before posting; the server refuses a post below it.
PLAY_THRESHOLD_S = 60.0

#: Local hour at which one history day rolls over to the next. A set running past
#: midnight stays one day (rekordbox splits by session; Open DJ by night).
HISTORY_DAY_ROLLOVER_HOUR = 6

_SQL_CHUNK = 500


@dataclass(frozen=True)
class DeletedPlays:
    """Result of deleting plays in a time window."""

    count: int
    stable_ids: tuple[str, ...]


@dataclass(frozen=True)
class OwnPlays:
    """Open DJ plays of one track."""

    count: int
    last_played_at: str | None


@dataclass(frozen=True)
class ImportedPlay:
    """One play another app logged, with its original time and a stable id."""

    stable_id: str
    play_id: str
    ts: str
    audible_s: float | None = None
    deck: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    #: Per-play source label; :func:`import_plays` uses it when no ``source`` is passed.
    source: str | None = field(default=None, kw_only=True)
    #: The file the play was read from (Traktor history).
    origin: str | None = field(default=None, kw_only=True)
    #: Seconds the source app says the track played, when ``audible_s`` is unset.
    played_s: float | None = field(default=None, kw_only=True)


def _chunked(seq: Sequence[str]) -> Iterator[Sequence[str]]:
    for i in range(0, len(seq), _SQL_CHUNK):
        yield seq[i : i + _SQL_CHUNK]


def play_recorded(conn: sqlite3.Connection, stable_id: str, play_id: str) -> bool:
    """Whether this ``play_id`` is already logged for this track."""
    row = conn.execute(
        "SELECT 1 FROM events WHERE stable_id = ? AND kind = ? "
        "AND json_extract(payload_json, '$.play_id') = ? LIMIT 1",
        (stable_id, PLAY_KIND, play_id),
    ).fetchone()
    return row is not None


def play_payload(
    *, play_id: str, audible_s: float, deck: int | None, duration_ms: int | None
) -> dict[str, Any]:
    """The ``events.payload_json`` body of one play row."""
    if audible_s < PLAY_THRESHOLD_S:
        raise ValueError(
            f"audible_s {audible_s} is below the {PLAY_THRESHOLD_S} s play threshold"
        )
    return {
        "play_id": play_id,
        "audible_s": round(float(audible_s), 3),
        "deck": deck,
        "duration_ms": duration_ms,
        "threshold_s": PLAY_THRESHOLD_S,
    }


def imported_play_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    """``play_id``s already logged from ``source``, across every track."""
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.play_id') FROM events "
        "WHERE kind = ? AND json_extract(payload_json, '$.source') = ?",
        (PLAY_KIND, source),
    )
    return {str(play_id) for (play_id,) in rows}


def bulk_own_plays(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> dict[str, OwnPlays]:
    """Play count and latest play per track, every play the log holds (Open DJ's
    own and imported ones); tracks with none are absent."""
    ids = list(dict.fromkeys(stable_ids))
    out: dict[str, OwnPlays] = {}
    for chunk in _chunked(ids):
        placeholders = ",".join("?" * len(chunk))
        for sid, count, last in conn.execute(
            "SELECT stable_id, COUNT(*), MAX(ts) FROM events "
            f"WHERE kind = ? AND stable_id IN ({placeholders}) GROUP BY stable_id",
            (PLAY_KIND, *chunk),
        ):
            out[str(sid)] = OwnPlays(count=int(count), last_played_at=last)
    return out


def record_play(
    conn: sqlite3.Connection,
    stable_id: str,
    *,
    play_id: str,
    audible_s: float,
    deck: int | None,
    duration_ms: int | None,
    ts: str,
    actor: str,
) -> bool:
    """Log one play; False when this ``play_id`` was already logged.

    The caller owns the transaction (``BEGIN IMMEDIATE`` so the duplicate
    check and the insert cannot interleave with a concurrent retry).
    """
    payload = play_payload(
        play_id=play_id, audible_s=audible_s, deck=deck, duration_ms=duration_ms
    )
    if play_recorded(conn, stable_id, play_id):
        return False
    conn.execute(
        "INSERT INTO events(ts, kind, stable_id, payload_json, actor) VALUES (?, ?, ?, ?, ?)",
        (
            ts,
            PLAY_KIND,
            stable_id,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            actor,
        ),
    )
    return True


def history_day_bounds(day: date, tz: tzinfo | None = None) -> tuple[str, str]:
    """Canonical UTC ``[start, end)`` for one history day: local 06:00 on ``day`` to 06:00 the next.

    ``tz=None`` uses the machine local zone; each bound is its own wall-time ``astimezone()``
    so a DST fall-back night yields a 25-hour window instead of pinning the start offset.
    """
    if tz is None:
        start_local = datetime(day.year, day.month, day.day, HISTORY_DAY_ROLLOVER_HOUR).astimezone()
        next_day = day + timedelta(days=1)
        end_local = datetime(
            next_day.year, next_day.month, next_day.day, HISTORY_DAY_ROLLOVER_HOUR
        ).astimezone()
        return canonical_from(start_local), canonical_from(end_local)
    next_day = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, HISTORY_DAY_ROLLOVER_HOUR, tzinfo=tz)
    end = datetime(next_day.year, next_day.month, next_day.day, HISTORY_DAY_ROLLOVER_HOUR, tzinfo=tz)
    return canonical_from(start), canonical_from(end)


def delete_play(conn: sqlite3.Connection, stable_id: str, play_id: str) -> bool:
    """Remove one logged play; ``False`` when this track has no play with this id."""
    cur = conn.execute(
        "DELETE FROM events WHERE kind = ? AND stable_id = ? "
        "AND json_extract(payload_json, '$.play_id') = ?",
        (PLAY_KIND, stable_id, play_id),
    )
    return cur.rowcount > 0


def delete_plays_between(conn: sqlite3.Connection, start_ts: str, end_ts: str) -> DeletedPlays:
    """Remove every play with ``start_ts <= ts < end_ts``; return count and touched stable_ids."""
    stable_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT stable_id FROM events WHERE kind = ? AND ts >= ? AND ts < ? ORDER BY stable_id",
            (PLAY_KIND, start_ts, end_ts),
        )
    ]
    cur = conn.execute(
        "DELETE FROM events WHERE kind = ? AND ts >= ? AND ts < ?",
        (PLAY_KIND, start_ts, end_ts),
    )
    return DeletedPlays(count=cur.rowcount, stable_ids=tuple(stable_ids))


def plays_by_source(conn: sqlite3.Connection, stable_id: str) -> dict[str, OwnPlays]:
    """Plays of one track keyed by source; rows with no ``source`` are Open DJ's."""
    return {
        str(source): OwnPlays(count=int(count), last_played_at=last)
        for source, count, last in conn.execute(
            "SELECT COALESCE(json_extract(payload_json, '$.source'), ?), COUNT(*), MAX(ts) "
            "FROM events WHERE kind = ? AND stable_id = ? GROUP BY 1",
            (OPENDJ_SOURCE, PLAY_KIND, stable_id),
        )
    }


def import_plays(
    conn: sqlite3.Connection,
    plays: Sequence[ImportedPlay],
    *,
    actor: str,
    source: str | None = None,
) -> int:
    """Log plays from another app; returns rows written, skipping logged ``play_id`` s.

    ``source`` labels every play; when omitted each play's own ``source`` does.
    A ``play_id`` already logged for the track, or from the same source on any
    track, is skipped. The 60 s threshold is not re-applied: the source app's
    own "played" verdict is the record, and ``threshold_s: null`` in the row
    says so. The caller owns the transaction, as for :func:`record_play`.
    """
    if source == OPENDJ_SOURCE:
        raise ValueError("imported plays cannot carry Open DJ's own source label")
    seen: dict[str, set[str]] = {}
    written = 0
    for play in plays:
        label = source if source is not None else play.source
        if label is None:
            raise ValueError(f"imported play {play.play_id!r} has no source label")
        if label == OPENDJ_SOURCE:
            raise ValueError("imported plays cannot carry Open DJ's own source label")
        ids = seen.setdefault(label, imported_play_ids(conn, label))
        if play.play_id in ids or play_recorded(conn, play.stable_id, play.play_id):
            continue
        audible = play.audible_s if play.audible_s is not None else play.played_s
        payload = {
            **play.detail,
            **({} if play.origin is None else {"origin": play.origin}),
            "play_id": play.play_id,
            "source": label,
            "audible_s": None if audible is None else round(float(audible), 3),
            "deck": play.deck,
            "duration_ms": None,
            "threshold_s": None,
        }
        conn.execute(
            "INSERT INTO events(ts, kind, stable_id, payload_json, actor) VALUES (?, ?, ?, ?, ?)",
            (play.ts, PLAY_KIND, play.stable_id,
             json.dumps(payload, sort_keys=True, separators=(",", ":")), actor),
        )
        ids.add(play.play_id)
        written += 1
    return written


__all__ = [
    "HISTORY_DAY_ROLLOVER_HOUR",
    "OPENDJ_SOURCE",
    "PLAY_KIND",
    "PLAY_THRESHOLD_S",
    "DeletedPlays",
    "ImportedPlay",
    "OwnPlays",
    "bulk_own_plays",
    "delete_play",
    "delete_plays_between",
    "history_day_bounds",
    "import_plays",
    "imported_play_ids",
    "play_payload",
    "play_recorded",
    "plays_by_source",
    "record_play",
]
