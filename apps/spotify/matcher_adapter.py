"""Match Spotify tracks against local state-layer tracks.

Self-contained matcher (no hard dep on apps/sync/matcher.py which is
still in flight). Signal weights:
    ISRC exact (0.35)  -- one-signal auto-match; globally unique.
    Title (0.20)       -- normalised (casefold + strip version suffixes).
    Artist (0.15)      -- set overlap on normalised names.
    Duration (0.20)    -- +/- 1500 ms.

Accept policy (CONTEXT D2):
    ISRC alone -> matched.
    Otherwise conf >= 0.70 AND >= 3 signals -> matched.
    Conf >= 0.50 AND >= 2 signals -> review.
    Else -> unmatched (acquisition queue).

TODO(phase-2): swap for apps/sync/matcher.py 6-signal stack when ready.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Literal

from .client import SpotifyTrack

__all__ = [
    "LocalTrack",
    "MatchResult",
    "MatchedPair",
    "match_spotify_tracks",
    "normalise_title",
    "normalise_artist",
    "load_local_tracks",
    "ACCEPT_CONFIDENCE",
    "REVIEW_CONFIDENCE",
    "DURATION_TOLERANCE_MS",
    "MIN_SIGNALS_FOR_AUTO",
]


ACCEPT_CONFIDENCE: float = 0.70
REVIEW_CONFIDENCE: float = 0.50
DURATION_TOLERANCE_MS: int = 1500
MIN_SIGNALS_FOR_AUTO: int = 3

_W_ISRC: float = 0.35
_W_TITLE: float = 0.20
_W_ARTIST: float = 0.15
_W_DURATION: float = 0.20


@dataclass(frozen=True)
class LocalTrack:
    """Minimal projection of a state-layer ``tracks`` row."""

    stable_id: str
    isrc: str | None
    title: str
    artists: tuple[str, ...]
    duration_ms: int | None

    @property
    def artists_joined(self) -> str:
        return ", ".join(self.artists)


@dataclass(frozen=True)
class MatchedPair:
    """Decision for one Spotify track."""

    source: SpotifyTrack
    target: LocalTrack | None
    confidence: float
    signals: tuple[str, ...]
    status: Literal["matched", "review", "unmatched"]


@dataclass
class MatchResult:
    pairs: list[MatchedPair] = field(default_factory=list)

    @property
    def matched(self) -> list[MatchedPair]:
        return [p for p in self.pairs if p.status == "matched"]

    @property
    def review(self) -> list[MatchedPair]:
        return [p for p in self.pairs if p.status == "review"]

    @property
    def unmatched(self) -> list[MatchedPair]:
        return [p for p in self.pairs if p.status == "unmatched"]

    @property
    def match_rate(self) -> float:
        if not self.pairs:
            return 0.0
        return len(self.matched) / len(self.pairs)


_TITLE_SUFFIX_RE = re.compile(
    r"""
    \s*
    [\-\(\[]*
    \s*
    (remaster(?:ed)?(?:\s*\d{4})?
     |radio\s*edit
     |club\s*mix
     |extended\s*mix
     |original\s*mix
     |single\s*version
     |deluxe(?:\s*edition)?
     |explicit
     |clean(?:\s*version)?
     |bonus\s*track
     |from\s*['"].*?['"]
     )
    [\-\)\]]*
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Fuzzy-match-friendly title: strip version suffix, punct, casefold."""
    if not title:
        return ""
    s = title
    for _ in range(3):
        new = _TITLE_SUFFIX_RE.sub("", s).rstrip(" -()[]")
        if new == s:
            break
        s = new
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip().casefold()
    return s


def normalise_artist(artist: str) -> str:
    """Casefold + strip punct; callers compute set overlap over per-artist."""
    if not artist:
        return ""
    s = _PUNCT_RE.sub(" ", artist)
    s = _WS_RE.sub(" ", s).strip().casefold()
    return s


def _artist_set(artists: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for a in artists:
        n = normalise_artist(a)
        if n:
            out.add(n)
    return out


def _score_pair(src: SpotifyTrack, tgt: LocalTrack) -> tuple[float, list[str]]:
    signals: list[str] = []
    score = 0.0

    if src.isrc and tgt.isrc and src.isrc.upper() == tgt.isrc.upper():
        score += _W_ISRC
        signals.append("isrc")

    nt_src = normalise_title(src.title)
    if nt_src and nt_src == normalise_title(tgt.title):
        score += _W_TITLE
        signals.append("title")

    src_set = _artist_set(src.artists)
    tgt_set = _artist_set(tgt.artists)
    if src_set and tgt_set and (src_set & tgt_set):
        score += _W_ARTIST
        signals.append("artist")

    if tgt.duration_ms is not None and src.duration_ms:
        if abs(src.duration_ms - tgt.duration_ms) <= DURATION_TOLERANCE_MS:
            score += _W_DURATION
            signals.append("duration")

    return score, signals


def _classify(confidence: float, signals: list[str]) -> Literal["matched", "review", "unmatched"]:
    if "isrc" in signals:
        return "matched"
    if confidence >= ACCEPT_CONFIDENCE and len(signals) >= MIN_SIGNALS_FOR_AUTO:
        return "matched"
    if confidence >= REVIEW_CONFIDENCE and len(signals) >= 2:
        return "review"
    return "unmatched"


def match_spotify_tracks(
    sources: Iterable[SpotifyTrack],
    targets: Iterable[LocalTrack],
) -> MatchResult:
    """Classify every source into matched / review / unmatched.

    O(S * T). Fine up to a few thousand tracks. ISRC index short-circuits
    the hottest path.
    """
    targets_list = list(targets)
    isrc_index: dict[str, LocalTrack] = {}
    for t in targets_list:
        if t.isrc:
            isrc_index[t.isrc.upper()] = t

    pairs: list[MatchedPair] = []
    for src in sources:
        if src.isrc:
            tgt = isrc_index.get(src.isrc.upper())
            if tgt is not None:
                conf, sigs = _score_pair(src, tgt)
                status = _classify(conf, sigs)
                pairs.append(MatchedPair(src, tgt, conf, tuple(sigs), status))
                continue

        best_tgt: LocalTrack | None = None
        best_conf: float = 0.0
        best_sigs: list[str] = []
        for tgt in targets_list:
            conf, sigs = _score_pair(src, tgt)
            if conf > best_conf:
                best_conf = conf
                best_sigs = sigs
                best_tgt = tgt

        status = _classify(best_conf, best_sigs)
        if status == "unmatched":
            best_tgt = None
        pairs.append(MatchedPair(src, best_tgt, best_conf, tuple(best_sigs), status))

    return MatchResult(pairs=pairs)


def load_local_tracks(conn: sqlite3.Connection) -> list[LocalTrack]:
    """Read every row from state-layer ``tracks`` as :class:`LocalTrack`."""
    import json as _json

    rows = conn.execute(
        "SELECT stable_id, isrc, title, artists_json, duration_ms FROM tracks"
    ).fetchall()
    out: list[LocalTrack] = []
    for stable_id, isrc, title, artists_json, duration_ms in rows:
        artists: tuple[str, ...] = ()
        if artists_json:
            try:
                parsed = _json.loads(artists_json)
                if isinstance(parsed, list):
                    artists = tuple(str(a) for a in parsed if a)
                elif isinstance(parsed, str):
                    artists = (parsed,)
            except (ValueError, TypeError):
                artists = ()
        out.append(
            LocalTrack(
                stable_id=stable_id,
                isrc=isrc,
                title=title or "",
                artists=artists,
                duration_ms=duration_ms,
            )
        )
    return out
