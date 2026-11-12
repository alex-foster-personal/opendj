"""Serato history sessions (``_Serato_/History/Sessions/*.session``) reader.

Clean room: layout only, as documented by public Serato history parsers; no
source copied (mimicked, no review round). A session file is a top-level tag
stream, each tag ``type`` (4 ASCII bytes) + ``length`` (uint32 BE) + payload:
``vrsn`` (version string), ``oses`` (session header) and one ``oent`` per deck
load. An ``oent`` holds one ``adat`` block whose payload is a field stream,
each field ``id`` (uint32 BE) + ``length`` (uint32 BE) + value. Field ids
read here are the module ``FIELD_*`` constants; any other id is skipped by its
length. ``history.database`` (the session index) is not needed: session files
are globbed directly.

A truncated tag or field stops the read of that file with
:class:`SessionTruncated`, naming the file and the absolute offset of the
header that ran out; entries read before it are kept.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

FIELD_PATH = 2
FIELD_TITLE = 6
FIELD_ARTIST = 7
FIELD_START = 28
FIELD_END = 29
FIELD_DECK = 31
FIELD_PLAYTIME = 45
FIELD_PLAYED = 50

_TAG = struct.Struct(">4sI")
_FIELD = struct.Struct(">II")
_STRING_FIELDS = {FIELD_PATH: "path", FIELD_TITLE: "title", FIELD_ARTIST: "artist"}
_INT_FIELDS = {
    FIELD_START: "start_s",
    FIELD_END: "end_s",
    FIELD_DECK: "deck",
    FIELD_PLAYTIME: "playtime_s",
}


@dataclass(frozen=True)
class HistoryEntry:
    """One deck load in a Serato session, played or not."""

    session_file: str
    index: int
    path: str | None = None
    title: str | None = None
    artist: str | None = None
    start_s: int | None = None
    end_s: int | None = None
    deck: int | None = None
    playtime_s: int | None = None
    played: bool = False

    @property
    def play_id(self) -> str:
        """Stable across reruns and across moving the ``_Serato_`` folder."""
        return f"serato-{Path(self.session_file).stem}-{self.index}-{self.start_s}"


@dataclass(frozen=True)
class SessionRead:
    file: Path
    entries: tuple[HistoryEntry, ...]
    error: str | None


class SessionTruncated(ValueError):
    def __init__(
        self, file: str, offset: int, what: str, entries: tuple[HistoryEntry, ...] = ()
    ) -> None:
        super().__init__(f"{file}: truncated at offset {offset}: {what}")
        self.file = file
        self.offset = offset
        self.entries = entries


class SessionMalformed(ValueError):
    def __init__(
        self, file: str, offset: int, detail: str, entries: tuple[HistoryEntry, ...] = ()
    ) -> None:
        super().__init__(f"{file}: malformed field at offset {offset}: {detail}")
        self.file = file
        self.offset = offset
        self.entries = entries


def _frames(
    data: bytes, start: int, end: int, header: struct.Struct, file: str, what: str
) -> list[tuple[int, bytes | int, int, int]]:
    """``(header offset, key, value start, value end)`` for each frame in ``data[start:end]``."""
    out: list[tuple[int, bytes | int, int, int]] = []
    pos = start
    while pos < end:
        if end - pos < header.size:
            raise SessionTruncated(file, pos, f"{what} header")
        key, length = header.unpack_from(data, pos)
        value_start = pos + header.size
        if value_start + length > end:
            raise SessionTruncated(file, pos, f"{what} of {length} bytes")
        out.append((pos, key, value_start, value_start + length))
        pos = value_start + length
    return out


def _entry(data: bytes, start: int, end: int, file: str, index: int) -> HistoryEntry:
    values: dict[str, object] = {}
    for _, tag, a_start, a_end in _frames(data, start, end, _TAG, file, "tag"):
        if tag != b"adat":
            continue
        for f_offset, fid, v_start, v_end in _frames(data, a_start, a_end, _FIELD, file, "field"):
            value = data[v_start:v_end]
            if fid in _STRING_FIELDS:
                try:
                    values[_STRING_FIELDS[fid]] = value.decode("utf-16-be").rstrip("\x00")
                except UnicodeDecodeError as exc:
                    raise SessionMalformed(file, f_offset, "invalid utf-16-be") from exc
            elif fid in _INT_FIELDS:
                if len(value) == 0:
                    if fid == FIELD_START:
                        continue
                    raise SessionMalformed(file, f_offset, "empty integer field")
                values[_INT_FIELDS[fid]] = int.from_bytes(value, "big")
            elif fid == FIELD_PLAYED:
                values["played"] = value[:1] == b"\x01"
    return HistoryEntry(session_file=file, index=index, **values)  # type: ignore[arg-type]


def _read_entries(data: bytes, session_file: str, entries: list[HistoryEntry]) -> None:
    pos, end = 0, len(data)
    while pos < end:
        if end - pos < _TAG.size:
            raise SessionTruncated(session_file, pos, "tag header")
        tag, length = _TAG.unpack_from(data, pos)
        v_start = pos + _TAG.size
        if v_start + length > end:
            raise SessionTruncated(session_file, pos, f"tag of {length} bytes")
        if tag == b"oent":
            entries.append(_entry(data, v_start, v_start + length, session_file, len(entries)))
        pos = v_start + length


def parse_session(data: bytes, session_file: str) -> tuple[HistoryEntry, ...]:
    """Every ``oent`` of one session file, in file order.

    A truncation raises :class:`SessionTruncated` carrying the entries read before it.
    """
    entries: list[HistoryEntry] = []
    try:
        _read_entries(data, session_file, entries)
    except (SessionTruncated, SessionMalformed) as exc:
        exc.entries = tuple(entries)
        raise
    return tuple(entries)


def read_session(path: Path) -> SessionRead:
    data = path.read_bytes()
    try:
        return SessionRead(path, parse_session(data, path.name), None)
    except (SessionTruncated, SessionMalformed) as exc:
        return SessionRead(path, exc.entries, str(exc))


def session_files(serato_dir: Path) -> list[Path]:
    sessions = Path(serato_dir) / "History" / "Sessions"
    if not sessions.is_dir():
        raise FileNotFoundError(f"no Serato history sessions folder at {sessions}")
    return sorted(sessions.glob("*.session"))


__all__ = [
    "FIELD_ARTIST",
    "FIELD_DECK",
    "FIELD_END",
    "FIELD_PATH",
    "FIELD_PLAYED",
    "FIELD_PLAYTIME",
    "FIELD_START",
    "FIELD_TITLE",
    "HistoryEntry",
    "SessionMalformed",
    "SessionRead",
    "SessionTruncated",
    "parse_session",
    "read_session",
    "session_files",
]
