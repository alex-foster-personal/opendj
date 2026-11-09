"""Traktor history NMLs -> plays ready for Open DJ's play log (DJIMPORT-10069).

Traktor Pro writes one ``History/history_<yyyy>y<mm>m<dd>d_<hh>h<mm>m<ss>s.nml``
per session: an NML whose PLAYLIST holds one ENTRY per loaded track, with a
PRIMARYKEY naming the file and an EXTENDEDDATA carrying STARTDATE, STARTTIME,
DURATION, PLAYEDPUBLIC and DECK (mimicked from Traktor, no review round).

* ``STARTDATE`` is packed ``year*65536 + month*256 + day``.
* ``STARTTIME`` is seconds since local midnight.
* ``PLAYEDPUBLIC == "1"`` is Traktor's own verdict that the room heard it;
  only those entries become plays.

Reading is pure (no DB). Matching to library tracks uses the same decoded
VOLUME + DIR + FILE location and path resolver as the collection import.
"""

from __future__ import annotations

import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path

from apps.adapters.traktor.nml import NMLDocument, decode_primary_key
from apps.shared.state.play_log import ImportedPlay
from apps.shared.state.sync_stamp import canonical_from

HISTORY_DIRNAME = "History"
PLAY_SOURCE = "traktor-history"
_SECONDS_PER_DAY = 86_400

# ----------------------------------------------------------- data model


@dataclass(frozen=True)
class HistoryPlay:
    """One PLAYEDPUBLIC entry of a history file."""

    origin: str
    #: 0-based position among ALL entries of the file's playlists, public or not.
    index: int
    location: str
    started_at: str
    deck: int | None
    played_s: float | None

    @property
    def play_id(self) -> str:
        return f"{PLAY_SOURCE}:{self.origin}:{self.index}"


@dataclass
class HistoryRead:
    """What the history folder held. ``entries_seen`` is the denominator."""

    plays: list[HistoryPlay] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_rejected: list[tuple[str, str]] = field(default_factory=list)
    entries_seen: int = 0
    entries_not_public: int = 0
    #: No PRIMARYKEY KEY, or a STARTDATE / STARTTIME that does not decode.
    entries_unreadable: int = 0


# ----------------------------------------------------------- decoding


def decode_start(startdate: int, starttime: int, zone: tzinfo | None) -> datetime:
    """Aware UTC datetime of a Traktor STARTDATE + STARTTIME.

    ``zone=None`` means this machine's zone, with the play date's own DST.
    Raises ValueError for a date that does not exist or a time outside a day.
    """
    if not 0 <= starttime < _SECONDS_PER_DAY:
        raise ValueError(f"STARTTIME {starttime} is outside 0..{_SECONDS_PER_DAY - 1}")
    day = date(startdate >> 16, (startdate >> 8) & 0xFF, startdate & 0xFF)
    local = datetime.combine(day, time()) + timedelta(seconds=starttime)
    if zone is None:
        return local.astimezone(UTC)
    if isinstance(zone, tzinfo):
        return local.replace(tzinfo=zone).astimezone(UTC)
    raise TypeError(f"zone must be a tzinfo or None, got {type(zone).__name__}")


def _optional_float(text: str | None) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _deck(text: str | None) -> int | None:
    try:
        return int(text) + 1 if text else None
    except ValueError:
        return None


# ----------------------------------------------------------- reading


def _playlist_entries(doc: NMLDocument) -> list[ET.Element]:
    entries: list[ET.Element] = []
    for node in doc.root.iter("NODE"):
        if node.get("TYPE") == "PLAYLIST":
            for playlist in node.findall("PLAYLIST"):
                entries.extend(playlist.findall("ENTRY"))
    return entries


def _read_entry(entry: ET.Element, origin: str, index: int, zone: tzinfo | None, out: HistoryRead) -> None:
    ext = entry.find("EXTENDEDDATA")
    if ext is None or ext.get("PLAYEDPUBLIC") != "1":
        out.entries_not_public += 1
        return
    primary = entry.find("PRIMARYKEY")
    key = primary.get("KEY", "") if primary is not None else ""
    try:
        started = decode_start(int(ext.get("STARTDATE", "")), int(ext.get("STARTTIME", "")), zone)
    except ValueError:
        out.entries_unreadable += 1
        return
    if not key:
        out.entries_unreadable += 1
        return
    out.plays.append(
        HistoryPlay(
            origin=origin,
            index=index,
            location=decode_primary_key(key),
            started_at=canonical_from(started),
            deck=_deck(ext.get("DECK")),
            played_s=_optional_float(ext.get("DURATION")),
        )
    )


def read_history_file(path: Path, zone: tzinfo | None, out: HistoryRead) -> None:
    """Add one history file's plays and counts to ``out``; raises on a non-NML file."""
    doc = NMLDocument.read(path)
    for index, entry in enumerate(_playlist_entries(doc)):
        out.entries_seen += 1
        _read_entry(entry, path.name, index, zone, out)


def read_history_dir(history_dir: Path, zone: tzinfo | None) -> HistoryRead:
    """Every history file in ``history_dir``; a file that is not NML is named and skipped."""
    out = HistoryRead()
    for path in sorted(p for p in history_dir.iterdir() if p.is_file()):
        if path.suffix.lower() != ".nml":
            out.files_rejected.append((path.name, "not an NML file"))
            continue
        try:
            read_history_file(path, zone, out)
        except (ET.ParseError, ValueError) as exc:
            out.files_rejected.append((path.name, str(exc)))
            continue
        out.files_read.append(path.name)
    return out


def history_dir_for(traktor_dir: Path) -> Path:
    """``<traktor_dir>/History``; FileNotFoundError naming it when absent."""
    history = traktor_dir / HISTORY_DIRNAME
    if not history.is_dir():
        raise FileNotFoundError(f"no Traktor history folder at {history}")
    return history


def default_traktor_dir() -> Path | None:
    """The newest ``~/Documents/Native Instruments/Traktor *`` folder holding History/."""
    root = Path.home() / "Documents" / "Native Instruments"
    found = sorted(p.parent for p in root.glob(f"Traktor */{HISTORY_DIRNAME}") if p.is_dir()) if root.is_dir() else []
    return found[-1] if found else None


# ----------------------------------------------------------- matching


def match_plays(
    plays: list[HistoryPlay], tracks_by_path: dict[str, str], resolve_path: Callable[[str], str]
) -> tuple[list[ImportedPlay], int]:
    """Plays whose file is a library track, plus how many were not matched.

    ``tracks_by_path`` is keyed by NFC path, as ``library_import.live_tracks_by_path`` builds it.
    """
    matched: list[ImportedPlay] = []
    unmatched = 0
    for play in plays:
        stable_id = tracks_by_path.get(unicodedata.normalize("NFC", resolve_path(play.location)))
        if stable_id is None:
            unmatched += 1
            continue
        matched.append(
            ImportedPlay(
                stable_id=stable_id,
                play_id=play.play_id,
                ts=play.started_at,
                source=PLAY_SOURCE,
                origin=play.origin,
                deck=play.deck,
                played_s=play.played_s,
            )
        )
    return matched, unmatched


__all__ = [
    "HISTORY_DIRNAME",
    "PLAY_SOURCE",
    "HistoryPlay",
    "HistoryRead",
    "decode_start",
    "default_traktor_dir",
    "history_dir_for",
    "match_plays",
    "read_history_dir",
    "read_history_file",
]
