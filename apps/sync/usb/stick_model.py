"""Play from USB: stick ids, typed errors and the parsed library model.

Pure data, no I/O: :mod:`apps.sync.usb.stick_library` does the mounting,
caching and file resolution and re-exports everything here.

Ids (USBPLAY-04). A stick track is ``usb-<VolumeUUID>-<pdb_track_id>``, e.g.
``usb-C74521A7-BFC7-3382-B11A-FBE569767B61-36``: stable across unplug and
replug because both halves live on the stick, and safe as a URL and artifact
path segment (:func:`apps.shared.stable_id.is_safe_stable_id_segment`; a
colon would fail it). The UUID has hyphens of its own, so parsing splits on
the ``usb-`` prefix and the LAST hyphen. Playlists are ``pl-<pdb id>`` and
history playlists ``hist-<pdb id>``, unique within one stick's library.

Display strings from the pdb are trimmed (titles carry trailing spaces);
paths never are (the SSK stick's own mount path ends in a space).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from apps.shared.stable_id import is_safe_stable_id_segment

StickErrorCode = Literal[
    "USB_TRACK_ID_INVALID",
    "USB_VOLUME_ID_INVALID",
    "USB_VOLUME_HAS_NO_UUID",
    "USB_STICK_NOT_MOUNTED",
    "USB_STICK_ACCESS_BLOCKED",
    "USB_TRACK_NOT_FOUND",
    "USB_PATH_OUTSIDE_VOLUME",
    "USB_FILE_MISSING",
]

TRACK_ID_PREFIX = "usb-"
PLAYLIST_ID_PREFIX = "pl-"
HISTORY_ID_PREFIX = "hist-"
_PDB_ID_MAX = 2**32 - 1
_VOLUME_UUID_RE = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
_PDB_ID_RE = re.compile(r"[1-9][0-9]{0,9}")
_OPEN_KEY_RE = re.compile(r"(1[0-2]|[1-9])([md])")
# Camelot number n (1..12) -> rekordbox's musical spelling; A = minor, B = major.
_CAMELOT_MINOR = ("Abm", "Ebm", "Bbm", "Fm", "Cm", "Gm", "Dm", "Am", "Em", "Bm", "F#m", "Dbm")
_CAMELOT_MAJOR = ("B", "F#", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E")


class StickError(Exception):
    """A typed refusal; ``code`` plus ``extra`` become the HTTP ``detail``."""

    def __init__(self, code: StickErrorCode, message: str, **extra: str) -> None:
        self.code: StickErrorCode = code
        self.message = message
        self.extra = extra
        super().__init__(f"{code}: {message}")

    def to_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, **self.extra}


# ----- ids ------------------------------------------------------------------


@dataclass(frozen=True)
class StickTrackRef:
    volume_uuid: str
    pdb_id: int


def is_canonical_volume_uuid(volume_uuid: str) -> bool:
    """Uppercase-hex 8-4-4-4-12, the shape diskutil reports as VolumeUUID."""
    return _VOLUME_UUID_RE.fullmatch(volume_uuid) is not None


def mint_stick_track_id(volume_uuid: str, pdb_id: int) -> str:
    """``usb-<VolumeUUID>-<pdb_id>``; refuses anything parse would not round-trip."""
    if not is_canonical_volume_uuid(volume_uuid):
        raise ValueError(f"volume uuid {volume_uuid!r} is not uppercase-hex 8-4-4-4-12")
    if isinstance(pdb_id, bool) or not isinstance(pdb_id, int) or not 0 < pdb_id <= _PDB_ID_MAX:
        raise ValueError(f"pdb track id must be an int in 1..{_PDB_ID_MAX}, got {pdb_id!r}")
    track_id = f"{TRACK_ID_PREFIX}{volume_uuid}-{pdb_id}"
    if not is_safe_stable_id_segment(track_id):
        raise ValueError(f"minted stick id {track_id!r} fails is_safe_stable_id_segment")
    return track_id


def parse_stick_track_id(track_id: str) -> StickTrackRef:
    """Strict inverse of :func:`mint_stick_track_id`; anything else is invalid."""
    uuid_part, _, pdb_part = track_id.removeprefix(TRACK_ID_PREFIX).rpartition("-")
    if (
        not track_id.startswith(TRACK_ID_PREFIX)
        or not is_safe_stable_id_segment(track_id)
        or not is_canonical_volume_uuid(uuid_part)
        or _PDB_ID_RE.fullmatch(pdb_part) is None
        or int(pdb_part) > _PDB_ID_MAX
    ):
        raise StickError(
            "USB_TRACK_ID_INVALID",
            f"{track_id!r} is not a stick track id (usb-<VolumeUUID>-<pdb track id>)",
        )
    return StickTrackRef(volume_uuid=uuid_part, pdb_id=int(pdb_part))


# ----- display strings ------------------------------------------------------


def normalize_key(name: str | None) -> str | None:
    """Open Key (``6m``, ``9d``) -> rekordbox musical name; musical and
    Camelot names pass through unchanged (trimmed). The deck's key sync
    parses musical and Camelot only, so Open Key would lose sync."""
    text = _display(name)
    match = None if text is None else _OPEN_KEY_RE.fullmatch(text)
    if match is None:
        return text
    camelot_number = (int(match[1]) + 6) % 12 + 1
    names = _CAMELOT_MINOR if match[2] == "m" else _CAMELOT_MAJOR
    return names[camelot_number - 1]


def _display(text: str | None) -> str | None:
    """Trim a pdb display string; blank reads as absent. Never used on paths."""
    if text is None:
        return None
    return text.strip() or None


# ----- library model --------------------------------------------------------


@dataclass(frozen=True)
class StickTrack:
    id: str
    pdb_id: int
    title: str
    artist: str | None
    album: str | None
    genre: str | None
    key: str | None
    bpm: float | None
    duration_s: float | None
    rating: int
    play_count: int
    file_path: str
    analyze_path: str | None
    artwork_id: int
    artwork_path: str | None
    date_added: str | None

    @property
    def has_analysis(self) -> bool:
        return self.analyze_path is not None

    @property
    def has_artwork(self) -> bool:
        return self.artwork_id > 0


@dataclass(frozen=True)
class StickPlaylist:
    id: str
    pdb_id: int
    name: str
    parent_id: str | None
    is_folder: bool
    sort_order: int
    track_ids: tuple[str, ...]


@dataclass(frozen=True)
class StickHistory:
    id: str
    pdb_id: int
    name: str
    track_ids: tuple[str, ...]


@dataclass(frozen=True)
class StickLibrary:
    volume_uuid: str
    tracks: tuple[StickTrack, ...]
    tracks_by_pdb_id: Mapping[int, StickTrack]
    playlists: tuple[StickPlaylist, ...]
    history: tuple[StickHistory, ...]
    playlist_entry_count: int
    pdb_size: int
    pdb_mtime_ns: int
    parse_ms: float


def build_stick_library(
    volume_uuid: str, raw: dict[str, Any], fingerprint: tuple[int, int], parse_ms: float
) -> StickLibrary:
    artwork: Mapping[int, str] = raw["artwork"]
    tracks = tuple(_stick_track(volume_uuid, row, artwork) for row in raw["tracks"])
    by_pdb_id = {track.pdb_id: track for track in tracks}
    if len(by_pdb_id) != len(tracks):
        raise ValueError(f"export.pdb on {volume_uuid} repeats a track id")
    return StickLibrary(
        volume_uuid=volume_uuid,
        tracks=tracks,
        tracks_by_pdb_id=by_pdb_id,
        playlists=tuple(
            StickPlaylist(
                id=f"{PLAYLIST_ID_PREFIX}{row['id']}",
                pdb_id=row["id"],
                name=_display(row["name"]) or "",
                parent_id=f"{PLAYLIST_ID_PREFIX}{row['parent_id']}" if row["parent_id"] else None,
                is_folder=row["is_folder"],
                sort_order=row["sort_order"],
                track_ids=tuple(mint_stick_track_id(volume_uuid, t) for t in row["track_ids"]),
            )
            for row in _playlists_in_tree_order(raw["playlists"])
        ),
        history=tuple(
            StickHistory(
                id=f"{HISTORY_ID_PREFIX}{row['id']}",
                pdb_id=row["id"],
                name=_display(row["name"]) or "",
                track_ids=tuple(mint_stick_track_id(volume_uuid, t) for t in row["track_ids"]),
            )
            for row in raw["history"]
        ),
        playlist_entry_count=len(raw["playlist_entries"]),
        pdb_size=fingerprint[0],
        pdb_mtime_ns=fingerprint[1],
        parse_ms=parse_ms,
    )


def _stick_track(volume_uuid: str, row: dict[str, Any], artwork: Mapping[int, str]) -> StickTrack:
    return StickTrack(
        id=mint_stick_track_id(volume_uuid, row["id"]),
        pdb_id=row["id"],
        title=_display(row["title"]) or "",
        artist=_display(row["artist"]),
        album=_display(row["album"]),
        genre=_display(row["genre"]),
        key=normalize_key(row["key"]),
        bpm=row["bpm"],
        duration_s=float(row["duration_s"]) if row["duration_s"] else None,
        rating=row["rating"],
        play_count=row["play_count"],
        file_path=row["path"] or "",
        analyze_path=row["analyze_path"] or None,
        artwork_id=row["artwork_id"],
        artwork_path=artwork.get(row["artwork_id"]) if row["artwork_id"] > 0 else None,
        date_added=_display(row["date_added"]),
    )


def _playlists_in_tree_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Depth-first, each level by ``sort_order`` (rekordbox's own order), so
    a flat render is already correct. A row whose parent is missing (or a
    parent cycle) is appended at the end rather than dropped."""
    children: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        children.setdefault(row["parent_id"], []).append(row)
    for siblings in children.values():
        siblings.sort(key=lambda r: (r["sort_order"], r["id"]))
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()
    stack = list(reversed(children.get(0, [])))
    while stack:
        row = stack.pop()
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        ordered.append(row)
        stack.extend(reversed(children.get(row["id"], [])))
    stranded = [r for r in rows if r["id"] not in seen]
    stranded.sort(key=lambda r: (r["sort_order"], r["id"]))
    return ordered + stranded


__all__ = [
    "HISTORY_ID_PREFIX",
    "PLAYLIST_ID_PREFIX",
    "TRACK_ID_PREFIX",
    "StickError",
    "StickErrorCode",
    "StickHistory",
    "StickLibrary",
    "StickPlaylist",
    "StickTrack",
    "StickTrackRef",
    "build_stick_library",
    "is_canonical_volume_uuid",
    "mint_stick_track_id",
    "normalize_key",
    "parse_stick_track_id",
]
