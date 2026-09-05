"""Three-tier matcher: MIK song -> ``tracks.stable_id``.

Why three tiers and not path equality: OBSERVED Tue 28 Jul 2026, NOT ONE of
MIK's 7,006 stored paths resolves on disk. They point at the dead
``/Users/dev`` home and two deleted ``/Users/dev/Documents`` folders.
Path equality against ``tracks.file_path`` still matches a lot, because
``tracks`` records the same dead paths, but it cannot be the only tier.

Tiers, strongest first:

======================  ==========================================
tier                    key
======================  ==========================================
``exact_path``          NFC-normalised absolute path
``basename``            NFC + casefolded filename (NOT unique)
``artist_title``        NFC + casefolded ``artist`` + ``title``,
                        with MIK's ``"7 - "`` energy prefix stripped
======================  ==========================================

The tier that matched is recorded on every match and carried into the DB, so
the maintainer can audit a bad fuzzy match later. Given no MIK path resolves on disk,
tier provenance is not a nicety, it is the only audit handle.

Two collision directions, both resolved WITHOUT inventing data:

* One MIK song matching SEVERAL tracks in its winning tier is
  ``ambiguous_candidates``. It gets no stable_id.
* Several MIK songs matching the SAME track (the dominant direction: 6,555
  matched MIK rows collapse to 4,655 distinct tracks) is resolved
  deterministically by (best tier, highest key confidence, lowest MIK row id).
  The losers are ``lost_collision`` and get no stable_id.

All three no-stable_id outcomes are retained in ``unmatched_source_analysis``
with the reason recorded. Nothing MIK knows is thrown away.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from apps.shared.state.locations import list_location_paths

from .mikdb import MikSong

Tier = Literal["exact_path", "basename", "artist_title"]

TIER_ORDER: tuple[Tier, ...] = ("exact_path", "basename", "artist_title")
TIER_RANK: dict[Tier, int] = {tier: rank for rank, tier in enumerate(TIER_ORDER)}
FUZZY_TIERS: frozenset[str] = frozenset({"basename", "artist_title"})

UnmatchedReason = Literal["no_candidate", "ambiguous_candidates", "lost_collision"]


@dataclass(frozen=True)
class Match:
    """A MIK song bound to one track, plus how confidently."""

    mik_row_id: int
    stable_id: str
    tier: Tier

    @property
    def is_fuzzy(self) -> bool:
        return self.tier in FUZZY_TIERS


@dataclass(frozen=True)
class Unmatched:
    """A MIK song with no stable_id, and why."""

    mik_row_id: int
    reason: UnmatchedReason
    detail: str


@dataclass
class MatchReport:
    matches: dict[int, Match] = field(default_factory=dict)
    unmatched: dict[int, Unmatched] = field(default_factory=dict)

    def by_tier(self) -> dict[str, int]:
        counts: dict[str, int] = {tier: 0 for tier in TIER_ORDER}
        for match in self.matches.values():
            counts[match.tier] += 1
        return counts

    def by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.unmatched.values():
            counts[row.reason] = counts.get(row.reason, 0) + 1
        return counts

    def tier_candidate_counts(self) -> dict[str, int]:
        """How many MIK rows produced at least one candidate PER tier.

        Reported separately from :meth:`by_tier` because a row can produce
        candidates in several tiers while only the strongest one is used.
        """
        return dict(self._tier_candidates)

    _tier_candidates: dict[str, int] = field(default_factory=dict)


# ----------------------------------------------------------- normalising


def norm_path(path: str | None) -> str | None:
    """NFC-normalise an absolute path. Case is PRESERVED (paths are data)."""
    if not path:
        return None
    return unicodedata.normalize("NFC", path)


def norm_loose(text: str | None) -> str | None:
    """NFC + casefold + collapse whitespace, for fuzzy keys only."""
    if text is None:
        return None
    folded = unicodedata.normalize("NFC", text).casefold()
    collapsed = " ".join(folded.split())
    return collapsed or None


def basename_key(path: str | None) -> str | None:
    normalised = norm_path(path)
    if normalised is None:
        return None
    return norm_loose(normalised.rsplit("/", 1)[-1])


def artist_title_key(artist: str | None, title: str | None) -> str | None:
    artist_key = norm_loose(artist)
    title_key = norm_loose(title)
    if not artist_key or not title_key:
        return None
    return f"{artist_key}\x1f{title_key}"


def _first_artist(artists_json: str | None) -> str | None:
    if not artists_json:
        return None
    try:
        artists = json.loads(artists_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(artists, list) or not artists:
        return None
    first = artists[0]
    return first if isinstance(first, str) and first.strip() else None


# ------------------------------------------------------------- indices


@dataclass
class TrackIndex:
    """Lookup tables over ``tracks``, built once per run."""

    by_path: dict[str, list[str]] = field(default_factory=dict)
    by_basename: dict[str, list[str]] = field(default_factory=dict)
    by_artist_title: dict[str, list[str]] = field(default_factory=dict)
    track_count: int = 0

    @classmethod
    def from_conn(cls, conn: sqlite3.Connection) -> TrackIndex:
        index = cls()
        tracks = conn.execute(
            "SELECT stable_id, title, artists_json, file_path FROM tracks "
            "WHERE deleted_at IS NULL"
        ).fetchall()
        alt_paths = list_location_paths(conn, [row[0] for row in tracks])
        for stable_id, title, artists_json, file_path in tracks:
            index.track_count += 1
            path_key = norm_path(file_path)
            if path_key:
                index.by_path.setdefault(path_key, []).append(stable_id)
            # A recovered copy can live only in track_locations (this
            # machine's relocated/alternate path), never touching the legacy
            # tracks.file_path -- see apps.mik.availability.probe, which
            # already treats such an alternate as playable. Without it here,
            # a staged row whose source_path exactly equals the alternate
            # stays stranded in staging past its documented relocate trigger.
            for alt in alt_paths.get(stable_id, []):
                alt_key = norm_path(alt)
                if alt_key and alt_key != path_key:
                    index.by_path.setdefault(alt_key, []).append(stable_id)
            base_key = basename_key(file_path)
            if base_key:
                index.by_basename.setdefault(base_key, []).append(stable_id)
            at_key = artist_title_key(_first_artist(artists_json), title)
            if at_key:
                index.by_artist_title.setdefault(at_key, []).append(stable_id)
        return index

    def candidates_for(
        self,
        tier: Tier,
        *,
        path: str | None,
        artist: str | None,
        title: str | None,
    ) -> list[str]:
        """Candidate stable_ids in ``tier``. ``title`` must ALREADY be stripped.

        The explicit form exists so :mod:`apps.mik.promote` can re-match a
        staged row whose title was stripped at import time, without stripping
        it a second time (``"12 - Foo"`` stripped twice is a different string).
        """
        if tier == "exact_path":
            key = norm_path(path)
            return sorted(set(self.by_path.get(key, []))) if key else []
        if tier == "basename":
            key = basename_key(path)
            return sorted(set(self.by_basename.get(key, []))) if key else []
        if tier == "artist_title":
            key = artist_title_key(artist, title)
            return sorted(set(self.by_artist_title.get(key, []))) if key else []
        raise ValueError(f"unknown tier {tier!r}")

    def candidates(self, song: MikSong, tier: Tier) -> list[str]:
        return self.candidates_for(
            tier,
            path=song.path,
            artist=song.artist,
            title=song.stripped_title,
        )


# ------------------------------------------------------------- matching


def _confidence_for_sort(song: MikSong) -> float:
    return song.key_confidence if song.key_confidence is not None else -1.0


def _best_tier_per_song(
    songs: list[MikSong],
    index: TrackIndex,
    tiers: tuple[Tier, ...],
    tier_candidates: dict[str, int],
    report: MatchReport,
    allow_fuzzy: bool,
) -> dict[int, tuple[Match, MikSong]]:
    """Pass 1 of ``match_songs``, split out to keep it under the complexity
    ceiling: the best tier per song, or why none qualified."""
    provisional: dict[int, tuple[Match, MikSong]] = {}
    for song in songs:
        chosen: Match | None = None
        ambiguous_detail: str | None = None
        for tier in tiers:
            candidates = index.candidates(song, tier)
            if not candidates:
                continue
            tier_candidates[tier] += 1
            if chosen is not None or ambiguous_detail is not None:
                continue
            if len(candidates) > 1:
                ambiguous_detail = (
                    f"tier {tier} matched {len(candidates)} tracks: "
                    f"{','.join(candidates[:4])}"
                )
                continue
            chosen = Match(
                mik_row_id=song.row_id, stable_id=candidates[0], tier=tier
            )
        if chosen is not None:
            provisional[song.row_id] = (chosen, song)
        elif ambiguous_detail is not None:
            report.unmatched[song.row_id] = Unmatched(
                mik_row_id=song.row_id,
                reason="ambiguous_candidates",
                detail=ambiguous_detail,
            )
        else:
            report.unmatched[song.row_id] = Unmatched(
                mik_row_id=song.row_id,
                reason="no_candidate",
                detail=(
                    "no tier produced a candidate"
                    if allow_fuzzy
                    else "no exact-path candidate (--allow-fuzzy is off)"
                ),
            )
    return provisional


def _resolve_claim_collisions(
    provisional: dict[int, tuple[Match, MikSong]], report: MatchReport
) -> None:
    """Pass 2 of ``match_songs``, split out to keep it under the complexity
    ceiling: several MIK songs claiming one track, deterministic winner."""
    claimants: dict[str, list[tuple[Match, MikSong]]] = {}
    for match, song in provisional.values():
        claimants.setdefault(match.stable_id, []).append((match, song))
    for stable_id, entries in claimants.items():
        entries.sort(
            key=lambda pair: (
                TIER_RANK[pair[0].tier],
                -_confidence_for_sort(pair[1]),
                pair[0].mik_row_id,
            )
        )
        winner = entries[0][0]
        report.matches[winner.mik_row_id] = winner
        for loser_match, _loser_song in entries[1:]:
            report.unmatched[loser_match.mik_row_id] = Unmatched(
                mik_row_id=loser_match.mik_row_id,
                reason="lost_collision",
                detail=(
                    f"track {stable_id} already claimed via tier "
                    f"{winner.tier} by MIK row {winner.mik_row_id}"
                ),
            )


def match_songs(
    songs: Iterable[MikSong],
    index: TrackIndex,
    *,
    allow_fuzzy: bool = True,
) -> MatchReport:
    """Bind each song to at most one ``stable_id``. Deterministic.

    ``allow_fuzzy=False`` restricts matching to ``exact_path``; everything else
    becomes ``no_candidate``. That is the honest floor (3,530 MIK rows) for a
    run that must not lean on filename or artist/title heuristics.
    """
    songs = list(songs)
    report = MatchReport()
    tiers: tuple[Tier, ...] = TIER_ORDER if allow_fuzzy else ("exact_path",)
    tier_candidates: dict[str, int] = {tier: 0 for tier in TIER_ORDER}

    provisional = _best_tier_per_song(
        songs, index, tiers, tier_candidates, report, allow_fuzzy
    )
    _resolve_claim_collisions(provisional, report)

    report._tier_candidates = tier_candidates
    return report


__all__ = [
    "FUZZY_TIERS",
    "TIER_ORDER",
    "TIER_RANK",
    "Match",
    "MatchReport",
    "Tier",
    "TrackIndex",
    "Unmatched",
    "UnmatchedReason",
    "artist_title_key",
    "basename_key",
    "match_songs",
    "norm_loose",
    "norm_path",
]
