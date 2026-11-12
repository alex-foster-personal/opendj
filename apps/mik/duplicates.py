"""Duplicate copies of one recording share MIK energy.

the maintainer's standing rule: duplicate copies of one recording share their analysis.
MIK cannot tell two library rows for one recording apart (one basename, two
rows), so the three tiers leave such a song ``ambiguous_candidates`` and its
energy reaches no row at all.

**Which rows are one recording.** Four bases, the last opt-in:

``same_file`` (always on)
    Two present rows whose audio resolves to the SAME file on disk
    (``track_availability.checked_path``, NFC). Not a heuristic: the deck
    would open one file for both. Measured Tue 13 Oct 2026: 2,637 present
    rows resolve to 1,482 files, up to 11 rows on one file.

``same_stored_path`` (always on)
    Two live rows whose ``tracks.file_path`` is the IDENTICAL spelling (NFC,
    ``/``-separated, case as written), whatever their availability. Each row
    records the same file, which is the same rule ``same_file`` applies to an
    identical checked path, so it is not a heuristic either. ``same_file``
    alone cannot see it when one of the rows is absent or was relinked:
    measured Thu 29 Oct 2026 on a copy of the desktop library, 35 batch songs
    bound by ``.sources.txt`` to two such rows reached neither, 34 of them with
    a present row, including two tracks of the OLTF crate.

``identity_hash`` (always on)
    The app's own definition, reused and not redefined:
    :func:`apps.sync_hub.sync_set.identity_duplicate_remap`, the rule the hub
    collapses duplicates with (equal ``content_hash`` or ``audio_hash``, else an
    equal normalizable ISRC).

``artist_title_duration`` (only with ``--share-across-duplicates``)
    Most rows carry no hash (``audio_hash`` is NULL for nearly all of them),
    so the identity rule covers few. The strict fallback: the same normalized
    artist list AND title, AND every duration in the group within
    :data:`CFG.duration_tolerance_ms` of every other. A row with no title, no
    artist or no duration is never grouped. It is a heuristic, which is why it
    is off unless asked for.

**What is shared.** Only :data:`SHARED_FIELDS` (the energy scalar and series).
A song with a home in one group -- its unique match, or ambiguous candidates
that ALL sit in one group -- is offered to every row of that group that has
no unique match of its own. A row offered different energies by different
songs is a conflict: it is written by none of them and reported.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from apps.shared.state.ingest.path_collisions import _file_identity
from apps.sync_hub.sync_set import identity_duplicate_remap

from . import sources_match as sources
from .load_plan import SHARED_FIELDS
from .load_types import LoadError
from .match import TIER_ORDER, Match, MatchReport, Tier, TrackIndex, norm_loose
from .mikdb import MikSong

SHARE_TIER: Tier = "shared_duplicate"
BASIS_SAME_FILE = "same_file"
BASIS_SAME_STORED_PATH = "same_stored_path"
BASIS_IDENTITY = "identity_hash"
BASIS_METADATA = "artist_title_duration"


@dataclass(frozen=True)
class _Config:
    duration_tolerance_ms: int = 1000
    """The widest duration spread inside one ``artist_title_duration`` group."""


CFG = _Config()


# ------------------------------------------------------------------ groups


@dataclass
class DuplicateGroups:
    """A partition of live rows into recordings. A lone row is its own group."""

    _root: dict[str, str] = field(default_factory=dict)
    _members: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _basis: dict[str, str] = field(default_factory=dict)

    def group_of(self, stable_id: str) -> str:
        return self._root.get(stable_id, stable_id)

    def members_of(self, stable_id: str) -> tuple[str, ...]:
        return self._members.get(self.group_of(stable_id), (stable_id,))

    def basis_of(self, stable_id: str) -> str | None:
        return self._basis.get(self.group_of(stable_id))

    def by_basis(self) -> dict[str, int]:
        """Groups of two or more rows, counted by what proved them."""
        counts: dict[str, int] = {}
        for basis in self._basis.values():
            counts[basis] = counts.get(basis, 0) + 1
        return counts


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.bases: dict[str, set[str]] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str, basis: str) -> None:
        low, high = sorted((self.find(left), self.find(right)))
        merged = self.bases.pop(high, set()) | self.bases.pop(low, set()) | {basis}
        self.parent[high] = low
        self.bases[low] = merged


def parse_artists(stable_id: str, artists_json: str | None) -> list[str]:
    """A row's artists, refusing ``artists_json`` that is not a JSON list of strings.

    Corrupt artist data is never read as "no artist": that would quietly drop
    the row out of metadata duplicate grouping and out of status.
    """
    if artists_json is None:
        return []
    try:
        artists = json.loads(artists_json)
    except json.JSONDecodeError as error:
        raise LoadError(f"track {stable_id}: artists_json is not valid JSON ({error})") from error
    if not isinstance(artists, list) or not all(isinstance(artist, str) for artist in artists):
        raise LoadError(f"track {stable_id}: artists_json is not a list of strings: {artists_json!r}")
    return artists


def _artists_key(stable_id: str, artists_json: str | None) -> str | None:
    """Every artist, normalized and order-preserved, or ``None`` if there is none."""
    keys = [key for artist in parse_artists(stable_id, artists_json) if (key := norm_loose(artist))]
    return "\x1f".join(keys) or None


def artist_title_duration_groups(conn: sqlite3.Connection) -> Iterable[list[str]]:
    """Rows sharing artists + title, cut into runs no wider than the tolerance.

    A run starts at its shortest duration and closes at the first row more
    than the tolerance above it, so every pair inside a run is within the
    tolerance. Two rows 0.9 s apart can still land in neighboring runs; that
    errs toward NOT sharing, which is the safe side.
    """
    buckets: dict[tuple[str, str], list[tuple[int, str]]] = {}
    query = (
        "SELECT stable_id, title, artists_json, duration_ms FROM tracks "
        "WHERE deleted_at IS NULL AND duration_ms IS NOT NULL"
    )
    for stable_id, title, artists_json, duration_ms in conn.execute(query):
        title_key, artists_key = norm_loose(title), _artists_key(stable_id, artists_json)
        if title_key and artists_key:
            buckets.setdefault((artists_key, title_key), []).append((int(duration_ms), stable_id))
    for rows in buckets.values():
        rows.sort()
        run: list[tuple[int, str]] = []
        for duration, stable_id in rows:
            if run and duration - run[0][0] > CFG.duration_tolerance_ms:
                yield [sid for _d, sid in run]
                run = []
            run.append((duration, stable_id))
        yield [sid for _d, sid in run]


def _same_file_rows(conn: sqlite3.Connection) -> Iterable[list[str]]:
    """Present live rows grouped by the one on-disk file they resolve to.

    A path that stats is keyed by its file identity (device, inode), so two
    spellings group only when the volume really resolves them to one file: a
    case-sensitive APFS or external volume keeps ``Track.mp3`` and
    ``track.mp3`` apart. A path that does not stat (stale availability, a
    permission error, an unmounted volume) proves nothing about case, so it
    groups only with the IDENTICAL spelling: no case rule is assumed, since a
    wrong group would copy energy to a different recording.
    """
    by_file: dict[object, list[str]] = {}
    query = (
        "SELECT tracks.stable_id, track_availability.checked_path FROM tracks "
        "JOIN track_availability ON track_availability.stable_id = tracks.stable_id "
        "WHERE tracks.deleted_at IS NULL AND track_availability.state = 'present' "
        "AND track_availability.checked_path IS NOT NULL ORDER BY tracks.stable_id"
    )
    for stable_id, checked_path in conn.execute(query):
        # An identical spelling is one file whatever the volume's case rule.
        key = _file_identity(checked_path) or sources.path_spelling(checked_path)
        if key:
            by_file.setdefault(key, []).append(stable_id)
    return (rows for rows in by_file.values() if len(rows) > 1)


def _same_stored_path_rows(conn: sqlite3.Connection) -> Iterable[list[str]]:
    """Live rows grouped by the identical spelling of their stored ``file_path``.

    Spelling only, never a case rule or a stat: the stored path may name a
    file that is gone, and two spellings prove nothing about one file.
    """
    by_path: dict[str, list[str]] = {}
    query = (
        "SELECT stable_id, file_path FROM tracks "
        "WHERE deleted_at IS NULL AND file_path IS NOT NULL ORDER BY stable_id"
    )
    for stable_id, file_path in conn.execute(query):
        key = sources.path_spelling(file_path)
        if key:
            by_path.setdefault(key, []).append(stable_id)
    return (rows for rows in by_path.values() if len(rows) > 1)


def _union_each(union: _UnionFind, row_groups: Iterable[list[str]], basis: str) -> None:
    for rows in row_groups:
        for other in rows[1:]:
            union.union(rows[0], other, basis)


def duplicate_groups(conn: sqlite3.Connection, *, by_metadata: bool) -> DuplicateGroups:
    """Partition live rows into recordings. ``by_metadata`` adds the fallback."""
    union = _UnionFind()
    _union_each(union, _same_file_rows(conn), BASIS_SAME_FILE)
    _union_each(union, _same_stored_path_rows(conn), BASIS_SAME_STORED_PATH)
    for loser, survivor in identity_duplicate_remap(conn).items():
        union.union(loser, survivor, BASIS_IDENTITY)
    if by_metadata:
        _union_each(union, artist_title_duration_groups(conn), BASIS_METADATA)
    groups = DuplicateGroups()
    members: dict[str, list[str]] = {}
    for stable_id in union.parent:
        members.setdefault(union.find(stable_id), []).append(stable_id)
    for root, ids in members.items():
        if len(ids) < 2:
            continue
        groups._members[root] = tuple(sorted(ids))
        bases = union.bases.get(root, set())
        groups._basis[root] = "+".join(sorted(bases))
        for stable_id in ids:
            groups._root[stable_id] = root
    return groups


# ----------------------------------------------------------------- sharing


@dataclass
class ShareResult:
    shared: dict[int, tuple[Match, ...]] = field(default_factory=dict)
    """MIK row id -> the extra rows its energy is written to."""
    conflicts: dict[str, tuple[int, ...]] = field(default_factory=dict)
    """stable_id -> the differing energies offered to it. Never written."""
    basis_by_row: dict[str, str] = field(default_factory=dict)
    ambiguous_not_one_recording: int = 0
    groups_by_basis: dict[str, int] = field(default_factory=dict)

    def rows_by_basis(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for basis in self.basis_by_row.values():
            counts[basis] = counts.get(basis, 0) + 1
        return counts

    def counts(self) -> dict[str, object]:
        return {
            "songs_sharing": len(self.shared),
            "rows_shared": sum(len(targets) for targets in self.shared.values()),
            "rows_shared_by_basis": self.rows_by_basis(),
            "conflicting_rows": len(self.conflicts),
            "ambiguous_not_one_recording": self.ambiguous_not_one_recording,
            "groups_by_basis": self.groups_by_basis,
        }


def tier_candidates(
    index: TrackIndex, *, allow_fuzzy: bool = True
) -> Callable[[MikSong], list[str]]:
    """The candidate rows of a song in its strongest tier that has any: the
    same walk :func:`apps.mik.match.match_songs` made when it called the song
    ambiguous."""
    tiers = TIER_ORDER if allow_fuzzy else TIER_ORDER[:1]

    def candidates(song: MikSong) -> list[str]:
        for tier in tiers:
            found = index.candidates(song, tier)
            if found:
                return found
        return []

    return candidates


def _home_rows(
    song: MikSong,
    report: MatchReport,
    groups: DuplicateGroups,
    candidates_of: Callable[[MikSong], list[str]],
    result: ShareResult,
) -> tuple[str, ...]:
    """The rows this song is known to belong to, all in ONE group, or ``()``."""
    match = report.matches.get(song.row_id)
    if match is not None:
        return (match.stable_id,)
    unmatched = report.unmatched.get(song.row_id)
    if unmatched is not None and unmatched.reason == "lost_collision" and unmatched.claimed_stable_id:
        # A collision loser still analyzed that track: its result is an offer
        # to the track's duplicates, so a disagreement with the winner is seen.
        return (unmatched.claimed_stable_id,)
    if unmatched is None or unmatched.reason != "ambiguous_candidates":
        return ()
    candidates = candidates_of(song)
    if len(candidates) < 2:
        return ()
    if len({groups.group_of(stable_id) for stable_id in candidates}) != 1:
        result.ambiguous_not_one_recording += 1
        return ()
    return tuple(candidates)


_Offer = tuple[int, float, int, MikSong]


def _sharing_offers(
    songs: Iterable[MikSong],
    report: MatchReport,
    groups: DuplicateGroups,
    candidates_of: Callable[[MikSong], list[str]],
    result: ShareResult,
) -> dict[str, list[_Offer]]:
    """Row -> the songs offering it an energy, for rows with no match of their own."""
    own_match = {match.stable_id for match in report.matches.values()}
    offers: dict[str, list[_Offer]] = {}
    for song in songs:
        # Only a COMPLETE analysis is shared: a value with no series would
        # leave every receiving row needing its series, as a direct load does.
        if song.energy is None or song.analysed_at is None or not song.segments:
            continue
        home = _home_rows(song, report, groups, candidates_of, result)
        if not home:
            continue
        from_match = 0 if song.row_id in report.matches else 1
        confidence = song.key_confidence if song.key_confidence is not None else -1.0
        for target in groups.members_of(home[0]):
            if target not in own_match:
                offers.setdefault(target, []).append((from_match, -confidence, song.row_id, song))
    return offers


def plan_sharing(
    songs: Iterable[MikSong],
    report: MatchReport,
    groups: DuplicateGroups,
    *,
    candidates_of: Callable[[MikSong], list[str]],
) -> ShareResult:
    """Decide which extra rows each song's energy reaches. No writes."""
    result = ShareResult(groups_by_basis=groups.by_basis())
    offers = _sharing_offers(songs, report, groups, candidates_of, result)
    shared: dict[int, list[Match]] = {}
    for target in sorted(offers):
        ranked = sorted(offers[target], key=lambda offer: offer[:3])
        energies = tuple(sorted({offer[3].energy for offer in ranked if offer[3].energy is not None}))
        # Agreement is on the whole result, value AND series: one energy with
        # two different series would otherwise attach an arbitrary series.
        results = {(offer[3].energy, offer[3].segments) for offer in ranked}
        if len(results) > 1:
            result.conflicts[target] = energies
            continue
        winner = ranked[0][3]
        shared.setdefault(winner.row_id, []).append(
            Match(mik_row_id=winner.row_id, stable_id=target, tier=SHARE_TIER)
        )
        result.basis_by_row[target] = groups.basis_of(target) or BASIS_IDENTITY
    result.shared = {row_id: tuple(matches) for row_id, matches in shared.items()}
    return result


Rebind = Callable[[sqlite3.Connection], tuple[list[MikSong], MatchReport, Callable[[MikSong], list[str]]]]
"""Re-run a caller's matching against ``conn``: (songs, report, candidates_of)."""


def binding_unchanged(
    rebind: Rebind,
    report: MatchReport,
    shared: dict[int, tuple[Match, ...]],
    *,
    by_metadata: bool,
) -> Callable[[sqlite3.Connection], bool]:
    """A ``LoadPlan.still_valid`` check: re-derive every binding under the lock.

    A relink, deletion, availability refresh or identity update committed after
    planning can move a song's direct match or take a row out of its group
    while target fields stay empty, so the field-version check alone would
    still write the analysis to the stale row.
    """

    def unchanged(locked: sqlite3.Connection) -> bool:
        songs, now_report, candidates_of = rebind(locked)
        if now_report.matches != report.matches or now_report.unmatched != report.unmatched:
            return False
        groups = duplicate_groups(locked, by_metadata=by_metadata)
        return plan_sharing(songs, now_report, groups, candidates_of=candidates_of).shared == shared

    return unchanged


_metadata_chunks = artist_title_duration_groups


__all__ = [
    "BASIS_IDENTITY",
    "BASIS_METADATA",
    "BASIS_SAME_FILE",
    "BASIS_SAME_STORED_PATH",
    "CFG",
    "SHARED_FIELDS",
    "SHARE_TIER",
    "DuplicateGroups",
    "Rebind",
    "ShareResult",
    "artist_title_duration_groups",
    "binding_unchanged",
    "duplicate_groups",
    "plan_sharing",
    "tier_candidates",
]
