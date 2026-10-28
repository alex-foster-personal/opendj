"""Where every PRESENT library row stands on MIK energy. Read-only.

The denominator is named and fixed: ``present`` (``track_availability.state =
'present'`` on a live row). A row with no resolvable audio cannot be analyzed,
so it is never counted here (house rule: ``(private path)``).

Each present row lands in exactly ONE bucket, and the buckets must sum to the
denominator or :class:`BackfillError` is raised:

======================  =====================================================
bucket                  meaning
======================  =====================================================
``with_mik_energy``     ``track_fields`` holds a MIK energy AND
                        ``track_energy_segments`` holds the MIK series of
                        the same analysis (equal ``modified_at``): done
``needs_series``        a MIK energy without a MIK series: the scalar loaded,
                        the series did not, so the file is planned again
``other_source``        another source holds the energy field; the loader
                        only fills, so MIK would be blocked by precedence
``gain_shared``         no energy yet, and a complete MIK donor in the
                        library (scalar AND series) is one recording with it:
                        ``backfill share`` fills exactly these
                        (:mod:`apps.mik.share_plan` decides both)
``gain_unique``         no energy yet, and a MIK song binds to this row alone
``gain_unloaded``       no energy yet, and an analysis NOT in the library as a
                        complete donor reaches it only by duplicate sharing:
                        a staged MIK row, or a song of the ``store`` passed.
                        Loading that store shares it (``python -m apps.mik
                        load --store <store> --live``); share and promote
                        cannot
``conflicting``         duplicate copies offer DIFFERENT energies: written by
                        none, settled only by analyzing this file itself
``needs_analysis``      MIK holds nothing for it: a fresh analysis is the only
                        way
======================  =====================================================

Two evidence modes. With a MIK ``store`` the songs come from that store (what
a load of it WOULD do). Without one they come from the pending staged rows
(``unmatched_source_analysis``), re-walked for sharing and conflicts. Complete
MIK rows in the library are donors in BOTH modes, through
:mod:`apps.mik.share_plan`.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import SOURCE, mikdb
from . import duplicates as dups
from . import match as matcher
from . import sources_match as sources
from .load_types import SEGMENTS_FIELD, LoadError
from .mikdb import MikSong
from .share_plan import DENOMINATOR, plan_groups, same_analysis_rows

REASON_NO_ENERGY = "no_mik_energy"
REASON_CONFLICT = "conflicting_energies"
REASON_NO_SERIES = "mik_series_missing"


class BackfillError(LoadError):
    """The backfill cannot proceed safely, or its own numbers do not add up."""


@dataclass(frozen=True)
class PresentRow:
    stable_id: str
    checked_path: str | None
    file_path: str | None
    title: str | None
    artist: str | None
    location_paths: tuple[str, ...] = ()
    """Live local ``track_locations`` paths: where the file may really be."""

    def stored_paths(self) -> tuple[str, ...]:
        """Every path this row records for its file, legacy first."""
        return ((self.file_path,) if self.file_path else ()) + self.location_paths


@dataclass
class EnergyPicture:
    present: dict[str, PresentRow] = field(default_factory=dict)
    with_mik_energy: set[str] = field(default_factory=set)
    needs_series: set[str] = field(default_factory=set)
    other_source: dict[str, str] = field(default_factory=dict)
    gain_unique: set[str] = field(default_factory=set)
    gain_shared: set[str] = field(default_factory=set)
    gain_unloaded: set[str] = field(default_factory=set)
    conflicting: dict[str, tuple[int, ...]] = field(default_factory=dict)
    needs_analysis: set[str] = field(default_factory=set)
    staged_unmatched: dict[str, int] = field(default_factory=dict)
    pending_scalar_only: set[str] = field(default_factory=set)
    """Rows a pending store song binds that has an energy but no series.

    Not a bucket (the row still needs a fresh analysis), but share must not
    fill such a row from a donor: the store's own value would be overwritten.
    """
    groups: dups.DuplicateGroups = field(default_factory=dups.DuplicateGroups)
    store: str | None = None
    by_metadata: bool = False

    def counts(self) -> dict[str, Any]:
        other: dict[str, int] = {}
        for source in self.other_source.values():
            other[source] = other.get(source, 0) + 1
        return {
            "denominator": DENOMINATOR,
            DENOMINATOR: len(self.present),
            "with_mik_energy": len(self.with_mik_energy),
            "energy_from_other_source": other,
            "without_mik_energy": {
                "would_gain_by_unique_match": len(self.gain_unique),
                "would_gain_by_duplicate_sharing": len(self.gain_shared),
                "would_gain_by_loading_shared_analysis": len(self.gain_unloaded),
                "conflicting": len(self.conflicting),
                "needs_series": len(self.needs_series),
                "needs_fresh_analysis": len(self.needs_analysis),
            },
            "staged_unmatched": self.staged_unmatched,
            "evidence": {
                "mik_store": self.store,
                "share_across_duplicates": self.by_metadata,
                "duplicate_groups_by_basis": self.groups.by_basis(),
            },
        }


# ----------------------------------------------------------------- reading


def _present_rows(conn: sqlite3.Connection) -> dict[str, PresentRow]:
    query = (
        "SELECT tracks.stable_id, track_availability.checked_path, tracks.file_path, "
        "tracks.title, tracks.artists_json FROM tracks "
        "JOIN track_availability ON track_availability.stable_id = tracks.stable_id "
        "WHERE tracks.deleted_at IS NULL AND track_availability.state = ?"
    )
    rows = conn.execute(query, (DENOMINATOR,)).fetchall()
    located = sources.live_local_locations(conn, [row[0] for row in rows])
    return {
        row[0]: PresentRow(row[0], row[1], row[2], row[3], next(iter(dups.parse_artists(row[0], row[4])), None), tuple(located.get(row[0], [])))
        for row in rows
    }


def _energy_fields(conn: sqlite3.Connection) -> dict[str, tuple[str, Any]]:
    """``{stable_id: (source, value)}`` for every live energy field row."""
    query = (
        "SELECT stable_id, source, value_json FROM track_fields "
        "WHERE field_name = 'energy' AND deleted_at IS NULL"
    )
    return {row[0]: (row[1], json.loads(row[2])) for row in conn.execute(query)}


def _pseudo_song(
    row_id: int, *, energy: Any, path: str | None = None, segments: tuple[mikdb.EnergySegment, ...] = ()
) -> MikSong:
    """A stand-in carrying only what sharing reads: an id, an energy and its series."""
    return MikSong(
        row_id=row_id,
        title=None,
        artist=None,
        album=None,
        path=path,
        path_error=None,
        key_camelot=None,
        key_confidence=None,
        energy=energy if isinstance(energy, int) and not isinstance(energy, bool) else None,
        bpm=None,
        loudness=None,
        clipped_peak_count=None,
        analysed_at="library",
        segments=segments,
    )


@dataclass
class _Evidence:
    songs: list[MikSong] = field(default_factory=list)
    report: matcher.MatchReport = field(default_factory=matcher.MatchReport)
    staged_candidates: dict[int, list[str]] = field(default_factory=dict)
    staged_by_reason: dict[str, int] = field(default_factory=dict)
    scalar_only_bound: set[str] = field(default_factory=set)


def _staged_series(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[mikdb.EnergySegment, ...]]:
    """Each pending staged MIK row's energy series, keyed by (row id, analysis time).

    Keyed with ``modified_at`` so a scalar is paired only with the series of
    the SAME analysis: a refresh that restaged a scalar-only analysis leaves
    the older series behind, and that pair is not a complete analysis.
    """
    query = (
        "SELECT source_row_id, modified_at, value_json FROM unmatched_source_analysis "
        "WHERE source = ? AND field_name = ? AND promoted_stable_id IS NULL"
    )
    series: dict[tuple[str, str], tuple[mikdb.EnergySegment, ...]] = {}
    for raw_id, modified_at, value_json in conn.execute(query, (SOURCE, SEGMENTS_FIELD)):
        items = sorted(json.loads(value_json), key=lambda item: int(item["start_ms"]))
        series[(str(raw_id), modified_at)] = tuple(
            mikdb.EnergySegment(
                seq, int(item["start_ms"]), int(item["length_ms"]), int(item["energy"]), bool(item["start_clamped"])
            )
            for seq, item in enumerate(items)
        )
    return series


def _staged_evidence(
    conn: sqlite3.Connection, index: matcher.TrackIndex, evidence: _Evidence
) -> None:
    """Pending staged energy rows, re-walked against ``tracks`` as it is now.

    The staged title was stripped of MIK's energy prefix at import, so the
    candidates are looked up with it as-is (stripping twice changes
    ``"12 - Foo"``), which is why this does not go through ``MikSong``.
    """
    query = (
        "SELECT source_row_id, value_json, unmatched_reason, title, artist, source_path, modified_at "
        "FROM unmatched_source_analysis WHERE source = ? AND field_name = 'energy' "
        "AND promoted_stable_id IS NULL"
    )
    series = _staged_series(conn)
    rows = sources.row_index(conn, platform=sources.HOST_PLATFORM)
    for raw_id, value_json, reason, title, artist, source_path, modified_at in conn.execute(query, (SOURCE,)):
        evidence.staged_by_reason[reason] = evidence.staged_by_reason.get(reason, 0) + 1
        if not str(raw_id).lstrip("-").isdigit():
            continue
        row_id = int(raw_id)
        found, tier = _staged_row_candidates(index, rows, reason, source_path, artist, title)
        if not found:
            continue
        segments = series.get((str(raw_id), modified_at), ())
        if not segments:
            # A staged value with no series: every row it may land on, and
            # its duplicates, waits for a fresh analysis rather than a donor.
            evidence.scalar_only_bound.update(found)
            continue
        evidence.staged_candidates[row_id] = found
        evidence.songs.append(_pseudo_song(row_id, energy=json.loads(value_json), path=source_path, segments=segments))
        # A collision loser's claim is not stored, so it is re-found here: one
        # candidate is the row it lost, and its result still counts as an offer.
        claimed = found[0] if reason == "lost_collision" and len(found) == 1 else None
        if claimed is None and len(found) == 1:
            # Now uniquely matchable: it settles its row by loading, never a donor.
            evidence.report.matches[row_id] = matcher.Match(row_id, found[0], tier)
            continue
        evidence.report.unmatched[row_id] = matcher.Unmatched(
            row_id, "lost_collision" if claimed else "ambiguous_candidates", "staged", claimed_stable_id=claimed
        )


_FUZZY_STAGED_REASONS = ("ambiguous_candidates", "lost_collision")
"""Staged reasons the three-tier matcher produced, so it may replay all its tiers."""
_FUZZY_STAGED_TIERS: tuple[matcher.Tier, ...] = matcher.TIER_ORDER


@dataclass(frozen=True)
class _BatchOrigin:
    """A staged copy's batch provenance: the original its sources map names, and the batch's library root."""

    original: str
    library_root: str | None


def _batch_original(source_path: str | None) -> tuple[bool, _BatchOrigin | None]:
    """(whether ``source_path`` is a copy in a stage batch, how collect bound it).

    The origin is None when the map or ``.batch.json`` is gone or unreadable,
    or the map does not name exactly one line for this copy: then the row's
    binding cannot be replayed at all.
    """
    copy = Path(source_path or "")
    if not copy.parent.name.startswith(sources.BATCH_PREFIX):
        return False, None
    try:
        lines = sources.read_source_lines(copy.parent)
        root = sources.read_batch_library_root(copy.parent)
    except LoadError:
        return True, None
    name = sources.path_key(copy.name, platform=sources.HOST_PLATFORM)
    named = [line for line in lines if sources.path_key(Path(line).name, platform=sources.HOST_PLATFORM) == name]
    return True, (_BatchOrigin(named[0], root) if len(named) == 1 else None)


def _staged_row_candidates(
    index: matcher.TrackIndex,
    rows: sources.RowIndex,
    reason: str,
    source_path: str | None,
    artist: str | None,
    title: str | None,
) -> tuple[list[str], matcher.Tier]:
    """A pending staged row's candidates in ``tracks`` as it is now, replaying how it was bound.

    A collect row (its source_path is a batch copy) replays its sources map
    under collect's own rule, the original's whole path or its path below the
    batch's library root: a basename or artist/title hit on the copy would
    bypass the source-map rule. A former no_candidate row
    matches by exact path only (a relink). Only the matcher's own ambiguous
    and lost_collision rows replay every tier.
    """
    is_batch, origin = _batch_original(source_path)
    if is_batch:
        found = sources.rows_for_source_line(origin.original, origin.library_root, rows) if origin else set()
        return sorted(found), "exact_path"
    tiers = _FUZZY_STAGED_TIERS if reason in _FUZZY_STAGED_REASONS else ("exact_path",)
    return _current_candidates(index, source_path, artist, title, tiers)


def _current_candidates(
    index: matcher.TrackIndex,
    path: str | None,
    artist: str | None,
    title: str | None,
    tiers: tuple[matcher.Tier, ...],
) -> tuple[list[str], matcher.Tier]:
    """The first of ``tiers`` with candidates for a staged row as ``tracks`` is now, and that tier."""
    for tier in tiers:
        found = index.candidates_for(tier, path=path, artist=artist, title=title)
        if found:
            return found, tier
    return [], tiers[-1]


def _candidates_of(
    index: matcher.TrackIndex, evidence: _Evidence
) -> Callable[[MikSong], list[str]]:
    by_tier = dups.tier_candidates(index)

    def candidates(song: MikSong) -> list[str]:
        if song.row_id in evidence.staged_candidates:
            return evidence.staged_candidates[song.row_id]
        return by_tier(song)

    return candidates


def read_store(store: Path) -> list[MikSong]:
    conn = mikdb.open_ro(store)
    try:
        return mikdb.read_songs(conn)[0]
    finally:
        conn.close()


# ----------------------------------------------------------------- picture


def _add_store_evidence(songs: list[MikSong], index: matcher.TrackIndex, evidence: _Evidence) -> None:
    """Merge a not-yet-loaded store's songs into ``evidence``.

    Store row ids are renumbered below zero so they can never collide with
    staged rows, whose ids are MIK's own positive row ids. Only a song with an
    energy SERIES is a complete analysis: a scalar-only song leaves its row
    needing the series, so it counts as scalar-only evidence, never settled.
    """
    songs = [dataclasses.replace(song, row_id=-(song.row_id + 1)) for song in songs]
    complete = [song for song in songs if song.segments]
    report = matcher.match_songs(complete, index)
    evidence.songs += complete
    evidence.report.matches.update(report.matches)
    evidence.report.unmatched.update(report.unmatched)
    everything = matcher.match_songs(songs, index)
    candidates = dups.tier_candidates(index)
    evidence.scalar_only_bound |= {
        stable_id
        for song in songs
        if not song.segments and song.energy is not None
        for stable_id in _rows_a_song_may_land_on(song, everything, candidates)
    }


def _gather_evidence(
    conn: sqlite3.Connection, index: matcher.TrackIndex, store: Path | None
) -> tuple[_Evidence, set[str]]:
    """The songs to reason from, and the rows a store song binds to alone."""
    evidence = _Evidence()
    # Staged evidence always counts: a store names more analyses, it never
    # makes an earlier staged contradiction disappear.
    _staged_evidence(conn, index, evidence)
    if store is not None:
        _add_store_evidence(read_store(store), index, evidence)
    bound: set[str] = set()
    for song in evidence.songs:
        match = evidence.report.matches.get(song.row_id)
        if match is not None and song.energy is not None and song.analysed_at is not None:
            bound.add(match.stable_id)
    return evidence, bound


def _rows_a_song_may_land_on(
    song: MikSong, report: matcher.MatchReport, candidates: Callable[[MikSong], list[str]]
) -> set[str]:
    """Every row a song could settle: its match, its ambiguous candidates, or the row it lost a claim on."""
    match = report.matches.get(song.row_id)
    if match is not None:
        return {match.stable_id}
    unmatched = report.unmatched.get(song.row_id)
    if unmatched is not None and unmatched.reason == "lost_collision" and unmatched.claimed_stable_id:
        return {unmatched.claimed_stable_id}
    if unmatched is not None and unmatched.reason == "ambiguous_candidates":
        return set(candidates(song))
    return set()


@dataclass
class _Reach:
    """What can reach a present row with no energy, by the action that would write it."""

    share_fills: set[str]
    """``backfill share`` writes these (:func:`apps.mik.share_plan.plan_groups`)."""
    share_conflicts: dict[str, tuple[int, ...]]
    bound: set[str]
    """A song of the evidence binds to the row alone."""
    unloaded: dups.ShareResult
    """Sharing from evidence that is not a complete donor in the library."""

    def shared_unloaded(self) -> set[str]:
        return {match.stable_id for targets in self.unloaded.shared.values() for match in targets}


def _reach(
    conn: sqlite3.Connection,
    picture: EnergyPicture,
    evidence: _Evidence,
    bound: set[str],
    index: matcher.TrackIndex,
) -> _Reach:
    donor_answer: dict[str, tuple[int, tuple[mikdb.EnergySegment, ...]]] = {}
    conflicts: dict[str, tuple[int, ...]] = {}
    for share in plan_groups(conn, picture.groups):
        if share.conflicting:
            energies = tuple(sorted({donor.energy for donor in share.donors}))
            conflicts.update(dict.fromkeys(share.fills, energies))
        else:
            donor_answer.update(dict.fromkeys(share.fills, share.donors[0].answer()))
    unloaded = dups.plan_sharing(
        evidence.songs, evidence.report, picture.groups, candidates_of=_candidates_of(index, evidence)
    )
    # A library donor and a pending analysis offering one row DIFFERENT results
    # is a conflict: `backfill share` would otherwise write the donor's value
    # over an analysis that disagrees with it.
    # Compared on the whole answer, value AND series, as plan_sharing does.
    answer_of = {song.row_id: (song.energy, song.segments) for song in evidence.songs}
    for row_id, targets in unloaded.shared.items():
        for match in targets:
            donor = donor_answer.get(match.stable_id)
            pending = answer_of.get(row_id)
            if donor is not None and pending is not None and pending != donor:
                del donor_answer[match.stable_id]
                energies = {donor[0]} | ({pending[0]} if pending[0] is not None else set())
                conflicts[match.stable_id] = tuple(sorted(energies))
    return _Reach(set(donor_answer), conflicts, bound, unloaded)


def _bucket(picture: EnergyPicture, stable_id: str, reach: _Reach, shared_unloaded: set[str]) -> None:
    """Put one present row with NO energy field in exactly one bucket, cheapest action first."""
    # Pending evidence (a direct store match, or staged analyses that
    # disagree) is decided BEFORE a library donor: telling the operator to
    # share a donor's value over a pending result would write one of two
    # differing analyses, which the duplicate contract forbids.
    if stable_id in reach.bound:
        picture.gain_unique.add(stable_id)
    elif stable_id in reach.unloaded.conflicts:
        picture.conflicting[stable_id] = reach.unloaded.conflicts[stable_id]
    elif stable_id in picture.pending_scalar_only:
        # The pending store has this row's value but no series: share must
        # not overwrite it, and only a fresh analysis brings the series.
        picture.needs_analysis.add(stable_id)
    elif stable_id in reach.share_fills:
        picture.gain_shared.add(stable_id)
    elif stable_id in reach.share_conflicts:
        picture.conflicting[stable_id] = reach.share_conflicts[stable_id]
    elif stable_id in shared_unloaded:
        picture.gain_unloaded.add(stable_id)
    else:
        picture.needs_analysis.add(stable_id)


def energy_picture(
    conn: sqlite3.Connection, *, store: Path | None = None, by_metadata: bool = False
) -> EnergyPicture:
    """Bucket every present row. See the module docstring for the buckets."""
    picture = EnergyPicture(store=str(store) if store else None, by_metadata=by_metadata)
    picture.present = _present_rows(conn)
    energy = _energy_fields(conn)
    index = matcher.TrackIndex.from_conn(conn)
    evidence, store_bound = _gather_evidence(conn, index, store)
    picture.staged_unmatched = dict(sorted(evidence.staged_by_reason.items()))
    with_series = same_analysis_rows(conn)
    picture.groups = dups.duplicate_groups(conn, by_metadata=by_metadata)
    # A pending value without a series speaks for its row's whole recording:
    # sharing to any duplicate would write over a value it contradicts.
    picture.pending_scalar_only = {
        member for stable_id in evidence.scalar_only_bound for member in picture.groups.members_of(stable_id)
    }
    reach = _reach(conn, picture, evidence, store_bound, index)
    shared_unloaded = reach.shared_unloaded()
    for stable_id in picture.present:
        source = energy.get(stable_id, (None, None))[0]
        if source == SOURCE:
            (picture.with_mik_energy if stable_id in with_series else picture.needs_series).add(stable_id)
        elif source is not None:
            picture.other_source[stable_id] = source
        else:
            _bucket(picture, stable_id, reach, shared_unloaded)
    _check_sum(picture)
    return picture


def _check_sum(picture: EnergyPicture) -> None:
    buckets = (
        len(picture.with_mik_energy)
        + len(picture.needs_series)
        + len(picture.other_source)
        + len(picture.gain_unique)
        + len(picture.gain_shared)
        + len(picture.gain_unloaded)
        + len(picture.conflicting)
        + len(picture.needs_analysis)
    )
    if buckets != len(picture.present):
        raise BackfillError(
            f"energy buckets sum to {buckets} but {DENOMINATOR} is {len(picture.present)}"
        )


# -------------------------------------------------------------------- plan


@dataclass(frozen=True)
class PlanEntry:
    """One file to analyze, and every present row it settles."""

    source: str | None
    stable_ids: tuple[str, ...]
    reason: str
    title: str | None
    artist: str | None
    stored_paths: tuple[str, ...] = ()
    """``tracks.file_path`` of every row in ``stable_ids``: what ``collect``
    will have to bind ``source`` to."""

    def payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "stable_ids": list(self.stable_ids),
            "reason": self.reason,
            "title": self.title,
            "artist": self.artist,
        }


def _reason(picture: EnergyPicture, members: list[str]) -> str:
    """Why a recording is planned: every row conflicting, every row missing only its series, or neither."""
    if all(sid in picture.conflicting for sid in members):
        return REASON_CONFLICT
    if all(sid in picture.needs_series for sid in members):
        return REASON_NO_SERIES
    return REASON_NO_ENERGY


def plan_entries(picture: EnergyPicture) -> list[PlanEntry]:
    """Files that need a fresh analysis, one per recording, in path order.

    A conflicting row is planned too: analyzing the file itself is the only
    thing that settles which energy is right.
    """
    wanted = set(picture.needs_analysis) | set(picture.conflicting) | picture.needs_series
    by_group: dict[str, list[str]] = {}
    for stable_id in wanted:
        by_group.setdefault(picture.groups.group_of(stable_id), []).append(stable_id)
    entries: list[PlanEntry] = []
    for members in by_group.values():
        members.sort()
        lead = next(
            (picture.present[sid] for sid in members if picture.present[sid].checked_path),
            picture.present[members[0]],
        )
        entries.append(
            PlanEntry(
                source=lead.checked_path,
                stable_ids=tuple(members),
                reason=_reason(picture, members),
                title=lead.title,
                artist=lead.artist,
                stored_paths=tuple(path for sid in members for path in picture.present[sid].stored_paths()),
            )
        )
    entries.sort(key=lambda entry: (entry.source or "", entry.stable_ids))
    return entries


__all__ = [
    "DENOMINATOR",
    "REASON_CONFLICT",
    "REASON_NO_ENERGY",
    "REASON_NO_SERIES",
    "BackfillError",
    "EnergyPicture",
    "PlanEntry",
    "PresentRow",
    "energy_picture",
    "plan_entries",
    "read_store",
]
