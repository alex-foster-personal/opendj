"""Open DJ's own play log: one ``events`` row per track played in Open DJ.

PLAYS-01. The library's play count used to be rekordbox ``DJPlayCount`` alone,
so a track played only in Open DJ read 0 forever. Open DJ already measures
what the room heard (``apps/webui/frontend/src/lib/sets/deck-audibility.ts``),
but the set recorder only banks it while REC is running. This log is the
always-on half: the browser posts one play per deck load once that load has
been audible for :data:`PLAY_THRESHOLD_S`, and the library adds these to the
imported count.

Storage is the state layer's append-only ``events`` table, ``kind =
'track_played'``. It needs no migration and keeps every play as a row, so a
count is re-derivable and nothing is ever incremented in place. The table is
``machine_local`` in the CloudSync register, so each machine counts its own
plays until the event log syncs (a known limit, recorded in PLAYS-01).

``play_id`` makes a post idempotent: the browser retries a failed post, and a
retry that did land the first time must not count twice.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

PLAY_KIND = "track_played"

#: Audible seconds after which a deck load counts as a play. Same default as
#: the set analytics read filter (``apps.play_analytics.query
#: .DEFAULT_MIN_AUDIBLE_S``) and docs/product/set-dwell-threshold-analysis.md,
#: so "played" means one thing across sets and the library. The browser
#: applies it before posting; the server refuses a post below it.
PLAY_THRESHOLD_S = 60.0

_SQL_CHUNK = 500


@dataclass(frozen=True)
class OwnPlays:
    """Open DJ plays of one track."""

    count: int
    last_played_at: str | None


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


def bulk_own_plays(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> dict[str, OwnPlays]:
    """Open DJ play count and latest play per track; tracks with none are absent."""
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


__all__ = [
    "PLAY_KIND",
    "PLAY_THRESHOLD_S",
    "OwnPlays",
    "bulk_own_plays",
    "play_payload",
    "play_recorded",
    "record_play",
]
