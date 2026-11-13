"""Playlist, pairing, queue and stats reads mixed into :class:`.sqlite_backend.SqliteBackend`."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backend import (
    InMemoryBackend,
    Pairing,
    Playlist,
    PlaylistPage,
    QueueItem,
    QueueKind,
    Track,
    TrackPlaylistHit,
)
from .inbox_playlist import (
    INBOX_PLAYLIST_ID,
    inbox_updated_at_from_conn,
    make_inbox,
    unfiled_ids_from_conn,
    with_inbox,
)
from .pairings_sqlite import (
    count_http_pairings,
    list_http_pairings,
)
from .playlist_page import playlist_from_header, read_playlist_header, read_playlist_page
from .sqlite_backend_rows import (
    _graph_pairings,
    _pairing_matches,
    _warn_fallback_once,
)


class _CatalogReadsMixin:
    """Playlist, pairing, queue and stats reads of :class:`SqliteBackend`."""

    if TYPE_CHECKING:
        _path: Path
        _fallback: InMemoryBackend

        def _ro(self) -> AbstractContextManager[sqlite3.Connection]: ...

        def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool: ...

        def get_tracks_bulk(self, stable_ids: Sequence[str], *, live_only: bool = False) -> dict[str, Track]: ...

        def get_file_paths_bulk(self, stable_ids: Sequence[str]) -> dict[str, str | None]: ...

    def list_playlists(self) -> list[Playlist]:
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                _warn_fallback_once("list_playlists", "no playlists table")
                return self._fallback.list_playlists()
            rows = list(
                conn.execute(
                    "SELECT playlist_id, name, vendor, vendor_pl_id, "
                    "       created_at, updated_at, forbid_duplicates "
                    "FROM playlists WHERE deleted_at IS NULL ORDER BY playlist_id"
                )
            )
            if not self._table_exists(conn, "playlist_memberships"):
                members: dict[str, list[str]] = {}
            else:
                members = {}
                for pid, sid in conn.execute(
                    "SELECT playlist_id, stable_id FROM playlist_memberships "
                    "WHERE deleted_at IS NULL "
                    "ORDER BY playlist_id, "
                    "COALESCE(order_key, printf('%08d', position)), position"
                ):
                    members.setdefault(pid, []).append(sid)
            unfiled = unfiled_ids_from_conn(conn)
            updated = inbox_updated_at_from_conn(conn, unfiled)
        stored = [
            Playlist(
                playlist_id=r["playlist_id"],
                name=r["name"],
                vendor=r["vendor"],
                vendor_pl_id=r["vendor_pl_id"],
                items=members.get(r["playlist_id"], []),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                forbid_duplicates=bool(r["forbid_duplicates"]),
            )
            for r in rows
        ]
        return with_inbox(stored, unfiled, updated_at=updated)

    def get_playlist(self, playlist_id: str) -> Playlist:
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                _warn_fallback_once("get_playlist", "no playlists table")
                return self._fallback.get_playlist(playlist_id)
            if playlist_id == INBOX_PLAYLIST_ID:
                unfiled = unfiled_ids_from_conn(conn)
                updated = inbox_updated_at_from_conn(conn, unfiled)
                return make_inbox(items=unfiled, updated_at=updated)
            row = read_playlist_header(conn, playlist_id)
            member_rows = []
            if self._table_exists(conn, "playlist_memberships"):
                member_rows = conn.execute(
                    "SELECT stable_id, item_id, edit_id FROM playlist_memberships "
                    "WHERE playlist_id = ? AND deleted_at IS NULL "
                    "ORDER BY COALESCE(order_key, printf('%08d', position)), "
                    "position",
                    (playlist_id,),
                ).fetchall()
        return playlist_from_header(
            row,
            [r[0] for r in member_rows],
            [r[1] or "" for r in member_rows],
            [r[2] for r in member_rows],
        )

    def get_playlist_page(
        self,
        playlist_id: str,
        *,
        limit: int,
        offset: int,
    ) -> PlaylistPage:
        """One ordered window of the live membership (LIBM-133, playlist_page.py)."""
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                _warn_fallback_once("get_playlist_page", "no playlists table")
                return self._fallback.get_playlist_page(
                    playlist_id,
                    limit=limit,
                    offset=offset,
                )
            if playlist_id == INBOX_PLAYLIST_ID:
                unfiled = unfiled_ids_from_conn(conn)
                updated = inbox_updated_at_from_conn(conn, unfiled)
                pl = make_inbox(items=unfiled, updated_at=updated)
                window = slice(offset, offset + limit)
                return PlaylistPage(
                    playlist=replace(
                        pl,
                        items=pl.items[window],
                        item_ids=pl.item_ids[window],
                        edit_ids=pl.edit_ids[window],
                    ),
                    total=len(pl.items),
                )
            return read_playlist_page(conn, playlist_id, limit=limit, offset=offset)

    def get_playlist_summary_stats(self, playlist_id: str) -> Any:
        from .inbox_playlist import INBOX_PLAYLIST_ID
        from .playlist_summary_stats import stats_from_in_memory, stats_from_sqlite

        if playlist_id == INBOX_PLAYLIST_ID:
            pl = self.get_playlist(playlist_id)
            distinct = list(dict.fromkeys(pl.items))
            tracks = self.get_tracks_bulk(distinct)
            paths = self.get_file_paths_bulk(distinct)
            return stats_from_in_memory(
                distinct,
                tracks,
                paths,
                state_db_path=self._path,
            )
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                return self._fallback.get_playlist_summary_stats(playlist_id)
            return stats_from_sqlite(conn, playlist_id)

    def list_track_playlists(self, stable_id: str) -> list[TrackPlaylistHit]:
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists") or not self._table_exists(conn, "playlist_memberships"):
                return []
            rows = list(
                conn.execute(
                    "SELECT p.playlist_id, p.name, p.vendor, m.position "
                    "FROM playlist_memberships m "
                    "JOIN playlists p ON p.playlist_id = m.playlist_id "
                    "WHERE m.stable_id = ? "
                    "  AND m.deleted_at IS NULL "
                    "  AND p.deleted_at IS NULL "
                    "ORDER BY p.name COLLATE NOCASE, p.playlist_id, m.position",
                    (stable_id,),
                )
            )
        hits: list[TrackPlaylistHit] = []
        current: TrackPlaylistHit | None = None
        for row in rows:
            pid = row["playlist_id"]
            if current is not None and current.playlist_id == pid:
                current.positions.append(row["position"])
                continue
            current = TrackPlaylistHit(
                playlist_id=pid,
                name=row["name"],
                vendor=row["vendor"],
                positions=[row["position"]],
            )
            hits.append(current)
        return hits

    def list_pairings(
        self,
        *,
        from_stable_id: str | None = None,
        to_stable_id: str | None = None,
        source: str | None = None,
    ) -> list[Pairing]:
        with self._ro() as conn:
            http_rows = list_http_pairings(
                conn,
                from_stable_id=from_stable_id,
                to_stable_id=to_stable_id,
                source=source,
            )
            graph_rows = [
                pairing
                for pairing in _graph_pairings(conn)
                if _pairing_matches(
                    pairing,
                    from_stable_id=from_stable_id,
                    to_stable_id=to_stable_id,
                    source=source,
                )
            ]
        # HTTP rows keep the wire id and snapshot. A graph edge with the
        # same visible endpoints is that row's mirror (or a CLI edge the
        # HTTP table already captured) and is not listed twice. Graph-only
        # edges, including a reversed ``out_of`` read as ``->``, stay.
        covered = {(pairing.from_stable_id, pairing.to_stable_id, pairing.direction) for pairing in http_rows}
        out = list(http_rows)
        out.extend(
            pairing
            for pairing in graph_rows
            if (pairing.from_stable_id, pairing.to_stable_id, pairing.direction) not in covered
        )
        out.sort(key=lambda pairing: pairing.created_at)
        return out

    def get_queue(
        self,
        kind: QueueKind,
    ) -> tuple[list[QueueItem], str | None]:
        # Triage queues come from Phase 6/7/12; not in state.db yet.
        _warn_fallback_once(
            "get_queue",
            "triage queues not in Phase 5 state.db",
        )
        return self._fallback.get_queue(kind)

    def stats(self) -> dict[str, Any]:
        with self._ro() as conn:
            tracks = 0
            playlists = 0
            if self._table_exists(conn, "tracks"):
                tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            else:
                _warn_fallback_once("stats:tracks", "no tracks table")
            if self._table_exists(conn, "playlists"):
                playlists = conn.execute("SELECT COUNT(*) FROM playlists WHERE deleted_at IS NULL").fetchone()[0]
            else:
                _warn_fallback_once("stats:playlists", "no playlists table")
            http_rows = list_http_pairings(conn)
            pairings = count_http_pairings(conn)
            covered = {(pairing.from_stable_id, pairing.to_stable_id, pairing.direction) for pairing in http_rows}
            if self._table_exists(conn, "pairings"):
                pairings += sum(
                    1
                    for pairing in _graph_pairings(conn)
                    if (pairing.from_stable_id, pairing.to_stable_id, pairing.direction) not in covered
                )
            tracks_playable = 0
            if self._table_exists(conn, "tracks") and self._table_exists(conn, "track_availability"):
                tracks_playable = conn.execute(
                    "SELECT COUNT(*) FROM tracks AS t "
                    "JOIN track_availability AS a ON a.stable_id = t.stable_id "
                    "WHERE t.deleted_at IS NULL "
                    "AND a.state = 'present'"
                ).fetchone()[0]
        return {
            "tracks": tracks,
            "tracks_playable": tracks_playable,
            "playlists": playlists,
            "pairings": pairings,
        }
