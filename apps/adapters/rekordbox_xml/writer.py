"""Write a whole collection and its playlist folder tree as one rekordbox XML document.

Mimics rekordbox File > Export Collection in xml format (LIBORG-10051): one
``DJ_PLAYLISTS``, every track once in ``COLLECTION``, and ``PLAYLISTS`` holding one
``ROOT`` folder with nested folders (``Type="0"``, ``Count``) and playlists
(``Type="1"``, ``KeyType="0"``, ``Entries``, ``<TRACK Key="<TrackID>"/>``). The output is
what ``reader.read_rekordbox_xml`` reads back with the same tracks and tree.

Pure: no sqlite. The caller feeds tracks and playlist entries as iterators, and the
document comes out as byte chunks, so memory stays flat however large the library is.

Requirements:
- ✔︎ ✅ 🎯 Streams: chunks are yielded while tracks are still being consumed.
    [if] a 10k-track iterator is fed [then ⛔️] the first chunk arrives before it is exhausted
- ✔︎ ✅ 🎯 Never writes a lying count (the reader refuses one).
    [if] ``entries_of`` yields fewer ids than ``entry_count`` [then ⛔️] ValueError
    [if] ``tracks`` yields more than ``track_count`` [then ⛔️] ValueError
- ✔︎ ✅ 🎯 Locations are the exact inverse of ``reader.location_to_path``.
    [if] a POSIX, ``C:/`` or ``//host/share`` path round-trips [then ⛔️] it comes back unchanged
- ✔︎ ✅ 🎯 Deterministic: the same input gives the same bytes (no timestamps).

Several artists are joined with ``", "``; the reader reads them back as one artist string.
Cue points and beatgrids are not written yet.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote
from xml.sax.saxutils import quoteattr

PRODUCT_NAME = "Open DJ"
PRODUCT_VERSION = "1"
DEFAULT_CHUNK_BYTES = 64 * 1024

# XML 1.0 cannot carry these at all, not even escaped.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_PATH_SAFE = "/:!$&'()*+,;=@~-._"


@dataclass(frozen=True)
class XmlTrack:
    track_id: int
    location: str
    name: str
    artist: str
    album: str
    duration_ms: int | None
    bpm: float | None
    tonality: str | None
    rating_stars: int | None


@dataclass(frozen=True)
class XmlNode:
    #: Opaque id the caller's ``entries_of`` resolves; unused for folders.
    key: str
    name: str
    is_folder: bool
    children: tuple[XmlNode, ...] = ()
    #: Playlists only; 0 for folders.
    entry_count: int = 0


def path_to_location(path: str) -> str:
    """``/Volumes/Invented DJ/a.mp3`` -> ``file://localhost/Volumes/Invented%20DJ/a.mp3``."""
    if path.startswith("//"):
        host, _, rest = path[2:].partition("/")
        return f"file://{quote(host, safe='')}/{quote(rest, safe=_PATH_SAFE)}"
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return "file://localhost/" + quote(path, safe=_PATH_SAFE)
    if not path.startswith("/"):
        raise ValueError(f"path {path!r} is not absolute")
    return "file://localhost" + quote(path, safe=_PATH_SAFE)


def iter_dj_playlists_xml(
    *,
    track_count: int,
    tracks: Iterable[XmlTrack],
    tree: Sequence[XmlNode],
    entries_of: Callable[[str], Iterable[int]],
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> Iterator[bytes]:
    """The document as UTF-8 chunks of about ``chunk_bytes`` each."""
    buf: list[str] = []
    size = 0
    for part in _iter_parts(track_count, tracks, tree, entries_of):
        buf.append(part)
        size += len(part)
        if size >= chunk_bytes:
            yield "".join(buf).encode()
            buf, size = [], 0
    if buf:
        yield "".join(buf).encode()


# -----------------------------------------------------------------------------
# helpers


def _iter_parts(
    track_count: int,
    tracks: Iterable[XmlTrack],
    tree: Sequence[XmlNode],
    entries_of: Callable[[str], Iterable[int]],
) -> Iterator[str]:
    yield '<?xml version="1.0" encoding="UTF-8"?>\n<DJ_PLAYLISTS Version="1.0.0">\n'
    yield f"  <PRODUCT Name={_attr(PRODUCT_NAME)} Version={_attr(PRODUCT_VERSION)} Company={_attr(PRODUCT_NAME)}/>\n"
    yield f'  <COLLECTION Entries="{track_count}">\n'
    written = 0
    for track in tracks:
        written += 1
        if written > track_count:
            raise ValueError(f"COLLECTION declared {track_count} tracks but more were given")
        yield _track_element(track)
    if written != track_count:
        raise ValueError(f"COLLECTION declared {track_count} tracks but {written} were given")
    yield "  </COLLECTION>\n  <PLAYLISTS>\n"
    yield f'    <NODE Type="0" Name="ROOT" Count="{len(tree)}">\n'
    for node in tree:
        yield from _node_parts(node, entries_of, depth=3)
    yield "    </NODE>\n  </PLAYLISTS>\n</DJ_PLAYLISTS>\n"


def _node_parts(node: XmlNode, entries_of: Callable[[str], Iterable[int]], depth: int) -> Iterator[str]:
    pad = "  " * depth
    name = _attr(node.name)
    if node.is_folder:
        yield f'{pad}<NODE Type="0" Name={name} Count="{len(node.children)}">\n'
        for child in node.children:
            yield from _node_parts(child, entries_of, depth + 1)
        yield f"{pad}</NODE>\n"
        return
    yield f'{pad}<NODE Name={name} Type="1" KeyType="0" Entries="{node.entry_count}">\n'
    written = 0
    for track_id in entries_of(node.key):
        written += 1
        if written > node.entry_count:
            raise ValueError(f"playlist {node.name!r} declared {node.entry_count} entries but more were given")
        yield f'{pad}  <TRACK Key="{int(track_id)}"/>\n'
    if written != node.entry_count:
        raise ValueError(f"playlist {node.name!r} declared {node.entry_count} entries but {written} were given")
    yield f"{pad}</NODE>\n"


def _track_element(track: XmlTrack) -> str:
    attrs: list[tuple[str, str | None]] = [
        ("TrackID", str(int(track.track_id))),
        ("Name", track.name),
        ("Artist", track.artist),
        ("Album", track.album),
        ("TotalTime", _seconds(track.duration_ms)),
        ("AverageBpm", f"{track.bpm:.2f}" if track.bpm is not None else None),
        ("Tonality", track.tonality),
        ("Rating", str(track.rating_stars * 51) if track.rating_stars is not None else None),
        ("Location", track.location),
    ]
    try:
        body = " ".join(f"{key}={_attr(value)}" for key, value in attrs if value)
    except ValueError as exc:
        raise ValueError(f"TrackID={track.track_id}: {exc}") from exc
    return f"    <TRACK {body}/>\n"


def _seconds(ms: int | None) -> str | None:
    if ms is None:
        return None
    return format(Decimal(int(ms)).scaleb(-3), "f")


def _attr(value: str) -> str:
    if _XML_ILLEGAL.search(value):
        raise ValueError(f"{value!r} holds a character XML cannot carry")
    return quoteattr(value, {"\n": "&#10;", "\r": "&#13;", "\t": "&#9;"})
