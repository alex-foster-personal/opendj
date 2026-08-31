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

from apps.shared.state import sync_stamp
from apps.shared.state.writer import immediate_transaction, next_playlist_revision

from .client import SpotifyTrack
from .matcher_adapter import (
    LocalTrack,
    MatchedPair,
    MatchResult,
    load_local_tracks,
    match_spotify_tracks,
)
from .state_writer import (
    MEMBERSHIPS_TABLE,
    PLAYLISTS_TABLE,
    PendingRow,
    ensure_aux_tables,
    fetch_pending_tracks,
)

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

    # Round 2 finding N1c: the promotion below wrote playlist_memberships and
    # playlists with no origin_device_id and no local_changelog entry, so a
    # resolved purchase never reached the hub and every later sync failed its
    # digest compare. Both tables now go through the shared chokepoint.
    machine_id = sync_stamp.ensure_local_machine(conn)
    with immediate_transaction(conn):
        now = sync_stamp.canonical_now()
        for pending, _pair, target in resolved_pairs:
            inserted = conn.execute(
                """
                INSERT INTO playlist_memberships
                  (playlist_id, stable_id, position, updated_at,
                   origin_device_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (playlist_id, target.stable_id, pending.position, now, machine_id),
            )
            # Only a row that actually landed is logged: an ON CONFLICT skip
            # changed nothing, and a changelog entry for it would re-offer an
            # untouched row on every sync forever.
            if inserted.rowcount == 1:
                sync_stamp.stamp_and_log(
                    conn,
                    MEMBERSHIPS_TABLE,
                    (playlist_id, pending.position),
                    machine_id,
                    now=now,
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
        revision = next_playlist_revision(conn, playlist_id, now)
        stamp = sync_stamp.stamp_and_log(
            conn, PLAYLISTS_TABLE, (playlist_id,), machine_id, now=revision,
        )
        updated = conn.execute(
            "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
            "WHERE playlist_id = ?",
            (stamp.updated_at, stamp.origin_device_id, playlist_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"playlist not found during rematch: {playlist_id}")

    return outcome


def _count_abandoned(conn: sqlite3.Connection, playlist_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM pending_tracks "
        "WHERE playlist_id = ? AND status = 'abandoned'",
        (playlist_id,),
    ).fetchone()
    return int(row[0]) if row else 0
