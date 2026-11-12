"""Encode rekordbox classic ``export.pdb`` (DeviceSQL pages, issue #10031).

Layout matches crate-digger ``rekordbox_pdb.ksy`` (vendored under ``_vendor``):
page 0 is the header and table directory; each table is a chain of 4096-byte
pages with a 40-byte header, rows growing up, and 16-row group indexes down.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from apps.sync.usb.pioneer._vendor.rekordbox_pdb import RekordboxPdb  # type: ignore[attr-defined]

_PT = RekordboxPdb.PageType
PAGE_SIZE = 4096
_PAGE_HEADER_SIZE = 40
_ROW_GROUP_SIZE = 36
_ROWS_PER_GROUP = 16
_DATA_PAGE_FLAGS = 0x24
_TRACK_STRING_COUNT = 21
_TRACK_FIXED_SIZE = 94 + 2 * _TRACK_STRING_COUNT
_ANALYZE_PATH, _DATE_ADDED, _COMMENT, _TITLE, _FILENAME, _FILE_PATH = 14, 10, 16, 17, 19, 20


class PdbWriterError(ValueError):
    """Refused or invalid export.pdb payload."""


@dataclass(frozen=True)
class PdbTrack:
    id: int
    title: str
    file_path: str
    analyze_path: str = ""
    artist_id: int = 0
    album_id: int = 0
    genre_id: int = 0
    label_id: int = 0
    key_id: int = 0
    color_id: int = 0
    artwork_id: int = 0
    tempo_x100: int = 0
    duration_s: int = 0
    rating: int = 0
    play_count: int = 0
    sample_rate: int = 44100
    bitrate: int = 320
    file_size: int = 0
    date_added: str = ""
    comment: str = ""


@dataclass(frozen=True)
class PdbPlaylist:
    id: int
    name: str
    parent_id: int = 0
    sort_order: int = 0
    is_folder: bool = False


@dataclass(frozen=True)
class PdbExport:
    """Everything one ``export.pdb`` encodes.

    Entry tuples are ``(list_id, track_id, entry_index)`` for playlist and
    history membership rows.
    """

    tracks: Sequence[PdbTrack]
    artists: Mapping[int, str] = field(default_factory=dict)
    albums: Mapping[int, str] = field(default_factory=dict)
    genres: Mapping[int, str] = field(default_factory=dict)
    labels: Mapping[int, str] = field(default_factory=dict)
    keys: Mapping[int, str] = field(default_factory=dict)
    colors: Mapping[int, str] = field(default_factory=dict)
    artwork: Mapping[int, str] = field(default_factory=dict)
    playlists: Sequence[PdbPlaylist] = ()
    playlist_entries: Sequence[tuple[int, int, int]] = ()
    history: Mapping[int, str] = field(default_factory=dict)
    history_entries: Sequence[tuple[int, int, int]] = ()


def _device_sql_string(text: str) -> bytes:
    """Short ASCII when len<=126, else long UTF-16LE (DeviceSQL)."""
    if "\x00" in text:
        raise PdbWriterError("DeviceSQL string must not contain NUL")
    if text.isascii() and len(text) <= 126:
        return bytes([((len(text) + 1) << 1) | 1]) + text.encode("ascii")
    data = text.encode("utf-16-le")
    return b"\x90" + struct.pack("<HB", len(data) + 4, 0) + data


def _track_row(track: PdbTrack) -> bytes:
    strings = [""] * _TRACK_STRING_COUNT
    strings[_TITLE] = track.title
    strings[_FILE_PATH] = track.file_path
    strings[_FILENAME] = PurePosixPath(track.file_path).name
    strings[_ANALYZE_PATH] = track.analyze_path
    strings[_DATE_ADDED] = track.date_added
    strings[_COMMENT] = track.comment
    encoded = [_device_sql_string(text) for text in strings]
    offsets: list[int] = []
    cursor = _TRACK_FIXED_SIZE
    for blob in encoded:
        offsets.append(cursor)
        cursor += len(blob)
    head = struct.pack(
        "<HHIIIIIHHIIIIIIIIIIIIHHHHHHBBHH",
        0x24,
        0,
        0,
        track.sample_rate,
        0,
        track.file_size,
        0,
        0,
        0,
        track.artwork_id,
        track.key_id,
        0,
        track.label_id,
        0,
        track.bitrate,
        0,
        track.tempo_x100,
        track.genre_id,
        track.album_id,
        track.artist_id,
        track.id,
        0,
        track.play_count,
        0,
        16,
        track.duration_s,
        0,
        track.color_id,
        track.rating,
        0,
        0,
    )
    return head + struct.pack(f"<{_TRACK_STRING_COUNT}H", *offsets) + b"".join(encoded)


def _id_name_row(row_id: int, name: str) -> bytes:
    return struct.pack("<I", row_id) + _device_sql_string(name)


def _artist_row(row_id: int, name: str) -> bytes:
    return struct.pack("<HHIBB", 0x60, 0, row_id, 3, 10) + _device_sql_string(name)


def _album_row(row_id: int, name: str) -> bytes:
    return struct.pack("<HHIIIIBB", 0x80, 0, 0, 0, row_id, 0, 3, 22) + _device_sql_string(name)


def _key_row(row_id: int, name: str) -> bytes:
    return struct.pack("<II", row_id, row_id) + _device_sql_string(name)


def _color_row(row_id: int, name: str) -> bytes:
    return b"\0" * 5 + struct.pack("<HB", row_id, 0) + _device_sql_string(name)


def _playlist_row(playlist: PdbPlaylist) -> bytes:
    return struct.pack(
        "<I4sIII",
        playlist.parent_id,
        b"\0" * 4,
        playlist.sort_order,
        playlist.id,
        1 if playlist.is_folder else 0,
    ) + _device_sql_string(playlist.name)


def _sorted_rows(mapping: Mapping[int, str], factory: Any) -> list[bytes]:
    return [factory(i, n) for i, n in sorted(mapping.items())]


def _table_rows(export: PdbExport) -> list[tuple[int, list[bytes]]]:
    playlist_entry = [
        struct.pack("<III", entry_index, track_id, list_id)
        for list_id, track_id, entry_index in export.playlist_entries
    ]
    history_entry = [
        struct.pack("<III", track_id, list_id, entry_index) for list_id, track_id, entry_index in export.history_entries
    ]
    return [
        (_PT.tracks, [_track_row(track) for track in export.tracks]),
        (_PT.genres, _sorted_rows(export.genres, _id_name_row)),
        (_PT.artists, _sorted_rows(export.artists, _artist_row)),
        (_PT.albums, _sorted_rows(export.albums, _album_row)),
        (_PT.labels, _sorted_rows(export.labels, _id_name_row)),
        (_PT.keys, _sorted_rows(export.keys, _key_row)),
        (_PT.colors, _sorted_rows(export.colors, _color_row)),
        (_PT.playlist_tree, [_playlist_row(p) for p in export.playlists]),
        (_PT.playlist_entries, playlist_entry),
        (_PT.history_playlists, _sorted_rows(export.history, _id_name_row)),
        (_PT.history_entries, history_entry),
        (_PT.artwork, _sorted_rows(export.artwork, _id_name_row)),
    ]


def _padded(row: bytes) -> bytes:
    return row + b"\0" * (-len(row) % 4)


def _index_size(row_count: int) -> int:
    return _ROW_GROUP_SIZE * ((row_count + _ROWS_PER_GROUP - 1) // _ROWS_PER_GROUP)


def _split_into_pages(rows: list[bytes]) -> list[list[bytes]]:
    if not rows:
        return [[]]
    pages: list[list[bytes]] = [[]]
    heap_used = 0
    capacity = PAGE_SIZE - _PAGE_HEADER_SIZE
    for row in rows:
        size = len(_padded(row))
        if size + _index_size(1) > capacity:
            raise PdbWriterError(f"a {size}-byte row cannot fit on a {PAGE_SIZE}-byte page")
        if heap_used + size + _index_size(len(pages[-1]) + 1) > capacity:
            pages.append([])
            heap_used = 0
        pages[-1].append(row)
        heap_used += size
    return pages


def _data_page(page_index: int, page_type: int, next_page: int, rows: list[bytes]) -> bytes:
    page = bytearray(PAGE_SIZE)
    heap = bytearray()
    offsets: list[int] = []
    for row in rows:
        offsets.append(len(heap))
        heap += _padded(row)
    count = len(rows)
    struct.pack_into("<4sIIII4s", page, 0, b"\0" * 4, page_index, page_type, next_page, 1, b"\0" * 4)
    page[24:27] = (count | (count << 13)).to_bytes(3, "little")
    page[27] = _DATA_PAGE_FLAGS
    free = PAGE_SIZE - _PAGE_HEADER_SIZE - len(heap) - _index_size(count)
    struct.pack_into("<HHHHHH", page, 28, free, len(heap), 0, 0, 0, 0)
    page[_PAGE_HEADER_SIZE : _PAGE_HEADER_SIZE + len(heap)] = heap
    for group in range((count + _ROWS_PER_GROUP - 1) // _ROWS_PER_GROUP):
        base = PAGE_SIZE - group * _ROW_GROUP_SIZE
        group_offsets = offsets[group * _ROWS_PER_GROUP : (group + 1) * _ROWS_PER_GROUP]
        struct.pack_into("<HH", page, base - 4, (1 << len(group_offsets)) - 1, 0)
        for row_index, offset in enumerate(group_offsets):
            struct.pack_into("<H", page, base - 6 - 2 * row_index, offset)
    return bytes(page)


def build_export_pdb(export: PdbExport) -> bytes:
    """Encode ``export`` as ``export.pdb`` bytes the vendored reader parses."""
    tables = [(int(t), _split_into_pages(rows)) for t, rows in _table_rows(export)]
    next_unused = 1 + sum(len(pages) for _, pages in tables)
    directory: list[tuple[int, int, int]] = []
    body: list[bytes] = []
    page_index = 1
    for page_type, pages in tables:
        first = page_index
        for position, rows in enumerate(pages):
            is_last = position == len(pages) - 1
            next_page = next_unused if is_last else page_index + 1
            body.append(_data_page(page_index, page_type, next_page, rows))
            page_index += 1
        directory.append((page_type, first, page_index - 1))
    header = bytearray(PAGE_SIZE)
    struct.pack_into("<IIIIII4s", header, 0, 0, PAGE_SIZE, len(directory), next_unused, 5, 1, b"\0" * 4)
    for position, (page_type, first, last) in enumerate(directory):
        struct.pack_into("<IIII", header, 28 + 16 * position, page_type, next_unused, first, last)
    return bytes(header) + b"".join(body)


def write_export_pdb(volume_root: Path, export: PdbExport) -> Path:
    """Write ``PIONEER/rekordbox/export.pdb`` under ``volume_root``; return its path."""
    pdb_path = volume_root / "PIONEER" / "rekordbox" / "export.pdb"
    pdb_path.parent.mkdir(parents=True, exist_ok=True)
    pdb_path.write_bytes(build_export_pdb(export))
    return pdb_path


def pdb_export_from_dict(data: Mapping[str, Any]) -> PdbExport:
    """Build :class:`PdbExport` from a JSON-friendly dict (CLI helper)."""
    tracks = [
        PdbTrack(
            id=int(t["id"]),
            title=str(t["title"]),
            file_path=str(t["file_path"]),
            analyze_path=str(t.get("analyze_path", "")),
            artist_id=int(t.get("artist_id", 0)),
            album_id=int(t.get("album_id", 0)),
            genre_id=int(t.get("genre_id", 0)),
            label_id=int(t.get("label_id", 0)),
            key_id=int(t.get("key_id", 0)),
            color_id=int(t.get("color_id", 0)),
            artwork_id=int(t.get("artwork_id", 0)),
            tempo_x100=int(t.get("tempo_x100", 0)),
            duration_s=int(t.get("duration_s", 0)),
            rating=int(t.get("rating", 0)),
            play_count=int(t.get("play_count", 0)),
            sample_rate=int(t.get("sample_rate", 44100)),
            bitrate=int(t.get("bitrate", 320)),
            file_size=int(t.get("file_size", 0)),
            date_added=str(t.get("date_added", "")),
            comment=str(t.get("comment", "")),
        )
        for t in data.get("tracks", ())
    ]
    playlists = [
        PdbPlaylist(
            id=int(p["id"]),
            name=str(p["name"]),
            parent_id=int(p.get("parent_id", 0)),
            sort_order=int(p.get("sort_order", 0)),
            is_folder=bool(p.get("is_folder", False)),
        )
        for p in data.get("playlists", ())
    ]

    def _int_map(section: str) -> dict[int, str]:
        raw = data.get(section, {})
        return {int(k): str(v) for k, v in raw.items()}

    return PdbExport(
        tracks=tracks,
        artists=_int_map("artists"),
        albums=_int_map("albums"),
        genres=_int_map("genres"),
        labels=_int_map("labels"),
        keys=_int_map("keys"),
        colors=_int_map("colors"),
        artwork=_int_map("artwork"),
        playlists=playlists,
        playlist_entries=[tuple(int(x) for x in row) for row in data.get("playlist_entries", ())],
        history=_int_map("history"),
        history_entries=[tuple(int(x) for x in row) for row in data.get("history_entries", ())],
    )


__all__ = [
    "PAGE_SIZE",
    "PdbExport",
    "PdbPlaylist",
    "PdbTrack",
    "PdbWriterError",
    "build_export_pdb",
    "pdb_export_from_dict",
    "write_export_pdb",
]
