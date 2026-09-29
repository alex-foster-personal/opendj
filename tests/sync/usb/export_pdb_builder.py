"""Write a synthetic rekordbox ``export.pdb`` for tests (DeviceSQL pages).

The repo has no ``export.pdb`` writer (``writer_rbox`` writes only the
OneLibrary ``exportLibrary.db``) and the real stick fixtures are private, so
Play from USB tests build their own export: synthetic titles and paths,
encoded in the same page/row layout rekordbox writes, and parsed back by the
production Kaitai reader. Nothing here is copied from a real stick.

Layout (crate-digger ``rekordbox_pdb.ksy``, the parser vendored in
``apps/sync/usb/pioneer/_vendor``): page 0 holds the file header and the
table directory; every table is a chain of fixed-size pages, each a 40-byte
header, a row heap growing up, and row-group indexes (16 row offsets plus a
presence bitmask, 36 bytes) growing down from the end of the page.
"""
from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# The vendored parser is a whole-file ``# type: ignore``, so mypy sees no names in it.
from apps.sync.usb.pioneer._vendor.rekordbox_pdb import RekordboxPdb  # type: ignore[attr-defined]

_PT = RekordboxPdb.PageType
PAGE_SIZE = 4096
_PAGE_HEADER_SIZE = 40
_ROW_GROUP_SIZE = 36
_ROWS_PER_GROUP = 16
_DATA_PAGE_FLAGS = 0x24
_TRACK_STRING_COUNT = 21
_TRACK_FIXED_SIZE = 94 + 2 * _TRACK_STRING_COUNT
# Indexes into a track row's 21 string offsets (crate-digger TrackRow).
_ANALYZE_PATH, _DATE_ADDED, _COMMENT, _TITLE, _FILENAME, _FILE_PATH = 14, 10, 16, 17, 19, 20


@dataclass(frozen=True)
class PdbTrack:
    id: int
    title: str
    file_path: str
    analyze_path: str = ""
    artist_id: int = 0
    album_id: int = 0
    genre_id: int = 0
    key_id: int = 0
    artwork_id: int = 0
    tempo_x100: int = 0
    duration_s: int = 0
    rating: int = 0
    play_count: int = 0
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
    """Everything one synthetic ``export.pdb`` holds.

    Entry tuples are ``(list_id, track_id, entry_index)`` for both playlist
    and history entries.
    """

    tracks: Sequence[PdbTrack]
    artists: Mapping[int, str] = field(default_factory=dict)
    albums: Mapping[int, str] = field(default_factory=dict)
    genres: Mapping[int, str] = field(default_factory=dict)
    keys: Mapping[int, str] = field(default_factory=dict)
    artwork: Mapping[int, str] = field(default_factory=dict)
    playlists: Sequence[PdbPlaylist] = ()
    playlist_entries: Sequence[tuple[int, int, int]] = ()
    history: Mapping[int, str] = field(default_factory=dict)
    history_entries: Sequence[tuple[int, int, int]] = ()


# ----- strings and rows -----------------------------------------------------


def _device_sql_string(text: str) -> bytes:
    """Short ASCII when it fits (odd length-and-kind byte), else long UTF-16LE."""
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
        "<HHIIIIIHH" "IIIIIIIIIIII" "HHHHHH" "BB" "HH",
        0x24, 0, 0, 44100, 0, 0, 0, 0, 0,
        track.artwork_id, track.key_id, 0, 0, 0, 320, 0, track.tempo_x100,
        track.genre_id, track.album_id, track.artist_id, track.id,
        0, track.play_count, 0, 16, track.duration_s, 0x29,
        0, track.rating,
        1, 0,
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


def _playlist_row(playlist: PdbPlaylist) -> bytes:
    return struct.pack(
        "<I4sIII",
        playlist.parent_id,
        b"\0" * 4,
        playlist.sort_order,
        playlist.id,
        1 if playlist.is_folder else 0,
    ) + _device_sql_string(playlist.name)


def _table_rows(export: PdbExport) -> list[tuple[int, list[bytes]]]:
    playlist_entry = [
        struct.pack("<III", entry_index, track_id, list_id)
        for list_id, track_id, entry_index in export.playlist_entries
    ]
    history_entry = [
        struct.pack("<III", track_id, list_id, entry_index)
        for list_id, track_id, entry_index in export.history_entries
    ]
    return [
        (_PT.tracks, [_track_row(track) for track in export.tracks]),
        (_PT.genres, [_id_name_row(i, n) for i, n in export.genres.items()]),
        (_PT.artists, [_artist_row(i, n) for i, n in export.artists.items()]),
        (_PT.albums, [_album_row(i, n) for i, n in export.albums.items()]),
        (_PT.keys, [_key_row(i, n) for i, n in export.keys.items()]),
        (_PT.playlist_tree, [_playlist_row(p) for p in export.playlists]),
        (_PT.playlist_entries, playlist_entry),
        (_PT.history_playlists, [_id_name_row(i, n) for i, n in export.history.items()]),
        (_PT.history_entries, history_entry),
        (_PT.artwork, [_id_name_row(i, p) for i, p in export.artwork.items()]),
    ]


# ----- pages ---------------------------------------------------------------


def _padded(row: bytes) -> bytes:
    return row + b"\0" * (-len(row) % 4)


def _index_size(row_count: int) -> int:
    return _ROW_GROUP_SIZE * ((row_count + _ROWS_PER_GROUP - 1) // _ROWS_PER_GROUP)


def _split_into_pages(rows: list[bytes]) -> list[list[bytes]]:
    pages: list[list[bytes]] = [[]]
    heap_used = 0
    capacity = PAGE_SIZE - _PAGE_HEADER_SIZE
    for row in rows:
        size = len(_padded(row))
        if size + _index_size(1) > capacity:
            raise ValueError(f"a {size}-byte row cannot fit on a {PAGE_SIZE}-byte page")
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
    struct.pack_into(
        "<4sIIII4s", page, 0, b"\0" * 4, page_index, page_type, next_page, 1, b"\0" * 4
    )
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
    struct.pack_into(
        "<IIIIII4s", header, 0, 0, PAGE_SIZE, len(directory), next_unused, 5, 1, b"\0" * 4
    )
    for position, (page_type, first, last) in enumerate(directory):
        struct.pack_into("<IIII", header, 28 + 16 * position, page_type, next_unused, first, last)
    return bytes(header) + b"".join(body)


def write_export_pdb(volume_root: Path, export: PdbExport) -> Path:
    """Write ``PIONEER/rekordbox/export.pdb`` under ``volume_root``; return its path."""
    pdb_path = volume_root / "PIONEER" / "rekordbox" / "export.pdb"
    pdb_path.parent.mkdir(parents=True, exist_ok=True)
    pdb_path.write_bytes(build_export_pdb(export))
    return pdb_path


__all__ = [
    "PAGE_SIZE",
    "PdbExport",
    "PdbPlaylist",
    "PdbTrack",
    "build_export_pdb",
    "write_export_pdb",
]
