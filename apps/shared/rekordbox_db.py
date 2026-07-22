"""Thin typed wrapper around :mod:`pyrekordbox` for Rekordbox 6/7 master.db.

We do not re-implement any ORM — we just expose a small set of dataclasses
(:class:`RBTrack`, :class:`RBPlaylist`) and iterator helpers that hide the
rough edges (nullable relationships, BPM×100 storage, streaming FolderPaths).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

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
    folder_path: str  # raw FolderPath -- may be absolute path, URI, or empty
    file_path: Path | None  # None for streaming / empty
    is_streaming: bool
    bpm: float | None
    rating: int | None
    file_size: int | None
    date_added: str | None
    # Phase 15 widening: ISRC (tier-1 stable_id seed, Phase 9 spotify match)
    # and duration in seconds (Phase 2 matcher signal #4). Optional with
    # defaults so existing RBTrack(...) callsites stay backward-compatible.
    isrc: str | None = None
    duration_s: float | None = None


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

        # BPM is stored as BPM*100 integer. Anything else -> None.
        bpm_raw = _coerce_int(t.BPM)
        bpm = bpm_raw / 100.0 if bpm_raw else None

        # DjmdContent.Length is stored in WHOLE SECONDS (verified against live
        # data: sound-effect samples read Length=5/7, full tracks Length=491
        # for an 8:11 / 15.8 MB @256 kbps file). The previous code divided by
        # 1000 (treating it as ms), making every duration 1000x too small and
        # silently killing the matcher/relocator ``duration_match`` signal.
        # Fall back to None when missing.
        length_s = _coerce_int(getattr(t, "Length", None))
        duration_s = float(length_s) if length_s is not None else None

        isrc_raw = getattr(t, "ISRC", None)
        isrc = isrc_raw.strip() if isinstance(isrc_raw, str) and isrc_raw.strip() else None

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
            isrc=isrc,
            duration_s=duration_s,
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


# ----- Phase 4: cue + analysis readers ---------------------------------


def _rb_kind_to_normalised(kind: int | None, index_hint: int | None) -> tuple[str, int | None]:
    """Map pyrekordbox ``DjmdCue.Kind`` to a ``NormalisedCue`` kind/index.

    Kind semantics (pyrekordbox ``db6/tables.py`` DjmdCue):
      * 0 = memory cue.
      * 1..8 = hot cue slot (1-indexed in RB -> we surface 0..7).
      * Some builds use 3 for load cue and 4 for loop; treat 4 as loop
        when ``ActiveLoop`` is true, otherwise fold into hot.
    """
    k = _coerce_int(kind)
    if k is None or k == 0:
        return "memory", None
    if 1 <= k <= 8:
        return "hot", (index_hint if index_hint is not None else k - 1)
    if k == 4:
        return "loop", None
    return "hot", index_hint


def iter_cues(db: Rekordbox6Database, content_id: str) -> list:
    """Return the normalised cue list for a RB track by ContentID."""
    from .normalised import NormalisedCue
    from .rb_color_palette import color_index_to_rgb

    cues: list = []
    # Prefer the ORM's filtered query; fall back to scanning all rows.
    rows = []
    try:
        rows = list(db.get_cue(ContentID=str(content_id)))
    except Exception:
        try:
            rows = [r for r in db.get_cue() if str(getattr(r, "ContentID", "")) == str(content_id)]
        except Exception:
            rows = []
    for row in rows:
        in_msec = _coerce_int(getattr(row, "InMsec", None))
        if in_msec is None:
            continue
        kind = getattr(row, "Kind", None)
        color_idx = _coerce_int(getattr(row, "Color", None))
        active_loop = bool(getattr(row, "ActiveLoop", False))
        out_msec = _coerce_int(getattr(row, "OutMsec", None))
        comment = getattr(row, "Comment", None) or None
        # pyrekordbox doesn't expose an explicit hot-cue slot column; some
        # builds use ``Kind - 1`` as the slot. Fall back to enumeration order.
        kind_str, index = _rb_kind_to_normalised(kind, None)
        if (kind_str == "hot" and index is None):
            index = len([c for c in cues if c.kind == "hot"])
        if kind_str == "hot" and index is not None and index > 7:
            index = None
        if active_loop and out_msec and out_msec > in_msec:
            kind_str = "loop"
            index = None
        loop_length = (out_msec - in_msec) if (kind_str == "loop" and out_msec) else None
        cue = NormalisedCue(
            position_msec=int(in_msec),
            kind=kind_str,  # type: ignore[arg-type]
            index=index,
            color_rgb=color_index_to_rgb(color_idx),
            name=str(comment) if comment else None,
            loop_length_msec=loop_length,
        )
        cues.append(cue)
    cues.sort(
        key=lambda c: (
            c.position_msec,
            c.kind,
            c.index if c.index is not None else -1,
        )
    )
    return cues


def iter_analysis(db: Rekordbox6Database) -> "Iterator":
    """Yield ``NormalisedAnalysis`` for every RB track.

    BPM is read from ``DjmdContent.BPM`` (stored as BPM×100). Key is
    resolved through the harmonic module's Camelot mapping. Energy uses
    ``ColorID`` as the one-to-one proxy (see 04-RESEARCH §1).
    """
    from .harmonic import key_to_camelot
    from .normalised import NormalisedAnalysis

    for t in db.get_content():
        bpm_raw = _coerce_int(t.BPM)
        bpm = bpm_raw / 100.0 if bpm_raw else None
        color_id = _coerce_int(getattr(t, "ColorID", None))
        key_name = _safe_name(getattr(t, "Key", None))
        camelot = None
        if key_name:
            try:
                camelot = str(key_to_camelot(key_name))
            except ValueError:
                camelot = None
        yield NormalisedAnalysis(
            uuid_or_id=str(t.ID),
            source="rb",
            bpm=bpm,
            manual_bpm=None,
            key_camelot=camelot,
            energy=color_id,
            tags=None,
            is_straight_grid=None,
        )


__all__ = [
    "RBTrack",
    "RBPlaylist",
    "open_db",
    "iter_tracks",
    "iter_playlists",
    "iter_cues",
    "iter_analysis",
    "is_streaming_path",
]
