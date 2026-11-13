"""Export the live library from the state DB as one rekordbox XML document (LIBORG-10051).

``plan_library_export`` opens one read snapshot, counts the collection and loads the
playlist folder tree, and refuses a tree rekordbox XML cannot express before any byte is
written. ``LibraryExport.iter_bytes`` then streams tracks and playlist entries from that
snapshot through ``writer.iter_dj_playlists_xml``, so 10k tracks never sit in memory.
``plan_playlist_export`` does the same for one playlist (LIBORG-9751): its live tracks once
each in COLLECTION, and ROOT holding only that playlist with every entry in order.

- TrackID is ``tracks.rowid``.
- A deleted track (``deleted_at`` set, LIBM-140) is in neither COLLECTION nor any playlist;
  a playlist holding only deleted tracks is written with ``Entries="0"``.
- A live track with no ``file_path`` cannot be imported by any DJ app: it is left out and
  counted in ``skipped_no_location``, never dropped silently.
- A streaming track (``spotify:``, ``tidal:``, ``http(s)://`` URI) cannot be a rekordbox file
  TRACK: it is left out of COLLECTION and every playlist, and counted in ``skipped_streaming``.
- BPM and key are the values the library shows: ``track_fields`` overridden by the injected
  ``lane_fields`` read model (selected analysis source), resolved per 500-row batch. A failed or
  missing own lane is omitted, never filled back with the rekordbox value.
- A stored BPM of 0 (rekordbox's "not analysed") exports as unknown tempo.
- ``set`` rows are not exported (rekordbox has no set); their tracks stay in COLLECTION.

Requirements:
- ✔︎ ✅ 🎯 Refuses before streaming.
    [if] two live siblings share a name [then ⛔️] ExportRefused
    [if] a live node's parent is not a live folder [then ⛔️] ExportRefused
- ✔︎ ✅ 🎯 Counts come from the same snapshot as the streamed rows.
- ✔︎ ✅ 🎯 Streaming URIs are skipped and counted.
    [if] a ``spotify:`` track reaches COLLECTION or a playlist's Entries [then ⛔️]
- ✔︎ ✅ 🎯 Effective analysis values are exported.
    [if] an ok own bpm 128 sits beside a track_fields bpm 120 and 120 is written [then ⛔️]
    [if] a failed own lane exports the rekordbox value [then ⛔️]
- ✔︎ ✅ 🎯 [if] a stored bpm 0.0 aborts the export [then ⛔️]
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.adapters.rekordbox_xml.writer import XmlNode, XmlTrack, iter_dj_playlists_xml, path_to_location
from apps.shared.state.ingest.path_collisions import STREAMING_URI_PREFIXES

if TYPE_CHECKING:
    from apps.analysis.selection import EffectiveField

LaneFieldsResolver = Callable[[sqlite3.Connection, list[str]], Mapping[str, Mapping[str, "EffectiveField"]]]

# R5 guard (test_soft_delete_read_guard): each ``conn.execute`` SELECT must be one
# AST-foldable ``+`` chain of string literals only (no Name refs, no f-string SELECTs).
# Globs must stay aligned with STREAMING_URI_PREFIXES.
assert STREAMING_URI_PREFIXES == ("spotify:", "tidal:", "http://", "https://")
_SQL_COUNT_EXPORTABLE_TRACKS = (
    "SELECT COUNT(*) FROM tracks t WHERE "
    + "t.deleted_at IS NULL AND COALESCE(t.file_path, '') <> '' AND NOT ("
    + "t.file_path GLOB 'spotify:*' OR t.file_path GLOB 'tidal:*' OR "
    + "t.file_path GLOB 'http://*' OR t.file_path GLOB 'https://*'"
    + ")"
)
_SQL_COUNT_SKIPPED_NO_LOCATION = (
    "SELECT COUNT(*) FROM tracks t WHERE t.deleted_at IS NULL AND COALESCE(t.file_path, '') = ''"
)
_SQL_COUNT_STREAMING = (
    "SELECT COUNT(*) FROM tracks t WHERE t.deleted_at IS NULL AND ("
    + "t.file_path GLOB 'spotify:*' OR t.file_path GLOB 'tidal:*' OR "
    + "t.file_path GLOB 'http://*' OR t.file_path GLOB 'https://*'"
    + ")"
)
_SQL_COUNT_PLAYLIST_ENTRIES = (
    "SELECT m.playlist_id, COUNT(*) FROM playlist_memberships m "
    + "JOIN tracks t ON t.stable_id = m.stable_id WHERE "
    + "m.deleted_at IS NULL AND t.deleted_at IS NULL AND "
    + "COALESCE(t.file_path, '') <> '' AND NOT ("
    + "t.file_path GLOB 'spotify:*' OR t.file_path GLOB 'tidal:*' OR "
    + "t.file_path GLOB 'http://*' OR t.file_path GLOB 'https://*'"
    + ") GROUP BY m.playlist_id"
)
_SQL_ITER_PLAYLIST_ENTRIES = (
    "SELECT t.rowid FROM playlist_memberships m JOIN tracks t ON t.stable_id = m.stable_id "
    + "WHERE m.playlist_id = ? AND "
    + "m.deleted_at IS NULL AND t.deleted_at IS NULL AND "
    + "COALESCE(t.file_path, '') <> '' AND NOT ("
    + "t.file_path GLOB 'spotify:*' OR t.file_path GLOB 'tidal:*' OR "
    + "t.file_path GLOB 'http://*' OR t.file_path GLOB 'https://*'"
    + ") ORDER BY COALESCE(m.order_key, printf('%08d', m.position)), m.position"
)
_SQL_ITER_EXPORTABLE_TRACKS = (
    "SELECT t.rowid, t.stable_id, t.file_path, t.title, t.artists_json, t.album, t.duration_ms, "
    + "bpm.value_json, k.value_json, r.value_json FROM tracks t "
    + "LEFT JOIN track_fields bpm ON bpm.stable_id = t.stable_id AND bpm.field_name = 'bpm' "
    + "LEFT JOIN track_fields k ON k.stable_id = t.stable_id AND k.field_name = 'key' "
    + "LEFT JOIN track_fields r ON r.stable_id = t.stable_id AND r.field_name = 'rating' "
    + "WHERE t.deleted_at IS NULL AND COALESCE(t.file_path, '') <> '' AND NOT ("
    + "t.file_path GLOB 'spotify:*' OR t.file_path GLOB 'tidal:*' OR "
    + "t.file_path GLOB 'http://*' OR t.file_path GLOB 'https://*'"
    + ") ORDER BY t.rowid"
)
# LIBORG-9751: the same reads narrowed to one playlist's live members.
_SQL_PLAYLIST_NODE = "SELECT name, kind FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL"
_SQL_COUNT_PLAYLIST_TRACKS = (
    "SELECT COUNT(*), COUNT(DISTINCT t.rowid) FROM playlist_memberships m "
    + "JOIN tracks t ON t.stable_id = m.stable_id WHERE m.playlist_id = ? AND "
    + "m.deleted_at IS NULL AND t.deleted_at IS NULL AND "
    + "COALESCE(t.file_path, '') <> '' AND NOT ("
    + "t.file_path GLOB 'spotify:*' OR t.file_path GLOB 'tidal:*' OR "
    + "t.file_path GLOB 'http://*' OR t.file_path GLOB 'https://*'"
    + ")"
)
_SQL_COUNT_PLAYLIST_SKIPPED = (
    "SELECT COALESCE(SUM(COALESCE(t.file_path, '') = ''), 0), COALESCE(SUM("
    + "t.file_path GLOB 'spotify:*' OR t.file_path GLOB 'tidal:*' OR "
    + "t.file_path GLOB 'http://*' OR t.file_path GLOB 'https://*'"
    + "), 0) FROM playlist_memberships m JOIN tracks t ON t.stable_id = m.stable_id "
    + "WHERE m.playlist_id = ? AND m.deleted_at IS NULL AND t.deleted_at IS NULL"
)
_SQL_ITER_PLAYLIST_TRACKS = (
    "SELECT t.rowid, t.stable_id, t.file_path, t.title, t.artists_json, t.album, t.duration_ms, "
    + "bpm.value_json, k.value_json, r.value_json FROM tracks t "
    + "LEFT JOIN track_fields bpm ON bpm.stable_id = t.stable_id AND bpm.field_name = 'bpm' "
    + "LEFT JOIN track_fields k ON k.stable_id = t.stable_id AND k.field_name = 'key' "
    + "LEFT JOIN track_fields r ON r.stable_id = t.stable_id AND r.field_name = 'rating' "
    + "WHERE t.deleted_at IS NULL AND COALESCE(t.file_path, '') <> '' AND NOT ("
    + "t.file_path GLOB 'spotify:*' OR t.file_path GLOB 'tidal:*' OR "
    + "t.file_path GLOB 'http://*' OR t.file_path GLOB 'https://*'"
    + ") AND t.stable_id IN (SELECT m.stable_id FROM playlist_memberships m "
    + "WHERE m.playlist_id = ? AND m.deleted_at IS NULL) ORDER BY t.rowid"
)
_FETCH_BATCH = 500


class ExportRefused(ValueError):
    """The library cannot be expressed as rekordbox XML."""


class PlaylistNotFound(LookupError):
    """No live playlist, folder or set has the requested id."""


@dataclass(frozen=True)
class LibraryExport:
    conn: sqlite3.Connection
    track_count: int
    tree: tuple[XmlNode, ...]
    skipped_no_location: int
    skipped_streaming: int
    playlist_count: int
    lane_fields: LaneFieldsResolver

    def iter_bytes(self) -> Iterator[bytes]:
        """Stream the document; closes the connection when done or abandoned."""
        try:
            yield from iter_dj_playlists_xml(
                track_count=self.track_count,
                tracks=_iter_tracks(self.conn, self.lane_fields, _SQL_ITER_EXPORTABLE_TRACKS, ()),
                tree=self.tree,
                entries_of=lambda key: _iter_entries(self.conn, key),
            )
        finally:
            self.close()

    def close(self) -> None:
        if self.conn.in_transaction:
            self.conn.rollback()
        self.conn.close()


def plan_library_export(conn: sqlite3.Connection, *, lane_fields: LaneFieldsResolver) -> LibraryExport:
    """Snapshot ``conn`` (BEGIN) and plan the export. Closes ``conn`` on refusal.

    ``lane_fields`` resolves the library's effective lane-owned fields (bpm, key) for a batch.
    """
    try:
        conn.execute("BEGIN")
        track_count = conn.execute(_SQL_COUNT_EXPORTABLE_TRACKS).fetchone()[0]
        skipped = conn.execute(_SQL_COUNT_SKIPPED_NO_LOCATION).fetchone()[0]
        streaming = conn.execute(_SQL_COUNT_STREAMING).fetchone()[0]
        tree, playlist_count = _load_tree(conn)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        raise
    return LibraryExport(conn, int(track_count), tree, int(skipped), int(streaming), playlist_count, lane_fields)


@dataclass(frozen=True)
class PlaylistExport:
    """One playlist as a rekordbox XML document: its tracks in COLLECTION, ROOT holding just it."""

    conn: sqlite3.Connection
    playlist_id: str
    track_count: int
    entry_count: int
    tree: tuple[XmlNode, ...]
    skipped_no_location: int
    skipped_streaming: int
    lane_fields: LaneFieldsResolver

    def iter_bytes(self) -> Iterator[bytes]:
        """Stream the document; closes the connection when done or abandoned."""
        try:
            yield from iter_dj_playlists_xml(
                track_count=self.track_count,
                tracks=_iter_tracks(self.conn, self.lane_fields, _SQL_ITER_PLAYLIST_TRACKS, (self.playlist_id,)),
                tree=self.tree,
                entries_of=lambda key: _iter_entries(self.conn, key),
            )
        finally:
            self.close()

    def close(self) -> None:
        if self.conn.in_transaction:
            self.conn.rollback()
        self.conn.close()


def plan_playlist_export(
    conn: sqlite3.Connection, playlist_id: str, *, lane_fields: LaneFieldsResolver
) -> PlaylistExport:
    """Snapshot ``conn`` and plan one playlist's export (LIBORG-9751). Closes ``conn`` on refusal.

    Each track is in COLLECTION once, however often the playlist holds it; the playlist's
    Entries keep every position in order. Tracks with no path or a streaming URI are counted.
    """
    try:
        conn.execute("BEGIN")
        name = _exportable_playlist_name(conn, playlist_id)
        entry_count, track_count = conn.execute(_SQL_COUNT_PLAYLIST_TRACKS, (playlist_id,)).fetchone()
        no_location, streaming = conn.execute(_SQL_COUNT_PLAYLIST_SKIPPED, (playlist_id,)).fetchone()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
        raise
    tree = (XmlNode(key=playlist_id, name=name, is_folder=False, entry_count=int(entry_count)),)
    return PlaylistExport(
        conn, playlist_id, int(track_count), int(entry_count), tree, int(no_location), int(streaming), lane_fields
    )


def _exportable_playlist_name(conn: sqlite3.Connection, playlist_id: str) -> str:
    node = conn.execute(_SQL_PLAYLIST_NODE, (playlist_id,)).fetchone()
    if node is None:
        raise PlaylistNotFound(f"no playlist {playlist_id!r}")
    name, kind = node
    if kind != "playlist":
        raise ExportRefused(f"{playlist_id!r} is a {kind}, not a playlist; only a playlist exports on its own")
    return str(name)


# -----------------------------------------------------------------------------
# tree


@dataclass(frozen=True)
class _Row:
    playlist_id: str
    name: str
    parent_id: str | None
    is_folder: bool


def _load_tree(conn: sqlite3.Connection) -> tuple[tuple[XmlNode, ...], int]:
    rows = [
        _Row(pid, name, parent, kind == "folder")
        for pid, name, parent, kind in conn.execute(
            "SELECT playlist_id, name, parent_id, kind FROM playlists "
            "WHERE deleted_at IS NULL AND kind IN ('folder', 'playlist') "
            "ORDER BY sort_index IS NULL, sort_index, name, playlist_id"
        )
    ]
    folders = {r.playlist_id for r in rows if r.is_folder}
    orphans = [r for r in rows if r.parent_id is not None and r.parent_id not in folders]
    if orphans:
        named = ", ".join(f"{r.name!r} (parent {r.parent_id!r})" for r in orphans)
        raise ExportRefused(f"playlists whose parent is not a live folder: {named}")
    by_parent: dict[str | None, list[_Row]] = {}
    for row in rows:
        by_parent.setdefault(row.parent_id, []).append(row)
    counts = _entry_counts(conn)

    def build(parent: str | None) -> tuple[XmlNode, ...]:
        siblings = by_parent.get(parent, [])
        names = [r.name for r in siblings]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ExportRefused(f"two sibling playlists or folders share a name: {', '.join(map(repr, dupes))}")
        return tuple(
            XmlNode(key=r.playlist_id, name=r.name, is_folder=True, children=build(r.playlist_id))
            if r.is_folder
            else XmlNode(key=r.playlist_id, name=r.name, is_folder=False, entry_count=counts.get(r.playlist_id, 0))
            for r in siblings
        )

    return build(None), sum(1 for r in rows if not r.is_folder)


def _entry_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return dict(conn.execute(_SQL_COUNT_PLAYLIST_ENTRIES))


# -----------------------------------------------------------------------------
# streamed rows


def _iter_entries(conn: sqlite3.Connection, playlist_id: str) -> Iterator[int]:
    cursor = conn.execute(_SQL_ITER_PLAYLIST_ENTRIES, (playlist_id,))
    while batch := cursor.fetchmany(_FETCH_BATCH):
        for (rowid,) in batch:
            yield int(rowid)


def _iter_tracks(
    conn: sqlite3.Connection, lane_fields: LaneFieldsResolver, sql: str, params: tuple[str, ...]
) -> Iterator[XmlTrack]:
    cursor = conn.execute(sql, params)
    while batch := cursor.fetchmany(_FETCH_BATCH):
        resolved = lane_fields(conn, [row[1] for row in batch])
        for row in batch:
            yield _xml_track(row, resolved.get(row[1], {}))


_TrackRow = tuple[int, str, str, str | None, str | None, str | None, int | None, str | None, str | None, str | None]


def _xml_track(row: _TrackRow, lane: Mapping[str, EffectiveField]) -> XmlTrack:
    rowid, stable_id, file_path, title, artists_json, album, duration_ms, bpm_json, key_json, rating_json = row
    try:
        bpm = _effective(lane, "bpm", _decode(bpm_json))
        key = _effective(lane, "key", _decode(key_json))
        return XmlTrack(
            track_id=int(rowid),
            location=path_to_location(file_path),
            name=title or "",
            artist=_artists(artists_json),
            album=album or "",
            duration_ms=duration_ms,
            bpm=_bpm(bpm),
            tonality=_key(key),
            rating_stars=_rating(_decode(rating_json)),
        )
    except ValueError as exc:
        raise ValueError(f"track {stable_id}: {exc}") from exc


def _artists(raw: str | None) -> str:
    if not raw:
        return ""
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise ValueError(f"artists_json {raw!r} is not a list of strings")
    return ", ".join(value)


def _decode(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


def _effective(lane: Mapping[str, EffectiveField], name: str, stored: Any) -> Any:
    """Mirror ``sqlite_backend._fetch_fields``: a resolved lane field replaces the stored one."""
    if name not in lane:
        return stored
    field = lane[name]
    return field.value if field.status == "ok" else None


def _bpm(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"bpm {value!r} is not a non-negative finite number")
    return float(value) or None  # 0 is rekordbox's "not analysed": unknown tempo


def _key(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"key {value!r} is not a string")  # noqa: TRY004  # bad stored data, not a caller bug
    return value or None


def _rating(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5:
        raise ValueError(f"rating {value!r} is not 0-5 stars")
    return value
