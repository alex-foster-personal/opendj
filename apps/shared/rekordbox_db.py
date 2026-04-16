"""Thin typed wrapper around :mod:`pyrekordbox` for Rekordbox 6/7 master.db.

We do not re-implement any ORM — we just expose a small set of dataclasses
(:class:`RBTrack`, :class:`RBPlaylist`) and iterator helpers that hide the
rough edges (nullable relationships, BPM×100 storage, streaming FolderPaths).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from pyrekordbox import Rekordbox6Database

from . import paths

_STREAMING_PREFIXES = ("spotify:", "tidal:", "http://", "https://")


def is_streaming_path(p: str | None) -> bool:
    """True if ``p`` is empty/None or looks like a streaming service URI."""
    if not p:
        return True
    return p.startswith(_STREAMING_PREFIXES)


@dataclass(slots=True)
class RBTrack:
    id: str
    title: str
    artist: str
    album: str
    genre: str
    folder_path: str  # raw FolderPath — may be absolute path, URI, or empty
    file_path: Path | None  # None for streaming / empty
    is_streaming: bool
    bpm: float | None
    rating: int | None
    file_size: int | None
    date_added: str | None


@dataclass(slots=True)
class RBPlaylist:
    id: str
    name: str
    parent_id: str | None
    track_ids: list[str]


def open_db(path: Path | None = None) -> Rekordbox6Database:
    """Open the working-copy Rekordbox DB. Copies from live if missing.

    Parameters
    ----------
    path
        Optional override. Defaults to :data:`paths.REKORDBOX_WORKING_DB`.

    Raises
    ------
    FileNotFoundError
        If neither the working copy nor the live DB can be found.
    """
    target = Path(path) if path is not None else paths.REKORDBOX_WORKING_DB
    if not target.exists():
        copied = paths.copy_live_dbs()
        if copied.get("rekordbox") is None:
            raise FileNotFoundError(
                f"Rekordbox working DB not at {target} and live DB "
                f"{paths.REKORDBOX_LIVE_DB} is missing."
            )
        target = copied["rekordbox"]  # type: ignore[assignment]
    return Rekordbox6Database(path=str(target))


def _safe_name(rel) -> str:
    """Return ``rel.Name`` for a relationship object, else ''."""
    if rel is None:
        return ""
    name = getattr(rel, "Name", None)
    return name or ""


def _to_path(folder_path: str, streaming: bool) -> Path | None:
    if streaming or not folder_path:
        return None
    return Path(folder_path).expanduser()


def _coerce_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_to_str(value) -> str | None:
    if value is None:
        return None
    # pyrekordbox returns datetime objects for DateCreated; stringify safely.
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def iter_tracks(db: Rekordbox6Database) -> Iterator[RBTrack]:
    """Yield every :class:`RBTrack` row in the DB."""
    for t in db.get_content():
        folder_path = t.FolderPath or ""
        streaming = is_streaming_path(folder_path)

        # BPM is stored as BPM*100 integer. Anything else → None.
        bpm_raw = _coerce_int(t.BPM)
        bpm = bpm_raw / 100.0 if bpm_raw else None

        yield RBTrack(
            id=str(t.ID),
            title=t.Title or "",
            artist=_safe_name(t.Artist),
            album=_safe_name(t.Album),
            genre=_safe_name(t.Genre),
            folder_path=folder_path,
            file_path=_to_path(folder_path, streaming),
            is_streaming=streaming,
            bpm=bpm,
            rating=_coerce_int(t.Rating),
            file_size=_coerce_int(t.FileSize),
            date_added=_date_to_str(t.DateCreated),
        )


def iter_playlists(db: Rekordbox6Database) -> Iterator[RBPlaylist]:
    """Yield every :class:`RBPlaylist`. Track order respects ``TrackNo``."""
    for p in db.get_playlist():
        songs = list(getattr(p, "Songs", []) or [])
        songs.sort(key=lambda s: (getattr(s, "TrackNo", 0) or 0))
        track_ids = [str(s.ContentID) for s in songs if getattr(s, "ContentID", None) is not None]

        parent_id = getattr(p, "ParentID", None)
        # Rekordbox uses the literal string "root" for the top-level parent;
        # normalize that to None so callers can treat top-level as a root.
        if parent_id in (None, "", "root"):
            parent_norm: str | None = None
        else:
            parent_norm = str(parent_id)

        yield RBPlaylist(
            id=str(p.ID),
            name=p.Name or "",
            parent_id=parent_norm,
            track_ids=track_ids,
        )


__all__ = [
    "RBTrack",
    "RBPlaylist",
    "open_db",
    "iter_tracks",
    "iter_playlists",
    "is_streaming_path",
]
