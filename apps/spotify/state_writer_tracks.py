"""Synthetic-track and vendor-id writer helpers for Spotify imports.

Split out of :mod:`apps.spotify.state_writer` (issue #1583) following the
existing ODJ-twin-mirror seam: these helpers write placeholder ``tracks``
rows and ``track_vendor_ids`` entries for Spotify tracks that have no local
match yet, plus the suggested-search-link helpers shown alongside a pending
row. ``state_writer.py`` re-imports and re-exports ``synthetic_stable_id``
and ``SUGGESTED_SOURCE_KEYS`` (its only externally-imported names) and calls
the rest directly.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from urllib.parse import quote_plus

from apps.shared.state import sync_stamp

from .client import SpotifyTrack
from .state_aux import VENDOR


def synthetic_stable_id(spotify_uri: str) -> str:
    """Stable id for an unmatched Spotify track (no local file yet).

    Deterministic so re-imports reuse the same row and membership instead of
    spawning duplicates. Prefix keeps these easy to grep in the DB.
    """
    digest = hashlib.sha1(spotify_uri.encode("utf-8")).hexdigest()
    return f"spotify-pending:{digest}"


def _spotify_vendor_track_id(src: SpotifyTrack) -> str | None:
    """Canonical vendor id for track_vendor_ids (bare Spotify track id)."""
    if src.spotify_id:
        return src.spotify_id
    uri = src.spotify_uri or ""
    if uri.startswith("spotify:track:"):
        return uri.split(":", 2)[2] or None
    return None


def _upsert_synthetic_track(
    conn: sqlite3.Connection,
    src: SpotifyTrack,
    *,
    machine_id: str,
    now: str,
) -> str | None:
    """Insert/update a placeholder track row for an unmatched Spotify track.

    Returns the row's ``stable_id``, or None when the user removed that
    placeholder: a re-import of the playlist is not a restore (LIBM-140), so
    the tombstone is left byte-identical and the caller keeps the track out of
    the playlist.

    ``file_path`` is the Spotify URI so ``is_streaming_path`` marks the row
    streaming (not broken-missing) in the browser.

    ``tracks`` and ``track_vendor_ids`` are synced digest tables (ADR 08
    point 2): this writer stamps ``updated_at`` / ``origin_device_id`` and
    appends a ``local_changelog`` entry through
    :func:`apps.shared.state.sync_stamp.stamp_and_log`, or the row syncs as
    epoch-old and the push fence never offers it.
    """
    sid = synthetic_stable_id(src.spotify_uri)
    artists_json = json.dumps(
        list(src.artists), sort_keys=False, separators=(",", ":"), ensure_ascii=False
    )
    tier = "isrc" if src.isrc else "inferred"
    file_path = src.spotify_uri or f"spotify:track:{src.spotify_id or sid}"
    existing = conn.execute(
        "SELECT deleted_at FROM tracks WHERE stable_id = ?", (sid,)
    ).fetchone()
    if existing is not None and existing[0] is not None:
        return None
    track_stamp = sync_stamp.stamp_and_log(conn, "tracks", (sid,), machine_id, now=now)
    if existing is None:
        conn.execute(
            """
            INSERT INTO tracks
              (stable_id, stable_id_tier, title, artists_json, album, isrc,
               duration_ms, file_path, content_hash, created_at, updated_at,
               origin_device_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                sid,
                tier,
                src.title,
                artists_json,
                src.album or None,
                src.isrc,
                src.duration_ms or None,
                file_path,
                now,
                track_stamp.updated_at,
                track_stamp.origin_device_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE tracks SET stable_id_tier=?, title=?, artists_json=?,
              album=?, isrc=?, duration_ms=?, file_path=?, updated_at=?,
              origin_device_id=?
            WHERE stable_id=? AND deleted_at IS NULL
            """,
            (
                tier,
                src.title,
                artists_json,
                src.album or None,
                src.isrc,
                src.duration_ms or None,
                file_path,
                track_stamp.updated_at,
                track_stamp.origin_device_id,
                sid,
            ),
        )
    vendor_id = _spotify_vendor_track_id(src)
    if vendor_id:
        _set_track_vendor_id(conn, sid, vendor_id, machine_id=machine_id, now=now)
    return sid


def _set_track_vendor_id(
    conn: sqlite3.Connection,
    stable_id: str,
    vendor_id: str,
    *,
    machine_id: str,
    now: str,
) -> None:
    """Set the Spotify vendor id for a track, stamped for sync.

    ``track_vendor_ids`` is a synced digest table (ADR 08 point 2); the raw
    ``INSERT OR REPLACE`` this replaced left ``updated_at`` /
    ``origin_device_id`` NULL and logged nothing, so a re-import never
    reached the hub.
    """
    stamp = sync_stamp.stamp_and_log(
        conn, "track_vendor_ids", (stable_id, VENDOR), machine_id, now=now
    )
    conn.execute(
        "INSERT INTO track_vendor_ids"
        "(stable_id, vendor, vendor_id, updated_at, origin_device_id) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(stable_id, vendor) DO UPDATE SET "
        "vendor_id=excluded.vendor_id, updated_at=excluded.updated_at, "
        "origin_device_id=excluded.origin_device_id, deleted_at=NULL",
        (stable_id, VENDOR, vendor_id, stamp.updated_at, stamp.origin_device_id),
    )


_SUGGESTED_SOURCES_TEMPLATE: tuple[tuple[str, str], ...] = (
    ("beatport", "https://www.beatport.com/search?q={q}"),
    ("bandcamp", "https://bandcamp.com/search?q={q}&item_type=t"),
    ("qobuz", "https://www.qobuz.com/us-en/search?q={q}"),
    ("apple_music", "https://music.apple.com/us/search?term={q}"),
    ("discogs", "https://www.discogs.com/search?q={q}&type=release"),
)

SUGGESTED_SOURCE_KEYS: tuple[str, ...] = tuple(
    source_name for source_name, _ in _SUGGESTED_SOURCES_TEMPLATE
)


def _suggested_sources(track: SpotifyTrack) -> dict[str, str]:
    q = quote_plus(f"{track.artists_joined} {track.title}".strip())
    return {name: tmpl.format(q=q) for name, tmpl in _SUGGESTED_SOURCES_TEMPLATE}
