"""Cross-harmony-preserving-version key-sequence agreement (annotation-free).

Pairs titles that denote the same musical work (extended vs radio, clean vs
explicit, etc.), excludes remix/VIP-style releases that may transpose or
replace harmony, and scores whether decoded key segments agree after a simple
bar-length alignment. See ``(private path)
20260906-d-key-detection.md`` section 3.2 and ``(private path)/
nav1-key-record.md``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from apps.analysis_key.segments import BarGrid, KeySegment, SegmentBlock
from apps.shared import song_identity

_EXCLUDED = re.compile(
    r"(?i)\b(?:remix|bootleg|rework|refix|re-edit|re\-edit|flip|mashup|mash-up|vip)\b"
)
_SUFFIX_PATTERNS = (
    r"\([^)]*(?:extended mix|radio edit|original mix|club mix|clean|explicit|remaster)[^)]*\)",
    r"\[[^\]]*(?:extended mix|radio edit|original mix|club mix|clean|explicit|remaster)[^\]]*\]",
    r"(?i)(?:[\s\-]+)?(?:extended mix|radio edit|original mix|club mix|clean|explicit|remaster)\s*$",
)


def is_excluded_title(title: str) -> bool:
    """True when the title looks like a remix/VIP style release."""
    return bool(_EXCLUDED.search(title))


def strip_harmony_preserving_suffix(title: str) -> str:
    """Remove common harmony-preserving parenthetical or trailing tag, then norm."""
    stripped = title
    for pattern in _SUFFIX_PATTERNS:
        stripped = re.sub(pattern, "", stripped, flags=re.IGNORECASE)
    return song_identity.norm(stripped.strip())


def group_key(title: str, artists: Sequence[str]) -> str | None:
    """Grouping key for cross-version pairing, or None when excluded or empty."""
    if is_excluded_title(title):
        return None
    normalized = strip_harmony_preserving_suffix(title)
    return song_identity.title_artists_key(normalized, artists)


@dataclass(frozen=True)
class VersionTrack:
    """One analyzed version of a work."""

    stable_id: str
    title: str
    artists: tuple[str, ...]
    segments: SegmentBlock
    grid: BarGrid


@dataclass(frozen=True)
class ConsistencyScore:
    """Agreement counts over pairs the classifier actually formed.

    The rate properties use ``n_pairs`` as the denominator (present version
    pairs only).
    """

    n_pairs: int
    n_agree: int
    n_constant_agree: int

    @property
    def rate(self) -> float:
        return self.n_agree / self.n_pairs if self.n_pairs else 0.0

    @property
    def constant_predictor_rate(self) -> float:
        return self.n_constant_agree / self.n_pairs if self.n_pairs else 0.0


def pair_versions(
    tracks: Sequence[VersionTrack],
) -> tuple[tuple[VersionTrack, VersionTrack], ...]:
    """All unordered pairs within each non-singleton group key."""
    by_group: dict[str, list[VersionTrack]] = defaultdict(list)
    for tr in tracks:
        key = group_key(tr.title, tr.artists)
        if key is None:
            continue
        by_group[key].append(tr)
    pairs: list[tuple[VersionTrack, VersionTrack]] = []
    for group in by_group.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda t: t.stable_id)
        pairs.extend(
            (a, b) for i, a in enumerate(group) for b in group[i + 1 :]
        )
    pairs.sort(key=lambda ab: (ab[0].stable_id, ab[1].stable_id))
    return tuple(pairs)


def _interior_boundary_bars(segments: tuple[KeySegment, ...]) -> tuple[int, ...]:
    return tuple(seg.start_bar for seg in segments[1:])


def _stretched_boundary_indices(
    interior_bars: Sequence[int], n_bars: int, length: int
) -> list[int]:
    if n_bars <= 0 or length <= 0:
        return []
    lim = max(0, length - 1)
    return sorted(
        min(lim, int(bar * length / n_bars)) for bar in interior_bars
    )


def _key_sequences_equal(a: SegmentBlock, b: SegmentBlock) -> bool:
    if a.status != "ok" or b.status != "ok":
        return False
    return tuple(seg.key for seg in a.segments) == tuple(seg.key for seg in b.segments)


def sequences_agree(a: VersionTrack, b: VersionTrack) -> bool:
    """Both key label sequences and interior boundaries agree after stretch."""
    if a.segments.status != "ok" or b.segments.status != "ok":
        return False
    if not _key_sequences_equal(a.segments, b.segments):
        return False
    na, nb = a.grid.n_bars, b.grid.n_bars
    length = max(na, nb)
    if length == 0:
        return True
    bars_a = _interior_boundary_bars(a.segments.segments)
    bars_b = _interior_boundary_bars(b.segments.segments)
    if len(bars_a) != len(bars_b):
        return False
    sa = _stretched_boundary_indices(bars_a, na, length)
    sb = _stretched_boundary_indices(bars_b, nb, length)
    return all(abs(ia - ib) <= 1 for ia, ib in zip(sa, sb, strict=True))


def _constant_block(track: VersionTrack) -> SegmentBlock:
    """Single segment: first key over the whole grid (control predictor)."""
    grid = track.grid
    if grid.n_bars == 0 or not track.segments.segments:
        return SegmentBlock(status="failed", reason="empty", segments=())
    first = track.segments.segments[0].key
    confidence = float(track.segments.segments[0].confidence)
    seg = KeySegment(
        start_bar=0,
        end_bar=grid.n_bars,
        start_s=grid.starts[0],
        end_s=grid.ends[grid.n_bars - 1],
        key=first,
        confidence=min(max(confidence, 0.0), 1.0),
    )
    return SegmentBlock(status="ok", reason=None, segments=(seg,))


def constant_sequences_agree(a: VersionTrack, b: VersionTrack) -> bool:
    """Agreement under the single-key constant predictor."""
    ca = _constant_block(a)
    cb = _constant_block(b)
    if ca.status != "ok" or cb.status != "ok":
        return False
    va = VersionTrack(
        stable_id=a.stable_id,
        title=a.title,
        artists=a.artists,
        segments=ca,
        grid=a.grid,
    )
    vb = VersionTrack(
        stable_id=b.stable_id,
        title=b.title,
        artists=b.artists,
        segments=cb,
        grid=b.grid,
    )
    return sequences_agree(va, vb)


def consistency_rate(tracks: Sequence[VersionTrack]) -> ConsistencyScore:
    """Pair-level real and constant-predictor agreement."""
    pairs = pair_versions(tracks)
    agree = 0
    const_agree = 0
    for a, b in pairs:
        if sequences_agree(a, b):
            agree += 1
        if constant_sequences_agree(a, b):
            const_agree += 1
    return ConsistencyScore(
        n_pairs=len(pairs),
        n_agree=agree,
        n_constant_agree=const_agree,
    )


__all__ = [
    "ConsistencyScore",
    "VersionTrack",
    "consistency_rate",
    "constant_sequences_agree",
    "group_key",
    "is_excluded_title",
    "pair_versions",
    "sequences_agree",
    "strip_harmony_preserving_suffix",
]
