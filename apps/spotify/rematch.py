"""Post-acquisition rematch (CAT-01b).

DJ buys + downloads a track, drops it in the library, Phase 6 ingests
it into state-layer ``tracks``. Then rematch promotes the pending row
into ``playlist_memberships`` and marks it ``resolved``.

No filesystem watching in v1; explicit re-run. Safety mirrors
:mod:`state_writer`: dry-run default, single transaction, backup +
reversal handled by the caller.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .client import SpotifyTrack
from .matcher_adapter import (
    LocalTrack,
    MatchedPair,
    MatchResult,
    load_local_tracks,
    match_spotify_tracks,
)
from .state_writer import PendingRow, ensure_aux_tables, fetch_pending_tracks

__all__ = ["RematchOutcome", "rematch_playlist"]


@dataclass
class RematchOutcome:
    playlist_id: str
    resolved: list[tuple[PendingRow, LocalTrack]] = field(default_factory=list)
    still_pending: list[PendingRow] = field(default_factory=list)
    abandoned_before: int = 0
    live: bool = False

    @property
    def resolved_count(self) -> int:
        return len(self.resolved)

    @property
    def still_pending_count(self) -> int:
        return len(self.still_pending)


def _pending_as_source(row: PendingRow) -> SpotifyTrack:
    """Project a pending row back into a synthetic :class:`SpotifyTrack`.

    Lets the rematch path reuse the same matcher. ``is_local=False``
    because the source was Spotify; the fact that it's pending does not
    change that.
    """
    artists = tuple(a.strip() for a in row.artist.split(",") if a.strip()) or (row.artist,)
    return SpotifyTrack(
        spotify_id=None,
        spotify_uri=row.spotify_uri,
        isrc=row.isrc,
        title=row.title,
        artists=artists,
        album=row.album or "",
        duration_ms=row.duration_ms or 0,
        is_local=False,
    )


def rematch_playlist(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    live: bool = False,
) -> RematchOutcome:
    """Promote pending rows for ``playlist_id`` into memberships."""
    ensure_aux_tables(conn)

    pendings = fetch_pending_tracks(conn, playlist_id, status="pending")
    local_tracks = load_local_tracks(conn)

    outcome = RematchOutcome(playlist_id=playlist_id, live=live)
    outcome.abandoned_before = _count_abandoned(conn, playlist_id)

    if not pendings:
        return outcome

    synthetic_sources = [_pending_as_source(p) for p in pendings]
    result: MatchResult = match_spotify_tracks(synthetic_sources, local_tracks)

    resolved_pairs: list[tuple[PendingRow, MatchedPair, LocalTrack]] = []
    still: list[PendingRow] = []
    for pending, pair in zip(pendings, result.pairs):
        if pair.status == "matched" and pair.target is not None:
            resolved_pairs.append((pending, pair, pair.target))
        else:
            still.append(pending)

    outcome.resolved = [(p, tgt) for p, _pair, tgt in resolved_pairs]
    outcome.still_pending = still

    if not live or not resolved_pairs:
        return outcome

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN")
    try:
        for pending, _pair, target in resolved_pairs:
            conn.execute(
                """
                INSERT INTO playlist_memberships
                  (playlist_id, stable_id, position)
                VALUES (?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (playlist_id, target.stable_id, pending.position),
            )
            conn.execute(
                """
                UPDATE pending_tracks
                SET status = 'resolved',
                    resolved_stable_id = ?,
                    resolved_at = ?
                WHERE pending_id = ?
                """,
                (target.stable_id, now, pending.pending_id),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return outcome


def _count_abandoned(conn: sqlite3.Connection, playlist_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM pending_tracks "
        "WHERE playlist_id = ? AND status = 'abandoned'",
        (playlist_id,),
    ).fetchone()
    return int(row[0]) if row else 0
