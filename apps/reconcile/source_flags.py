"""Source flags: library tracks whose file may not be the recording it claims (SRCFLAG-01..03).

FLAG-ONLY (the maintainer, Thu 5 Nov 2026: "can always just flag em - not a large cost"):
nothing here refuses, moves or rewrites anything. A scan reads the library and
writes one verdict file, ``state/source-flags.json``; the listing, the HTTP
route, the CLI and the MCP tool all read that file.

Incident that motivated it: "Carmen - Opera Dance Music" was ingested on Mon 26
Oct as a different recording, 4.1 % shorter than the Spotify release, and
nothing compared the lengths.

Requirements:
* SRCFLAG-01 length vs release.
  [if] a track's length differs from its Spotify release length (same ISRC,
  else same title and artist) by more than 1 % [then] it carries a
  ``length_mismatch`` warning naming both lengths, [else stop].
* SRCFLAG-02 stem-set source identity, by audio length not file hash.
  [if] a track's stem-set manifest records a decoded source length more than
  1 % away from the track's current length [then] it carries a
  ``stems_other_recording`` warning; a changed file sha256 alone (a tag
  write) never flags, [else stop].
* SRCFLAG-03 hard-linked in-place write.
  [if] a track's file shares its inode with other names and was written after
  it was created [then] it carries a ``hardlinked_write`` info flag saying
  the write changed every linked name, [else stop].
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from apps.shared.same_recording import SameRecordingUnverifiable, check_same_recording

STORE_NAME = "source-flags.json"
STORE_SCHEMA = 1
#: A write this long after the file's birth counts as an in-place write.
IN_PLACE_WRITE_GRACE_S = 60.0

FlagCode = Literal["length_mismatch", "stems_other_recording", "hardlinked_write"]
Severity = Literal["warning", "info"]


@dataclass(frozen=True)
class SourceFlag:
    code: FlagCode
    severity: Severity
    message: str


@dataclass(frozen=True)
class LibraryTrack:
    stable_id: str
    title: str
    artist: str
    duration_s: float | None
    file_path: str | None
    isrc: str | None


@dataclass(frozen=True)
class ReleaseLength:
    duration_s: float
    source: str


# ----------------------------------------------------------------- matching


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "").casefold()).strip()


def load_spotify_release_lengths(spotify_dir: Path) -> tuple[dict[str, ReleaseLength], dict[tuple[str, str], ReleaseLength]]:
    """Release lengths from every Spotify import's ``matches.csv``, by ISRC and by (title, artist)."""
    by_isrc: dict[str, ReleaseLength] = {}
    by_title_artist: dict[tuple[str, str], ReleaseLength] = {}
    for matches in sorted(spotify_dir.glob("import-*/matches.csv")):
        with matches.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not row.get("duration_ms"):
                    continue
                release = ReleaseLength(int(row["duration_ms"]) / 1000, f"Spotify {row['spotify_uri']}")
                if row.get("isrc"):
                    by_isrc[row["isrc"].upper()] = release
                for artist in (row.get("artist") or "").split(","):
                    by_title_artist[(_norm(row.get("title")), _norm(artist))] = release
    return by_isrc, by_title_artist


def _release_for(track: LibraryTrack, by_isrc: dict[str, ReleaseLength], by_title_artist: dict[tuple[str, str], ReleaseLength]) -> ReleaseLength | None:
    if track.isrc and track.isrc.upper() in by_isrc:
        return by_isrc[track.isrc.upper()]
    release = by_title_artist.get((_norm(track.title), _norm(track.artist)))
    if release is None and track.file_path:
        stem = Path(track.file_path).stem
        if " - " in stem:
            artist, title = stem.split(" - ", 1)
            release = by_title_artist.get((_norm(title), _norm(artist.split(",")[0])))
    return release


# ----------------------------------------------------------------- the three checks


def length_flag(track: LibraryTrack, release: ReleaseLength | None) -> SourceFlag | None:
    """SRCFLAG-01. A release with no usable length cannot be compared, so it flags nothing."""
    if release is None or not track.duration_s or release.duration_s <= 0:
        return None
    verdict = check_same_recording(release.duration_s, track.duration_s)
    if verdict.same:
        return None
    return SourceFlag(
        "length_mismatch",
        "warning",
        f"Maybe a different recording: this file is {track.duration_s:.1f} s, the release is "
        f"{release.duration_s:.1f} s ({100 * (track.duration_s - release.duration_s) / release.duration_s:+.1f} %, {release.source})",
    )


def stems_flag(track: LibraryTrack, manifest: dict[str, object] | None) -> SourceFlag | None:
    """SRCFLAG-02: the manifest's decoded frame count is the source as the stems heard it."""
    if manifest is None or not track.duration_s:
        return None
    audio = manifest.get("audio")
    if not isinstance(audio, dict) or not audio.get("frame_count") or not audio.get("sample_rate"):
        return None
    stems_source_s = int(audio["frame_count"]) / int(audio["sample_rate"])
    try:
        verdict = check_same_recording(stems_source_s, track.duration_s)
    except SameRecordingUnverifiable:
        return None
    if verdict.same:
        return None
    return SourceFlag(
        "stems_other_recording",
        "warning",
        f"Stems were made from a different recording: their source was {stems_source_s:.1f} s, "
        f"this file is {track.duration_s:.1f} s",
    )


def hardlink_flag(*, nlink: int, mtime: float, birthtime: float | None) -> SourceFlag | None:
    """SRCFLAG-03. ``birthtime`` is None where the OS does not record it (Linux): no flag."""
    if nlink < 2 or birthtime is None or mtime - birthtime <= IN_PLACE_WRITE_GRACE_S:
        return None
    others = nlink - 1
    written = datetime.fromtimestamp(mtime, UTC).strftime("%a %d %b %H:%M UTC")
    return SourceFlag(
        "hardlinked_write",
        "info",
        f"Written in place on {written} through a hard link: the same write changed "
        f"{others} other file name{'s' if others != 1 else ''} (tags or audio)",
    )


def flags_for(track: LibraryTrack, *, release: ReleaseLength | None, manifest: dict[str, object] | None) -> list[SourceFlag]:
    flags = [length_flag(track, release), stems_flag(track, manifest)]
    if track.file_path:
        try:
            stat_result = os.stat(track.file_path)
        except FileNotFoundError:
            stat_result = None
        if stat_result is not None:
            flags.append(hardlink_flag(
                nlink=stat_result.st_nlink,
                mtime=stat_result.st_mtime,
                birthtime=getattr(stat_result, "st_birthtime", None),
            ))
    return [flag for flag in flags if flag is not None]


# ----------------------------------------------------------------- scan + store


def iter_library_tracks(state_db: Path) -> Iterator[LibraryTrack]:
    """Every live track with a local file path, read-only."""
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        isrc = {
            sid: json.loads(value)
            for sid, value in connection.execute("SELECT stable_id, value_json FROM track_fields WHERE field_name = 'isrc'")
        }
        for sid, title, artists_json, duration_ms, file_path in connection.execute(
            "SELECT stable_id, title, artists_json, duration_ms, file_path FROM tracks WHERE deleted_at IS NULL"
        ):
            artists = json.loads(artists_json or "[]")
            yield LibraryTrack(
                stable_id=sid,
                title=title or "",
                artist=artists[0] if artists else "",
                duration_s=duration_ms / 1000 if duration_ms else None,
                file_path=file_path,
                isrc=isrc.get(sid) if isinstance(isrc.get(sid), str) else None,
            )
    finally:
        connection.close()


def _manifest(stems_dir: Path, stable_id: str) -> dict[str, object] | None:
    path = stems_dir / stable_id / "manifest.json"
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def scan(data_dir: Path, tracks: Iterable[LibraryTrack] | None = None) -> dict[str, list[SourceFlag]]:
    """Flags for every library track under ``data_dir``. Reads only."""
    state_dir = data_dir / "state"
    by_isrc, by_title_artist = load_spotify_release_lengths(data_dir / "spotify")
    found: dict[str, list[SourceFlag]] = {}
    for track in tracks if tracks is not None else iter_library_tracks(state_dir / "state.db"):
        flags = flags_for(
            track,
            release=_release_for(track, by_isrc, by_title_artist),
            manifest=_manifest(state_dir / "stems", track.stable_id),
        )
        if flags:
            found[track.stable_id] = flags
    return found


def write_store(data_dir: Path, flags: dict[str, list[SourceFlag]]) -> Path:
    path = data_dir / "state" / STORE_NAME
    document = {
        "schema": STORE_SCHEMA,
        "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "flags": {sid: [asdict(flag) for flag in found] for sid, found in sorted(flags.items())},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_store(data_dir: Path) -> dict[str, object] | None:
    """The last scan, or None when no scan has run. A damaged file raises."""
    path = data_dir / "state" / STORE_NAME
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != STORE_SCHEMA or not isinstance(document.get("flags"), dict):
        raise ValueError(f"{path} is not a schema-{STORE_SCHEMA} source-flags store")
    return document


def flags_by_stable_id(data_dir: Path) -> dict[str, list[dict[str, str]]]:
    document = read_store(data_dir)
    if document is None:
        return {}
    flags = document["flags"]
    assert isinstance(flags, dict)
    return flags


__all__ = [
    "SourceFlag",
    "LibraryTrack",
    "ReleaseLength",
    "flags_by_stable_id",
    "flags_for",
    "hardlink_flag",
    "length_flag",
    "load_spotify_release_lengths",
    "read_store",
    "scan",
    "stems_flag",
    "write_store",
]
