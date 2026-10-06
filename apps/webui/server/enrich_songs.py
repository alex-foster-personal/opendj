"""The enrich card counts SONGS, not rows, and an unreadable file is not a failure (ENRICH-03).

Measured on silver's preview data (Tue 6 Oct 2026): 2,268 present rows, only
1,998 distinct files (270 files carry two inferred stable_ids each), and about
1,212 distinct songs (469 songs have two or more copies, mostly "(BACKUP)"
folders). Counting rows told the maintainer he had "2k tracks"; he has about 1.2k songs.

* ``build_song_index``: one song per group of present rows, joined when they
  share a file path (ALWAYS) or the v1 song key: normalized title + artists +
  duration in whole seconds (``SONG_KEY``). The ADR
  ``docs/decisions/ADR-NEW-enrich-card-song-identity.md`` records it and the
  post-v1 move to an acoustic fingerprint. A row missing any key part is its
  own song unless its path joins it to another.
* ``lane_songs`` / ``usable_songs`` / ``step_songs``: the drain's and the
  coverage's per-file sets folded to songs. A song is done when ANY copy is.
* Duds: a file that cannot be read (no codec parameters, no tags or duration,
  gone, nothing decoded) is not a failure of the lane. Its song counts as a
  dud, never as failed, and the files are reported once, apart (``dud_files``).
* ``CFG.RED_FAIL_SHARE``: a lane is red only when REAL failures are at least
  this share of its songs; "not analysed yet" is allowable under the same share.

Requirements (mini-PRD):
  ✔︎ same file, same song
    [if] two rows share a path [then] they are one song, whatever their tags say
    [if] two rows share title, artists and whole-second duration [then] they are one song
  ✔︎ duds are not failures
    [if] every failure of a song is a dud reason [then] it counts in duds, never failed
  ✔︎ red only at the threshold
    [if] real failures are 4.9 % of a lane's songs [then] red is false
    [if] they are 5.0 % [then] red is true
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class CFG:
    #: A lane is red at or above this share of real failures over its songs.
    RED_FAIL_SHARE: float = 0.05
    #: Reason fragment -> the dud label the tooltip shows. A failure whose
    #: reason holds one of these is about the FILE, not the lane.
    DUD_MARKERS: tuple[tuple[str, str], ...] = (
        ("Could not find codec parameters", "the audio cannot be decoded"),
        ("Invalid data found when processing input", "the audio cannot be decoded"),
        ("no tags or no duration", "the file has no readable tags or duration"),
        ("TrackUnreadable", "the audio cannot be decoded"),
        ("TrackVanished", "the file is gone"),
        ("No such file or directory", "the file is gone"),
        ("Nothing was written into output file", "no audio could be decoded"),
        ("produced no strip", "no audio could be decoded"),
    )
    ID_BIND_BATCH: int = 500


SONG_KEY: str = "title + artists + length to the second; two rows for one file always count once"


@dataclass(frozen=True)
class SongIndex:
    #: stable_id -> song id, for every present row.
    song_of: dict[str, str]
    #: stable_id -> file path.
    path_of: dict[str, str]

    @property
    def songs(self) -> int:
        return len(set(self.song_of.values()))

    @property
    def files(self) -> int:
        return len(set(self.path_of.values()))

    def summary(self) -> dict[str, Any]:
        return {"songs": self.songs, "files": self.files, "rows": len(self.song_of), "key": SONG_KEY}


#-----------------------------------------------------------------------------
# the index
#-----------------------------------------------------------------------------
def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def song_key(title: str | None, artists: Sequence[str], duration_ms: int | None) -> str | None:
    """The v1 song key, or None when a part is missing (no dedupe on metadata then)."""
    names = sorted(_norm(a) for a in artists if a and _norm(a))
    if not title or not _norm(title) or not names or not duration_ms or duration_ms <= 0:
        return None
    return f"{_norm(title)}|{'|'.join(names)}|{round(duration_ms / 1000)}"


def build_song_index(rows: Iterable[tuple[str, str, str | None, Sequence[str], int | None]]) -> SongIndex:
    """``rows`` are (stable_id, path, title, artists, duration_ms) for present rows."""
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    path_of: dict[str, str] = {}
    first_by: dict[str, str] = {}
    for sid, path, title, artists, duration_ms in rows:
        parent.setdefault(sid, sid)
        path_of[sid] = unicodedata.normalize("NFC", path)
        for join in (f"path:{path_of[sid]}", song_key(title, artists, duration_ms)):
            if join is None:
                continue
            if join in first_by:
                union(sid, first_by[join])
            else:
                first_by[join] = sid
    return SongIndex(song_of={sid: find(sid) for sid in parent}, path_of=path_of)


def read_song_index(conn_factory: Callable[[], sqlite3.Connection], present: Sequence[tuple[str, str]]) -> SongIndex:
    """The index for ``present`` (stable_id, path) pairs, tags read from ``tracks``."""
    paths = dict(present)
    meta: dict[str, tuple[str | None, list[str], int | None]] = {}
    conn = conn_factory()
    try:
        ids = list(paths)
        for start in range(0, len(ids), CFG.ID_BIND_BATCH):
            batch = ids[start : start + CFG.ID_BIND_BATCH]
            marks = ",".join("?" * len(batch))
            for sid, title, artists_json, duration_ms in conn.execute(
                f"SELECT stable_id, title, artists_json, duration_ms FROM tracks WHERE stable_id IN ({marks})", batch
            ):
                meta[str(sid)] = (title, _artists(artists_json), duration_ms)
    finally:
        conn.close()
    return build_song_index(
        (sid, path, *meta.get(sid, (None, [], None))) for sid, path in paths.items()
    )


def _artists(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw)) if raw else []
    except ValueError:
        return []
    return [str(a) for a in value] if isinstance(value, list) else []


#-----------------------------------------------------------------------------
# duds and the red rule
#-----------------------------------------------------------------------------
def dud_label(reason: str) -> str | None:
    """The dud label for a failure reason, or None when it is a real failure."""
    return next((label for marker, label in CFG.DUD_MARKERS if marker in reason), None)


def is_red(failed: int, total: int, share: float | None = None) -> bool:
    """Real failures at or above the red share of the lane's songs."""
    return total > 0 and failed / total >= (CFG.RED_FAIL_SHARE if share is None else share)


def _members(index: SongIndex, ids: Iterable[str]) -> dict[str, list[str]]:
    songs: dict[str, list[str]] = {}
    for sid in ids:
        if sid in index.song_of:
            songs.setdefault(index.song_of[sid], []).append(sid)
    return songs


#-----------------------------------------------------------------------------
# folding per-file sets to songs
#-----------------------------------------------------------------------------
def lane_songs(
    index: SongIndex,
    denominator: Sequence[str],
    done: Iterable[str],
    declined: Mapping[str, str],
    failed: Mapping[str, str],
) -> dict[str, Any]:
    """One drain lane over songs: done > declined > missing > failed > dud, per song."""
    done_set, declined_set = set(done) - set(declined), set(declined)
    out = {"total": 0, "done": 0, "declined": 0, "missing": 0, "failed": 0, "duds": 0}
    failed_reasons: dict[str, int] = {}
    for members in _members(index, denominator).values():
        out["total"] += 1
        if any(sid in done_set for sid in members):
            out["done"] += 1
        elif any(sid in declined_set for sid in members):
            out["declined"] += 1
        elif any(sid not in failed for sid in members):
            out["missing"] += 1
        elif real := [failed[sid] for sid in members if dud_label(failed[sid]) is None]:
            out["failed"] += 1
            failed_reasons[real[0]] = failed_reasons.get(real[0], 0) + 1
        else:
            out["duds"] += 1
    return {**out, "failed_reasons": failed_reasons, "red": is_red(out["failed"], out["total"])}


def usable_songs(
    index: SongIndex, present: Sequence[str], library_sources: Mapping[str, str], own_done: set[str]
) -> dict[str, Any]:
    """BPM or key over songs: ready when any copy has a value from any source."""
    by_source: dict[str, int] = {}
    total = ready = 0
    for members in _members(index, present).values():
        total += 1
        source = next((library_sources[sid] for sid in members if sid in library_sources), None)
        if source is None and any(sid in own_done for sid in members):
            source = "open_dj"
        if source is None:
            continue
        ready += 1
        by_source[source] = by_source.get(source, 0) + 1
    ranked = dict(sorted(by_source.items(), key=lambda item: (-item[1], item[0])))
    none = total - ready
    return {
        "denominator": "songs", "total": total, "ready": ready, "none": none, "by_source": ranked,
        "allowable": not is_red(none, total),
    }


def step_songs(
    index: SongIndex, present: Sequence[str], done: set[str], terminal: set[str], failed: set[str]
) -> dict[str, int | bool]:
    """One coverage step (stems, lyrics, ...) over songs: done > terminal > pending > failed."""
    out = {"done": 0, "terminal": 0, "pending": 0, "failed": 0}
    for members in _members(index, present).values():
        if any(sid in done for sid in members):
            out["done"] += 1
        elif any(sid in terminal for sid in members):
            out["terminal"] += 1
        elif any(sid not in failed for sid in members):
            out["pending"] += 1
        else:
            out["failed"] += 1
    return {**out, "red": is_red(out["failed"], sum(out.values()))}


LaneInput = tuple[Sequence[str], set[str], Mapping[str, str], Mapping[str, str]]


def drain_song_view(
    index: SongIndex,
    present: Sequence[str],
    inputs: Mapping[str, LaneInput],
    values: Mapping[str, Mapping[str, str]],
    lanes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Adds ``songs`` (and ``usable_songs`` for BPM/key) to each lane in ``lanes``
    and returns the drain-level ``songs`` summary and ``duds``."""
    for lane, (ids, done, declined, failed) in inputs.items():
        lanes[lane]["songs"] = lane_songs(index, ids, done, declined, failed)
        if lane in values:
            own = set(done) - set(declined)
            lanes[lane]["usable_songs"] = usable_songs(index, present, values[lane], own)
    summary = {**index.summary(), "red_fail_share": CFG.RED_FAIL_SHARE}
    return {"songs": summary, "duds": dud_files(index, (failed for *_rest, failed in inputs.values()))}


def dud_files(index: SongIndex, failures: Iterable[Mapping[str, str]]) -> dict[str, Any]:
    """Distinct FILES some lane could not read, with a count per dud label."""
    labels: dict[str, str] = {}
    for lane_failures in failures:
        for sid, why in lane_failures.items():
            label = dud_label(why)
            if label is not None and sid in index.path_of:
                labels.setdefault(index.path_of[sid], label)
    reasons: dict[str, int] = {}
    for label in labels.values():
        reasons[label] = reasons.get(label, 0) + 1
    return {"files": len(labels), "reasons": reasons}


__all__ = [
    "CFG",
    "SONG_KEY",
    "SongIndex",
    "build_song_index",
    "LaneInput",
    "dud_files",
    "drain_song_view",
    "dud_label",
    "is_red",
    "lane_songs",
    "read_song_index",
    "song_key",
    "step_songs",
    "usable_songs",
]
