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
    PendingRow,
    ensure_aux_tables,
    fetch_pending_tracks,
    fetch_playlist_link,
)

__all__ = ["RematchOutcome", "rematch_playlist"]

_ResolvedTriple = tuple[PendingRow, MatchedPair, LocalTrack]


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


def _partition_matches(
    pendings: list[PendingRow],
    result: MatchResult,
) -> tuple[list[_ResolvedTriple], list[PendingRow]]:
    """Split pending rows into (resolved triples, still-pending rows)."""
    resolved_pairs: list[_ResolvedTriple] = []
    still: list[PendingRow] = []
    # strict: match_spotify_tracks returns one pair per synthetic source.
    for pending, pair in zip(pendings, result.pairs, strict=True):
        if pair.status == "matched" and pair.target is not None:
            resolved_pairs.append((pending, pair, pair.target))
        else:
            still.append(pending)
    return resolved_pairs, still


def _linked_odj_playlist_id(
    conn: sqlite3.Connection,
    playlist_id: str,
) -> str | None:
    """ODJ twin id linked to this Spotify playlist, if any."""
    vendor_pl_id = (
        playlist_id.split(":", 1)[1]
        if playlist_id.startswith("spotify:")
        else playlist_id
    )
    link = fetch_playlist_link(conn, vendor_pl_id)
    return link.odj_playlist_id if link is not None else None


def _promote_pending_row(
    conn: sqlite3.Connection,
    playlist_ids: tuple[str | None, ...],
    pending: PendingRow,
    target: LocalTrack,
    now: str,
    *,
    machine_id: str,
) -> None:
    """Swap the synthetic placeholder for the matched local track.

    ``playlist_memberships`` and ``track_vendor_ids`` are synced digest
    tables, so every write is stamped and logged (ADR 08 point 2). The
    synthetic placeholder sits at ``(playlist_id, position)`` -- the
    membership primary key -- so the matched track replaces it in place via
    ``ON CONFLICT`` rather than a hard DELETE, which keeps this off the
    ``test_soft_delete`` allowlist (finding 4a).
    """
    # Replace the synthetic placeholder at this position (Spotify + ODJ).
    for pl_id in playlist_ids:
        if pl_id is None:
            continue
        member_stamp = sync_stamp.stamp_and_log(
            conn, "playlist_memberships", (pl_id, pending.position), machine_id,
            now=now,
        )
        conn.execute(
            """
            INSERT INTO playlist_memberships
              (playlist_id, stable_id, position, updated_at, origin_device_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(playlist_id, position) DO UPDATE SET
              stable_id = excluded.stable_id,
              updated_at = excluded.updated_at,
              origin_device_id = excluded.origin_device_id,
              deleted_at = NULL
            """,
            (
                pl_id, target.stable_id, pending.position,
                member_stamp.updated_at, member_stamp.origin_device_id,
            ),
        )
    # Link local track -> Spotify id for future de-dup.
    vendor_track = None
    uri = pending.spotify_uri or ""
    if uri.startswith("spotify:track:"):
        vendor_track = uri.split(":", 2)[2] or None
    if vendor_track:
        vendor_stamp = sync_stamp.stamp_and_log(
            conn, "track_vendor_ids", (target.stable_id, "spotify"), machine_id,
            now=now,
        )
        conn.execute(
            "INSERT INTO track_vendor_ids"
            "(stable_id, vendor, vendor_id, updated_at, origin_device_id) "
            "VALUES (?, 'spotify', ?, ?, ?) "
            "ON CONFLICT(stable_id, vendor) DO UPDATE SET "
            "vendor_id=excluded.vendor_id, updated_at=excluded.updated_at, "
            "origin_device_id=excluded.origin_device_id, deleted_at=NULL",
            (
                target.stable_id, vendor_track,
                vendor_stamp.updated_at, vendor_stamp.origin_device_id,
            ),
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


def _bump_playlist_revisions(
    conn: sqlite3.Connection,
    playlist_ids: tuple[str | None, ...],
    primary_playlist_id: str,
    now: str,
    *,
    machine_id: str,
) -> None:
    for pl_id in playlist_ids:
        if pl_id is None:
            continue
        revision = next_playlist_revision(conn, pl_id, now)
        stamp = sync_stamp.stamp_and_log(
            conn, "playlists", (pl_id,), machine_id, now=revision
        )
        updated = conn.execute(
            "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
            "WHERE playlist_id = ?",
            (stamp.updated_at, stamp.origin_device_id, pl_id),
        )
        if pl_id == primary_playlist_id and updated.rowcount != 1:
            raise RuntimeError(
                f"playlist not found during rematch: {primary_playlist_id}"
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

    resolved_pairs, still = _partition_matches(pendings, result)
    outcome.resolved = [(p, tgt) for p, _pair, tgt in resolved_pairs]
    outcome.still_pending = still

    if not live or not resolved_pairs:
        return outcome

    # Round 2 finding N1c: the promotion below wrote playlist_memberships,
    # playlists and track_vendor_ids with no origin_device_id and no
    # local_changelog entry, so a resolved purchase never reached the hub and
    # every later sync failed its digest compare. All go through the shared
    # chokepoint now.
    machine_id = sync_stamp.ensure_local_machine(conn)
    with immediate_transaction(conn):
        now = sync_stamp.canonical_now()
        # Linked ODJ twin (if any) must swap the same positions.
        target_playlists = (playlist_id, _linked_odj_playlist_id(conn, playlist_id))
        for pending, _pair, target in resolved_pairs:
            _promote_pending_row(
                conn, target_playlists, pending, target, now,
                machine_id=machine_id,
            )
        _bump_playlist_revisions(
            conn, target_playlists, playlist_id, now, machine_id=machine_id
        )

    return outcome


def _count_abandoned(conn: sqlite3.Connection, playlist_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM pending_tracks "
        "WHERE playlist_id = ? AND status = 'abandoned'",
        (playlist_id,),
    ).fetchone()
    return int(row[0]) if row else 0
