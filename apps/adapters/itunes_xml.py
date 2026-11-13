"""Apple Music or iTunes Library XML -> Open DJ playlists (issue #9752, rekordbox iTunes source parity).

rekordbox shows the playlists of an iTunes ``Library.xml`` as a browser source, tracks matched
by file location (mimicked, no review round). This module reads that plist with stdlib
``plistlib`` and writes one Open DJ playlist per iTunes playlist under a root ``iTunes``
folder, iTunes folders kept as folders.

The rules, each one tested:

* **Read.** The ``Tracks`` dict gives each Track ID a ``Location`` (a ``file://`` URI, decoded
  to a native path). ``Playlists`` keep their ``Playlist Items`` in order. The master library,
  the built-in views (``Distinguished Kind``) and smart playlists (``Smart Info`` /
  ``Smart Criteria``) are skipped and counted.
* **Match, never insert.** A location matches a live library track with the same path
  (Unicode NFC, as :func:`apps.adapters.library_import.nfc`). Unmatched locations are reported,
  not added to the library.
* **Re-run updates in place.** Each playlist id is ``compute_playlist_id("itunes", <Playlist
  Persistent ID>)``, so a second import renames and refills the same rows.
* **Dry-run by default**: the whole import runs in one writer transaction that is rolled back.

Reachable without the UI through ``POST /api/v1/import/itunes-xml`` (``opendj api POST ...``).
"""

from __future__ import annotations

import dataclasses
import plistlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from apps.adapters.library_import import live_tracks_by_path, nfc
from apps.adapters.m3u import file_uri_to_path
from apps.shared.state import library_org
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_common import compute_playlist_id

VENDOR = "itunes"
ROOT_FOLDER_NAME = "iTunes"
ROOT_FOLDER_KEY = "root-folder"
#: Undo-journal session the folder placements are recorded under.
UNDO_SESSION = "itunes-xml-import"


class ItunesXmlError(ValueError):
    """The file is not an iTunes or Apple Music Library XML plist."""


@dataclasses.dataclass(frozen=True)
class ItunesPlaylist:
    """One iTunes playlist or folder, items as Track IDs in iTunes order."""

    key: str
    name: str
    parent_key: str | None
    is_folder: bool
    track_ids: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ItunesLibrary:
    """What the reader kept and what it skipped."""

    locations: dict[str, str]
    playlists: tuple[ItunesPlaylist, ...]
    skipped: dict[str, int]


@dataclasses.dataclass
class ItunesImportReport:
    """What an import matched and wrote."""

    source: str
    dry_run: bool
    playlists_written: int = 0
    folders_written: int = 0
    entries_matched: int = 0
    entries_unmatched: int = 0
    unmatched_locations: list[str] = dataclasses.field(default_factory=list)
    skipped: dict[str, int] = dataclasses.field(default_factory=dict)
    playlist_ids: list[str] = dataclasses.field(default_factory=list)


# ---------- reader -----------------------------------------------------------


def _skip_reason(entry: Mapping[str, Any]) -> str | None:
    if entry.get("Master"):
        return "master"
    if "Distinguished Kind" in entry:
        return "built_in"
    if "Smart Info" in entry or "Smart Criteria" in entry:
        return "smart"
    return None


def parse_itunes_xml(data: bytes) -> ItunesLibrary:
    """Tracks with a location and playlists in order; master, built-in and smart lists skipped."""
    try:
        root = plistlib.loads(data, fmt=plistlib.FMT_XML)
    except (plistlib.InvalidFileException, ValueError) as error:
        raise ItunesXmlError(f"not a Library XML plist: {error}") from error
    if not isinstance(root, dict) or not isinstance(root.get("Tracks"), dict):
        raise ItunesXmlError("not a Library XML plist: no Tracks dict")
    locations = {
        str(track_id): file_uri_to_path(str(track["Location"]))
        for track_id, track in root["Tracks"].items()
        if str(track.get("Location", "")).lower().startswith("file://")
    }
    skipped: dict[str, int] = {}
    playlists: list[ItunesPlaylist] = []
    for entry in root.get("Playlists", []):
        reason = _skip_reason(entry)
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        playlists.append(
            ItunesPlaylist(
                key=str(entry.get("Playlist Persistent ID") or entry["Playlist ID"]),
                name=str(entry.get("Name") or "Untitled"),
                parent_key=entry.get("Parent Persistent ID"),
                is_folder=bool(entry.get("Folder")),
                track_ids=tuple(str(item["Track ID"]) for item in entry.get("Playlist Items", [])),
            )
        )
    return ItunesLibrary(locations=locations, playlists=tuple(playlists), skipped=skipped)


def read_itunes_xml(path: Path) -> ItunesLibrary:
    """Read a ``Library.xml`` file from disk."""
    return parse_itunes_xml(path.read_bytes())


# ---------- import -----------------------------------------------------------


def _depth(playlist: ItunesPlaylist, by_key: Mapping[str, ItunesPlaylist]) -> int:
    depth, cursor, seen = 0, playlist.parent_key, {playlist.key}
    while cursor in by_key and cursor not in seen:
        seen.add(cursor)
        depth += 1
        cursor = by_key[cursor].parent_key
    return depth


def _place(writer: StateWriter, playlist_id: str, parent_id: str, kind: str) -> None:
    library_org.organize_playlist(writer, UNDO_SESSION, playlist_id, {"parent_id": parent_id, "kind": kind})


def import_itunes_library(
    writer: StateWriter, library: ItunesLibrary, *, source: str, dry_run: bool = True
) -> ItunesImportReport:
    """Write each iTunes playlist under the ``iTunes`` folder, tracks matched by path; idempotent."""
    report = ItunesImportReport(source=source, dry_run=dry_run, skipped=dict(library.skipped))
    by_key = {playlist.key: playlist for playlist in library.playlists}
    folder_keys = {playlist.key for playlist in library.playlists if playlist.is_folder}
    with writer.transaction(commit=not dry_run):
        by_path = live_tracks_by_path(writer.raw_conn)
        root_id = compute_playlist_id(VENDOR, ROOT_FOLDER_KEY)
        writer.insert_playlist(playlist_id=root_id, name=ROOT_FOLDER_NAME, vendor=VENDOR, vendor_pl_id=ROOT_FOLDER_KEY)
        library_org.organize_playlist(writer, UNDO_SESSION, root_id, {"kind": "folder"})
        unmatched: dict[str, None] = {}
        # Folders first, parents before children; playlists keep XML order (sorted() is stable).
        ordered = sorted(library.playlists, key=lambda p: (not p.is_folder, _depth(p, by_key) if p.is_folder else 0))
        for playlist in ordered:
            playlist_id = compute_playlist_id(VENDOR, playlist.key)
            parent_key = playlist.parent_key if playlist.parent_key in folder_keys else None
            parent_id = root_id if parent_key is None else compute_playlist_id(VENDOR, parent_key)
            writer.insert_playlist(
                playlist_id=playlist_id, name=playlist.name, vendor=VENDOR, vendor_pl_id=playlist.key
            )
            _place(writer, playlist_id, parent_id, "folder" if playlist.is_folder else "playlist")
            report.playlist_ids.append(playlist_id)
            if playlist.is_folder:
                report.folders_written += 1
                continue
            members: list[str] = []
            for track_id in playlist.track_ids:
                location = library.locations.get(track_id)
                stable_id = None if location is None else by_path.get(nfc(location))
                if stable_id is None:
                    report.entries_unmatched += 1
                    unmatched[location or f"track {track_id} (no file location)"] = None
                    continue
                report.entries_matched += 1
                if stable_id not in members:
                    members.append(stable_id)
            writer.set_playlist_memberships(playlist_id, members)
            report.playlists_written += 1
        report.unmatched_locations = list(unmatched)
    return report


__all__ = [
    "ROOT_FOLDER_NAME",
    "VENDOR",
    "ItunesImportReport",
    "ItunesLibrary",
    "ItunesPlaylist",
    "ItunesXmlError",
    "import_itunes_library",
    "parse_itunes_xml",
    "read_itunes_xml",
]
