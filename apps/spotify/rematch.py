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
    PendingRow,
    ensure_aux_tables,
    fetch_pending_tracks,
    fetch_playlist_link,
    synthetic_stable_id,
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
    # strict: match_spotify_tracks returns one pair per synthetic source.
    for pending, pair in zip(pendings, result.pairs, strict=True):
        if pair.status == "matched" and pair.target is not None:
            resolved_pairs.append((pending, pair, pair.target))
        else:
            still.append(pending)

    outcome.resolved = [(p, tgt) for p, _pair, tgt in resolved_pairs]
    outcome.still_pending = still

    if not live or not resolved_pairs:
        return outcome

    with immediate_transaction(conn):
        now = datetime.now(timezone.utc).isoformat()
        # Linked ODJ twin (if any) must swap the same positions.
        vendor_pl_id = (
            playlist_id.split(":", 1)[1]
            if playlist_id.startswith("spotify:")
            else playlist_id
        )
        link = fetch_playlist_link(conn, vendor_pl_id)
        odj_id = link.odj_playlist_id if link is not None else None

        for pending, _pair, target in resolved_pairs:
            synth_sid = synthetic_stable_id(pending.spotify_uri)
            # Drop the synthetic placeholder at this position (Spotify + ODJ).
            for pl_id in (playlist_id, odj_id):
                if pl_id is None:
                    continue
                conn.execute(
                    "DELETE FROM playlist_memberships "
                    "WHERE playlist_id = ? AND stable_id = ? AND position = ?",
                    (pl_id, synth_sid, pending.position),
                )
                conn.execute(
                    """
                    INSERT INTO playlist_memberships
                      (playlist_id, stable_id, position)
                    VALUES (?, ?, ?)
                    ON CONFLICT(playlist_id, position) DO UPDATE SET
                      stable_id = excluded.stable_id
                    """,
                    (pl_id, target.stable_id, pending.position),
                )
            # Link local track -> Spotify id for future de-dup.
            vendor_track = None
            uri = pending.spotify_uri or ""
            if uri.startswith("spotify:track:"):
                vendor_track = uri.split(":", 2)[2] or None
            if vendor_track:
                conn.execute(
                    "INSERT OR REPLACE INTO track_vendor_ids"
                    "(stable_id, vendor, vendor_id) VALUES (?, 'spotify', ?)",
                    (target.stable_id, vendor_track),
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
        for pl_id in (playlist_id, odj_id):
            if pl_id is None:
                continue
            revision = next_playlist_revision(conn, pl_id, now)
            updated = conn.execute(
                "UPDATE playlists SET updated_at = ? WHERE playlist_id = ?",
                (revision, pl_id),
            )
            if pl_id == playlist_id and updated.rowcount != 1:
                raise RuntimeError(f"playlist not found during rematch: {playlist_id}")

    return outcome


def _count_abandoned(conn: sqlite3.Connection, playlist_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM pending_tracks "
        "WHERE playlist_id = ? AND status = 'abandoned'",
        (playlist_id,),
    ).fetchone()
    return int(row[0]) if row else 0
