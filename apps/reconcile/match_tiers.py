"""Ordered tier scorers for :mod:`apps.reconcile.match` candidate lookup.

Each ``_tier_*`` function returns scored :class:`~apps.reconcile.match.Candidate`
rows for one evidence tier. :data:`TIER_FNS` is the dispatch table
:func:`~apps.reconcile.match.find_candidates` walks in first-match-wins order.

Imported from :mod:`apps.reconcile.match` only after that module defines
:class:`~apps.reconcile.match.TrackRow` and :class:`~apps.reconcile.match.Candidate`.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable

from apps.reconcile import match as _match
from apps.reconcile.index_disk import DiskAudio, DiskIndex, basename_key

Candidate = _match.Candidate
Tier = _match.Tier
TrackRow = _match.TrackRow


def _ratio(a: str, b: str, floor: float) -> float:
    """SequenceMatcher ratio, short-circuited by the cheap upper bounds."""
    if not a or not b:
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b)
    if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
        return 0.0
    return matcher.ratio()


def _basename_matches(row: TrackRow, index: DiskIndex) -> list[DiskAudio]:
    if not row.basename:
        return []
    return index.by_basename.get(basename_key(row.basename), [])


def _tier_basename_exact_unique(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    hits = _basename_matches(row, index)
    if len(hits) != 1:
        return []
    return [
        Candidate(
            path=hits[0].path,
            tier="basename-exact-unique",
            confidence=_match.CONF_BASENAME_UNIQUE,
            reason=f"only file named '{row.basename}' under the indexed roots",
        )
    ]


def _tier_basename_duration(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    hits = _basename_matches(row, index)
    if len(hits) < 2 or row.duration_s is None:
        return []
    out: list[Candidate] = []
    for hit in hits:
        if hit.duration_s is None:
            continue
        delta = abs(hit.duration_s - row.duration_s)
        if delta <= _match.DURATION_TOLERANCE_S:
            out.append(
                Candidate(
                    path=hit.path,
                    tier="basename-duration",
                    confidence=_match.CONF_BASENAME_DURATION,
                    reason=(
                        f"basename '{row.basename}' shared by {len(hits)} files; "
                        f"duration {hit.duration_s:.1f}s is within "
                        f"{delta:.1f}s of the recorded {row.duration_s:.1f}s"
                    ),
                )
            )
    return out


def _tier_basename_size(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    hits = _basename_matches(row, index)
    if len(hits) < 2 or not row.expected_size_bytes:
        return []
    return [
        Candidate(
            path=hit.path,
            tier="basename-size",
            confidence=_match.CONF_BASENAME_SIZE,
            reason=(
                f"basename '{row.basename}' shared by {len(hits)} files; "
                f"size {hit.size_bytes} bytes matches the recorded size exactly"
            ),
        )
        for hit in hits
        if hit.size_bytes == row.expected_size_bytes
    ]


def _tier_isrc(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    if not row.isrc:
        return []
    return [
        Candidate(
            path=hit.path,
            tier="isrc",
            confidence=_match.CONF_ISRC,
            reason=f"ISRC tag {row.isrc} matches the recorded ISRC",
        )
        for hit in index.by_isrc.get(row.isrc, [])
    ]


def _fuzzy_pool(row: TrackRow, index: DiskIndex) -> list[DiskAudio]:
    """Blocked candidate pool for the fuzzy tiers, via the token index."""
    text = f"{row.title_key} {row.artist_key} {row.stem_key}"
    tokens = {t for t in text.split() if len(t) >= 4}
    pool: dict[str, DiskAudio] = {}
    for token in tokens:
        postings = index.by_token.get(token)
        if not postings or len(postings) > _match.MAX_TOKEN_POSTINGS:
            continue
        for entry in postings:
            pool[entry.path] = entry
            if len(pool) >= _match.MAX_FUZZY_CANDIDATES:
                return list(pool.values())
    return list(pool.values())


def _tier_title_artist_fuzzy(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    if not row.title_key or not row.artist_key:
        return []
    out: list[Candidate] = []
    for entry in _fuzzy_pool(row, index):
        if not entry.title_key or not entry.artist_key:
            continue
        t_sim = _ratio(row.title_key, entry.title_key, _match.TITLE_SIMILARITY_MIN)
        if t_sim < _match.TITLE_SIMILARITY_MIN:
            continue
        a_sim = _ratio(row.artist_key, entry.artist_key, _match.ARTIST_SIMILARITY_MIN)
        if a_sim < _match.ARTIST_SIMILARITY_MIN:
            continue
        combined = 0.6 * t_sim + 0.4 * a_sim
        out.append(
            Candidate(
                path=entry.path,
                tier="title-artist-fuzzy",
                confidence=round(
                    _match.CONF_TITLE_ARTIST_BASE + _match.CONF_TITLE_ARTIST_SPAN * combined,
                    3,
                ),
                reason=(
                    f"tagged title similarity {t_sim:.2f} and artist similarity "
                    f"{a_sim:.2f} against '{entry.title}' by '{entry.artist}'"
                ),
            )
        )
    return out


def _tier_duration_title_fuzzy(row: TrackRow, index: DiskIndex) -> list[Candidate]:
    if row.duration_s is None or not row.title_key:
        return []
    centre = int(row.duration_s)
    pool: dict[str, DiskAudio] = {}
    tol = _match.DURATION_TOLERANCE_S
    for bucket in range(centre - int(tol), centre + int(tol) + 1):
        for entry in index.by_duration_s.get(bucket, []):
            pool[entry.path] = entry
    out: list[Candidate] = []
    for entry in pool.values():
        if entry.duration_s is None:
            continue
        delta = abs(entry.duration_s - row.duration_s)
        if delta > tol:
            continue
        # Tag title when present, else the filename stem -- the reason string
        # says which, so a reviewer knows how strong the text evidence is.
        via, text = ("tagged title", entry.title_key)
        if not text:
            via, text = ("filename", entry.stem_key)
        sim = _ratio(row.title_key, text, _match.TITLE_ONLY_SIMILARITY_MIN)
        if sim < _match.TITLE_ONLY_SIMILARITY_MIN:
            continue
        out.append(
            Candidate(
                path=entry.path,
                tier="duration-title-fuzzy",
                confidence=round(
                    _match.CONF_DURATION_TITLE_BASE + _match.CONF_DURATION_TITLE_SPAN * sim,
                    3,
                ),
                reason=(
                    f"duration within {delta:.1f}s of the recorded {row.duration_s:.1f}s and {via} similarity {sim:.2f}"
                ),
            )
        )
    return out


TIER_FNS: dict[Tier, Callable[[TrackRow, DiskIndex], list[Candidate]]] = {
    "basename-exact-unique": _tier_basename_exact_unique,
    "basename-duration": _tier_basename_duration,
    "basename-size": _tier_basename_size,
    "isrc": _tier_isrc,
    "title-artist-fuzzy": _tier_title_artist_fuzzy,
    "duration-title-fuzzy": _tier_duration_title_fuzzy,
}
