"""Sources-map matcher: a COPY batch's MIK songs -> ``tracks.stable_id``.

A fresh Mixed In Key analysis is run on COPIES (MIK rewrites every file it
analyzes), so the store records the copy's path and the three tiers in
:mod:`apps.mik.match` cannot bind it: measured Tue 13 Oct 2026, 11 of 106.
The batch folder carries ``.sources.txt``, one ORIGINAL path per line, and
that is the join.

The rule, and nothing looser:

* a song belongs to the batch only when its path is directly under the batch
  folder; every other song in the store is ignored and counted;
* the song's basename must name EXACTLY ONE source line;
* that line must name EXACTLY ONE live library row, by EQUALITY (never a
  suffix, never a basename) on either the whole path, or the path below the
  mounted library root against ``tracks.file_path`` below its ``/Users/<name>``
  home (the originals sit under a mounted root while the row records the same
  file under a home directory);
* both sides are NFC-normalized, because names are stored decomposed on disk.

Anything else is unmatched with a reason and a count. Nothing is guessed and
nothing is dropped.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from apps.shared.state.locations import list_location_paths

from .load_types import LoadError
from .match import Match, MatchReport, Tier, Unmatched, UnmatchedReason, norm_path
from .mikdb import MikSong

SOURCES_FILE = ".sources.txt"
MANIFEST_FILE = ".batch.json"
"""Stage's record of a batch, including the library root its source lines sit under."""
BATCH_PREFIX = "mik-batch-"
"""Every copy batch folder's name starts with this (stage writes them, collect reads them)."""
SOURCES_TIER: Tier = "sources_map"
HOME_PARENT = "Users"
HOME_PREFIX = re.compile(rf"^(?:[A-Za-z]:)?/{HOME_PARENT}/[^/]+")
"""A row's home directory in a path KEY: ``/Users/<name>``, or ``C:/Users/<name>`` on Windows."""
_HOME_PREFIX_ANY_CASE = re.compile(HOME_PREFIX.pattern, re.IGNORECASE)
HOST_PLATFORM: str = sys.platform
"""The platform production callers pass to :func:`path_key`: the host's own."""
CASE_INSENSITIVE_PLATFORMS: frozenset[str] = frozenset({"darwin", "win32"})
"""Platforms whose DEFAULT volumes ignore case (APFS and NTFS as shipped), so
two spellings that differ only in case are one file there. Linux is not one:
there ``Track.mp3`` and ``track.mp3`` are two files."""
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")

# detail code -> the staging table's reason vocabulary (its CHECK admits only
# these three), so the precise cause survives in ``detail`` and the counts.
DETAIL_REASON: dict[str, UnmatchedReason] = {
    "no_source_line": "no_candidate",
    "several_source_lines": "ambiguous_candidates",
    "source_outside_library_root": "no_candidate",
    "no_library_row": "no_candidate",
    "several_library_rows": "ambiguous_candidates",
    "row_claimed_by_another_copy": "lost_collision",
}


@dataclass
class SourcesMatch:
    """The match report for one copy batch, plus what was left out and why."""

    report: MatchReport = field(default_factory=MatchReport)
    songs: list[MikSong] = field(default_factory=list)
    """Only the songs under the batch folder: the ones a plan may touch."""
    store_songs: int = 0
    ignored_outside_batch: int = 0
    source_lines: int = 0
    sources: dict[int, str] = field(default_factory=dict)
    """MIK row id -> its one source line, for every song that had exactly one."""
    details: dict[int, str] = field(default_factory=dict)
    """MIK row id -> detail code, for every unmatched song."""
    candidates: dict[int, list[str]] = field(default_factory=dict)
    """MIK row id -> the rows its source line equals, when there are several.
    Never a match here; :mod:`apps.mik.duplicates` may share to them when
    they are all one recording."""

    def candidates_of(self, song: MikSong) -> list[str]:
        return self.candidates.get(song.row_id, [])

    def by_detail(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for code in self.details.values():
            counts[code] = counts.get(code, 0) + 1
        return counts

    def counts(self) -> dict[str, int]:
        return {
            "store_songs": self.store_songs,
            "batch_songs": len(self.songs),
            "ignored_outside_batch": self.ignored_outside_batch,
            "source_lines": self.source_lines,
            "resolved": len(self.report.matches),
            "unresolved": len(self.report.unmatched),
        }

    def payload(self) -> dict[str, object]:
        return {**self.counts(), "unresolved_by_detail": self.by_detail()}


# ----------------------------------------------------------------- inputs


def read_source_lines(batch_dir: Path) -> list[str]:
    """The NFC-normalized, non-blank lines of the batch's ``.sources.txt``."""
    path = batch_dir / SOURCES_FILE
    if not path.is_file():
        raise LoadError(f"batch folder {batch_dir} has no {SOURCES_FILE}; it is not a copy batch")
    lines = [
        normalized
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and (normalized := norm_path(line)) is not None  # a name may begin or end with spaces
    ]
    if not lines:
        raise LoadError(f"{path} is empty; a copy batch names one original path per line")
    return lines


def read_batch_library_root(batch_dir: Path) -> str | None:
    """The library root stage recorded in the batch's ``.batch.json`` (None: originals bind whole)."""
    manifest = batch_dir / MANIFEST_FILE
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LoadError(f"{manifest} is unreadable ({error})") from error
    recorded = payload.get("library_root", ...) if isinstance(payload, dict) else ...
    if recorded is not None and not isinstance(recorded, str):
        raise LoadError(f"{manifest}: library_root must be a path string or null, not {recorded!r}")
    return recorded or None


@dataclass(frozen=True)
class RowIndex:
    """Live rows by path KEY: the whole path, and the path below its home.

    Keyed for ONE platform: every key looked up in it must use the same one.
    """

    platform: str
    by_full: dict[str, set[str]]
    by_below_home: dict[str, set[str]]


def row_index(conn: sqlite3.Connection, *, platform: str) -> RowIndex:
    """Index every live row by its legacy path AND its live local locations.

    A row can be playable only through a ``track_locations`` entry whose path
    shares nothing with ``tracks.file_path``; that location is where the
    source file really is, so it binds exactly like the legacy path.
    """
    index = RowIndex(platform, {}, {})
    rows = conn.execute("SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL").fetchall()
    for stable_id, file_path in rows:
        if file_path:
            _index_path(index, stable_id, file_path)
    for stable_id, paths in live_local_locations(conn, [row[0] for row in rows]).items():
        for path in paths:
            _index_path(index, stable_id, path)
    return index


def live_local_locations(conn: sqlite3.Connection, stable_ids: list[str]) -> dict[str, list[str]]:
    """The live LOCAL location paths per row: the ones deck load can open."""
    return list_location_paths(conn, stable_ids, live_only=True, kinds=("local",))


def path_key(path: str | None, *, platform: str) -> str | None:
    """The spelling two paths are compared by on ``platform``. Never used for I/O.

    NFC, ``/``-separated, no trailing ``/``: a Windows path (drive letter or
    UNC) is respelled with ``/``, so ``C:\\Users\\dev\\x.mp3`` keys as
    ``C:/Users/dev/x.mp3``. On a case-insensitive platform the key is also
    casefolded, because there both spellings name one file; on Linux case is
    kept. The rule is the platform's DEFAULT volume: a stored path may come
    from another machine, so its volume cannot be probed. A case-sensitive
    APFS or external volume holding ``Track.mp3`` and ``track.mp3`` as two
    files is a known limit (``(private path)``). Works on a bare file name too (a batch is flat, so a copy's name is
    its key inside the batch).
    """
    spelled = path_spelling(path)
    if spelled is None:
        return None
    return spelled.casefold() if platform in CASE_INSENSITIVE_PLATFORMS else spelled


def path_spelling(path: str | None) -> str | None:
    """:func:`path_key` before any case rule: NFC and ``/``-separated, case as written."""
    text = norm_path(path)
    if text is None:
        return None
    if _WINDOWS_ABSOLUTE.match(text):
        text = PureWindowsPath(text).as_posix()
    return str(PurePosixPath(text))


def strip_home(key: str, *, platform: str) -> tuple[str, bool]:
    """``(key below its /Users/<name> home, whether it had one)``, matching the key's case rule."""
    prefix = _HOME_PREFIX_ANY_CASE if platform in CASE_INSENSITIVE_PLATFORMS else HOME_PREFIX
    below, replaced = prefix.subn("", key, count=1)
    return below, bool(replaced)


def _basename(key: str) -> str:
    return PurePosixPath(key).name


def _below(key: str, root: str) -> str | None:
    """``key`` below ``root`` with a leading ``/``, or None when it is not under it."""
    path, base = PurePosixPath(key), PurePosixPath(root)
    if path == base or not path.is_relative_to(base):
        return None
    return f"/{path.relative_to(base).as_posix()}"


def _index_path(index: RowIndex, stable_id: str, raw: str) -> None:
    full = path_key(raw, platform=index.platform)
    if full is None:
        return
    index.by_full.setdefault(full, set()).add(stable_id)
    below_home, replaced = strip_home(full, platform=index.platform)
    if replaced:
        index.by_below_home.setdefault(below_home, set()).add(stable_id)


def _in_batch(song: MikSong, batch_key: str, platform: str) -> bool:
    """True when the song's file sits directly in the batch folder."""
    key = path_key(song.path, platform=platform)
    return key is not None and PurePosixPath(key).parent == PurePosixPath(batch_key)


# --------------------------------------------------------------- matching


def _lines_by_basename(lines: list[str], platform: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for line in lines:
        index.setdefault(_basename(path_key(line, platform=platform) or ""), []).append(line)
    return index


def _rows_for_line(line: str, root: str | None, rows: RowIndex) -> tuple[set[str], str | None, str]:
    """``(rows equal to the line, why it does not resolve or None, detail)``.

    Equality on the whole path, or on the path below ``root`` against a row's
    path below its home. Exactly one row resolves; anything else has a code.
    """
    key = path_key(line, platform=rows.platform) or ""
    hits = set(rows.by_full.get(key, set()))
    below = _below(key, root) if root else None
    under_root = below is not None
    if below is not None:
        hits |= rows.by_below_home.get(below, set())
    if len(hits) == 1:
        return hits, None, ""
    if len(hits) > 1:
        return hits, "several_library_rows", f"{len(hits)} rows equal {line!r}: {','.join(sorted(hits)[:4])}"
    if root and not under_root:
        return hits, "source_outside_library_root", f"{line!r} is not under {root!r}"
    return hits, "no_library_row", f"0 rows equal {line!r} below the library root"


def rows_for_source_line(line: str, library_root: str | None, rows: RowIndex) -> set[str]:
    """Every row a source line binds to under collect's rule: whole path, or path below the library root."""
    return _rows_for_line(line, path_key(library_root, platform=rows.platform), rows)[0]


def _unmatched(
    result: SourcesMatch, song: MikSong, code: str, detail: str, claimed: str | None = None
) -> None:
    result.details[song.row_id] = code
    result.report.unmatched[song.row_id] = Unmatched(
        mik_row_id=song.row_id, reason=DETAIL_REASON[code], detail=f"{code}: {detail}", claimed_stable_id=claimed
    )


def _bind_one(
    result: SourcesMatch,
    song: MikSong,
    line: str,
    root: str | None,
    rows: RowIndex,
    claimed: dict[str, list[MikSong]],
) -> None:
    result.sources[song.row_id] = line
    hits, code, detail = _rows_for_line(line, root, rows)
    if code is not None:
        if code == "several_library_rows":
            result.candidates[song.row_id] = sorted(hits)
        _unmatched(result, song, code, detail)
    elif (stable_id := next(iter(hits))) in claimed:
        claimed[stable_id].append(song)
        _unmatched(
            result,
            song,
            "row_claimed_by_another_copy",
            f"track {stable_id} already resolved by MIK row {claimed[stable_id][0].row_id}",
            claimed=stable_id,
        )
    else:
        claimed[stable_id] = [song]
        result.report.matches[song.row_id] = Match(mik_row_id=song.row_id, stable_id=stable_id, tier=SOURCES_TIER)


def _drop_disagreeing_claims(result: SourcesMatch, claimed: dict[str, list[MikSong]]) -> None:
    """A row claimed by analyses that disagree (energy or series) gets none of them.

    Keeping the first claim would make the written value depend on row id
    order; agreeing duplicates of one copy still bind through the first.
    """
    for stable_id, songs in claimed.items():
        if len({(song.energy, song.segments) for song in songs}) > 1:
            winner = songs[0]
            del result.report.matches[winner.row_id]
            _unmatched(
                result,
                winner,
                "row_claimed_by_another_copy",
                f"track {stable_id} is claimed by {len(songs)} MIK rows that disagree; none is written",
                claimed=stable_id,
            )


def bind_by_sources(
    songs: list[MikSong],
    lines: list[str],
    rows: RowIndex,
    *,
    batch_folder: str,
    library_root: str | None,
) -> SourcesMatch:
    """The pure core of :func:`match_songs_by_sources`, over path STRINGS.

    Takes strings so either path flavor can be checked on any machine; the
    platform whose case rule applies is the one ``rows`` was keyed for.
    """
    platform = rows.platform
    batch_key = path_key(batch_folder, platform=platform) or ""
    root = path_key(library_root, platform=platform)
    by_basename = _lines_by_basename(lines, platform)
    result = SourcesMatch(store_songs=len(songs), source_lines=len(lines))
    result.songs = [song for song in songs if _in_batch(song, batch_key, platform)]
    result.ignored_outside_batch = len(songs) - len(result.songs)
    claimed: dict[str, list[MikSong]] = {}
    for song in sorted(result.songs, key=lambda item: item.row_id):
        name = _basename(path_key(song.path, platform=platform) or "")
        named = by_basename.get(name, [])
        if not named:
            _unmatched(result, song, "no_source_line", f"0 source lines name {name!r}")
        elif len(named) > 1:
            _unmatched(result, song, "several_source_lines", f"{len(named)} source lines name {name!r}")
        else:
            _bind_one(result, song, named[0], root, rows, claimed)
    _drop_disagreeing_claims(result, claimed)
    return result


def match_songs_by_sources(
    songs: Iterable[MikSong],
    conn: sqlite3.Connection,
    *,
    batch_dir: Path,
    library_root: Path | None,
    platform: str,
) -> SourcesMatch:
    """Bind each batch song to at most one row through ``.sources.txt``.

    ``library_root=None`` means the originals are stored under their own
    on-disk paths: only whole-path equality can bind.
    """
    return bind_by_sources(
        list(songs),
        read_source_lines(batch_dir),
        row_index(conn, platform=platform),
        batch_folder=str(batch_dir),
        library_root=str(library_root) if library_root is not None else None,
    )


__all__ = [
    "BATCH_PREFIX",
    "CASE_INSENSITIVE_PLATFORMS",
    "DETAIL_REASON",
    "HOME_PREFIX",
    "HOST_PLATFORM",
    "SOURCES_FILE",
    "SOURCES_TIER",
    "RowIndex",
    "SourcesMatch",
    "bind_by_sources",
    "live_local_locations",
    "match_songs_by_sources",
    "path_key",
    "path_spelling",
    "read_source_lines",
    "row_index",
    "strip_home",
]
