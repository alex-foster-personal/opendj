"""Triple-validated Rekordbox <-> djay track matcher.

Implements the six-signal stack from Phase 2 CONTEXT D2:

1. ``isrc_exact``     -- 0.35 -- ISRC equality (whitespace-stripped, uppercased)
2. ``filename_exact`` -- 0.20 -- NFC-normalised basenames, case-insensitive
3. ``filename_fuzzy`` -- 0.15 -- difflib.SequenceMatcher >= 0.85
4. ``duration``       -- 0.15 -- absolute delta <= 0.5s
5. ``id3``            -- 0.15 -- title + artist each match via fuzzy ratio >= 0.9
6. ``chromaprint``    -- 0.30 -- lazy, only when cheap checks fire < 3

Accept policy: >=3 signals fired AND sum of weights >= 0.70 -> auto-accept.
2 signals -> review bucket. 0-1 signals -> ``rb_only``/``djay_only``.

Ports ``_normalise_for_match`` + indexing strategy from the companion repo
(``~/Desktop/music-dj/src/sync.py`` lines 86-326) and extends it with the
signal-based scoring model for Phase 2.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from apps.shared.djay_db import DjayTrack


WEIGHTS: dict[str, float] = {
    "isrc_exact": 0.35,
    "filename_exact": 0.20,
    "filename_fuzzy": 0.15,
    "duration": 0.15,
    "id3": 0.15,
    "chromaprint": 0.30,
}

#: Minimum number of signals that must fire for an auto-accept.
MIN_SIGNALS_FOR_ACCEPT: int = 3

#: Minimum confidence threshold (summed weight) for an auto-accept.
MIN_CONFIDENCE_FOR_ACCEPT: float = 0.70


# ---------------------------------------------------------------------------
# Normalisation (ported verbatim from companion sync.py)
# ---------------------------------------------------------------------------


def _normalise_for_match(text: str) -> str:
    """Normalise a string for fuzzy matching.

    - NFC fold
    - lowercase
    - collapse whitespace
    - strip parenthesised / bracketed remix/edit/version/mix markers
    - unify "feat." / "featuring" -> "ft. "
    """
    if not text:
        return ""
    result = text.strip().lower()
    result = unicodedata.normalize("NFC", result)
    result = re.sub(r"\bfeat(?:uring)?\.?\s", "ft. ", result)
    result = re.sub(
        r"\s*\([^)]*(?:remix|edit|mix|version|radio|extended|original|vip|dub)\s*[^)]*\)",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"\s*\[[^\]]*(?:remix|edit|mix|version|radio|extended|original|vip|dub)\s*[^\]]*\]",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(r"\s+", " ", result).strip()
    return result


def _make_match_key(title: str, artist: str) -> str:
    return f"{_normalise_for_match(title)}|||{_normalise_for_match(artist)}"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UnifiedTrack:
    """Normalised cross-platform track reference (for the matched bucket).

    Fields extend the companion's minimal shape with everything the Phase 2
    matcher + SYNC-03 CSV consumer need.
    """

    title: str = ""
    artist: str = ""
    rating: int = 0
    bpm: float | None = None
    key: str | None = None
    play_count: int = 0
    genre: str | None = None
    color_tag: str | None = None
    source_rekordbox_id: str | None = None
    source_djay_key: str | None = None
    uuid: str | None = None
    file_path: Path | None = None
    duration_s: float | None = None
    isrc: str | None = None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UnifiedTrack):
            return NotImplemented
        return (
            _normalise_for_match(self.title) == _normalise_for_match(other.title)
            and _normalise_for_match(self.artist) == _normalise_for_match(other.artist)
        )

    def __hash__(self) -> int:
        return hash(
            (
                _normalise_for_match(self.title),
                _normalise_for_match(self.artist),
            )
        )


@dataclass(slots=True)
class Signal:
    """Single matcher signal result."""

    name: str
    weight: float
    fired: bool
    detail: str = ""


@dataclass(slots=True)
class MatchedPair:
    """A track that exists in both platforms with scoring rationale."""

    rb_id: str
    djay_uuid: str
    rb_title: str
    rb_artist: str
    djay_title: str
    djay_artist: str
    confidence: float
    signals: tuple[str, ...]
    rationale: str
    status: str  # "matched" | "review"


@dataclass(slots=True)
class MatchResult:
    """Output of the track matching phase (SYNC-02)."""

    matched: list[MatchedPair] = field(default_factory=list)
    review: list[MatchedPair] = field(default_factory=list)
    rb_only: list[Any] = field(default_factory=list)
    djay_only: list[DjayTrack] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Signal scorer
# ---------------------------------------------------------------------------


def _basename_nfc(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return unicodedata.normalize("NFC", path.name).lower()
    except Exception:
        return None


def _read_id3(path: Path | None) -> tuple[str, str] | None:
    """Return ``(title, artist)`` from ID3 tags, or ``None`` on any failure."""
    if path is None:
        return None
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(path), easy=True)
        if audio is None:
            return None
        title = (audio.get("title") or [""])[0] or ""
        artist = (audio.get("artist") or [""])[0] or ""
        return (title, artist)
    except Exception:
        return None


def score_pair(
    rb: Any,
    dj: DjayTrack,
    *,
    fingerprint_fn: Callable[[Any, DjayTrack], Signal | None] | None = None,
    _skip_fingerprint: bool = False,
) -> tuple[float, list[Signal]]:
    """Score an (RB, djay) candidate pair with the six-signal stack.

    ``fingerprint_fn`` is invoked only when signals 1-5 fire fewer than
    ``MIN_SIGNALS_FOR_ACCEPT``. Pass ``None`` to disable (5-signal mode).

    ``_skip_fingerprint`` is an internal knob used by ``match_tracks`` for
    the initial pass; when True we never call the fingerprint callback.
    """
    signals: list[Signal] = []

    # 1) ISRC exact ---------------------------------------------------------
    rb_isrc = (getattr(rb, "isrc", "") or "").strip().upper()
    dj_isrc = (getattr(dj, "isrc", "") or "").strip().upper()
    isrc_fire = bool(rb_isrc) and bool(dj_isrc) and rb_isrc == dj_isrc
    signals.append(
        Signal(
            name="isrc_exact",
            weight=WEIGHTS["isrc_exact"],
            fired=isrc_fire,
            detail=rb_isrc if isrc_fire else "",
        )
    )

    # 2) Filename exact -----------------------------------------------------
    rb_base = _basename_nfc(getattr(rb, "file_path", None))
    dj_base = _basename_nfc(getattr(dj, "file_path", None))
    fn_exact = bool(rb_base) and bool(dj_base) and rb_base == dj_base
    signals.append(
        Signal(
            name="filename_exact",
            weight=WEIGHTS["filename_exact"],
            fired=fn_exact,
            detail=(rb_base or "") if fn_exact else "",
        )
    )

    # 3) Filename fuzzy (only meaningful if exact DIDN'T fire) --------------
    fn_fuzzy_ratio = 0.0
    fn_fuzzy = False
    if rb_base and dj_base and not fn_exact:
        fn_fuzzy_ratio = difflib.SequenceMatcher(None, rb_base, dj_base).ratio()
        fn_fuzzy = fn_fuzzy_ratio >= 0.85
    signals.append(
        Signal(
            name="filename_fuzzy",
            weight=WEIGHTS["filename_fuzzy"],
            fired=fn_fuzzy,
            detail=f"{fn_fuzzy_ratio:.2f}" if fn_fuzzy else "",
        )
    )

    # 4) Duration within 0.5s ----------------------------------------------
    rb_dur = getattr(rb, "duration_s", None)
    dj_dur = getattr(dj, "duration_s", None)
    dur_fire = (
        rb_dur is not None
        and dj_dur is not None
        and abs(float(rb_dur) - float(dj_dur)) <= 0.5
    )
    signals.append(
        Signal(
            name="duration",
            weight=WEIGHTS["duration"],
            fired=dur_fire,
            detail=f"{rb_dur}~{dj_dur}" if dur_fire else "",
        )
    )

    # 5) ID3 title/artist ---------------------------------------------------
    id3_fire = False
    id3_detail = ""
    rb_id3 = _read_id3(getattr(rb, "file_path", None))
    dj_id3 = _read_id3(getattr(dj, "file_path", None))
    if rb_id3 and dj_id3:
        t_ratio = difflib.SequenceMatcher(
            None, rb_id3[0].lower(), dj_id3[0].lower()
        ).ratio()
        a_ratio = difflib.SequenceMatcher(
            None, rb_id3[1].lower(), dj_id3[1].lower()
        ).ratio()
        if t_ratio >= 0.9 and a_ratio >= 0.9:
            id3_fire = True
            id3_detail = f"t={t_ratio:.2f},a={a_ratio:.2f}"
    signals.append(
        Signal(
            name="id3",
            weight=WEIGHTS["id3"],
            fired=id3_fire,
            detail=id3_detail,
        )
    )

    # 6) Chromaprint (lazy) -------------------------------------------------
    fp_signal: Signal | None = None
    fired_cheap = sum(1 for s in signals if s.fired)
    if (
        not _skip_fingerprint
        and fingerprint_fn is not None
        and fired_cheap < MIN_SIGNALS_FOR_ACCEPT
    ):
        try:
            fp_signal = fingerprint_fn(rb, dj)
        except Exception:
            fp_signal = None
    if fp_signal is None:
        fp_signal = Signal(
            name="chromaprint", weight=WEIGHTS["chromaprint"], fired=False
        )
    signals.append(fp_signal)

    confidence = sum(s.weight for s in signals if s.fired)
    return confidence, signals


# ---------------------------------------------------------------------------
# match_tracks
# ---------------------------------------------------------------------------


def _nfc_abs(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return unicodedata.normalize("NFC", str(path.resolve())).lower()
    except Exception:
        return unicodedata.normalize("NFC", str(path)).lower()


def _unified_from_rb(rb: Any) -> UnifiedTrack:
    return UnifiedTrack(
        title=getattr(rb, "title", "") or "",
        artist=getattr(rb, "artist", "") or "",
        rating=int(getattr(rb, "rating", 0) or 0),
        bpm=getattr(rb, "bpm", None),
        play_count=int(getattr(rb, "play_count", 0) or 0),
        genre=getattr(rb, "genre", None),
        source_rekordbox_id=str(getattr(rb, "id", "") or ""),
        file_path=getattr(rb, "file_path", None),
        duration_s=getattr(rb, "duration_s", None),
        isrc=getattr(rb, "isrc", None),
    )


def _unified_from_dj(dj: DjayTrack) -> UnifiedTrack:
    return UnifiedTrack(
        title=dj.title,
        artist=dj.artist,
        rating=dj.rating,
        play_count=dj.play_count,
        source_djay_key=dj.uuid,
        uuid=dj.uuid,
        file_path=dj.file_path,
        duration_s=dj.duration_s,
        isrc=dj.isrc or None,
    )


def _build_rationale(signals: list[Signal]) -> str:
    fired = [s for s in signals if s.fired]
    if not fired:
        return "no signals fired"
    parts = []
    for s in fired:
        if s.detail:
            parts.append(f"{s.name}({s.detail})")
        else:
            parts.append(s.name)
    return "+".join(parts)


def match_tracks(
    rb_tracks: Iterable[Any],
    dj_tracks: Iterable[DjayTrack],
    *,
    fingerprint_fn: Callable[[Any, DjayTrack], Signal | None] | None = None,
) -> MatchResult:
    """Bucket RB + djay tracks into matched / review / rb-only / djay-only.

    Strategy (Phase 2 D2):

    1. Build O(1) indices: by NFC absolute path, by ISRC, and by
       ``_make_match_key(title, artist)``.
    2. For each djay track, look up candidate RB tracks via each index in
       that order. Score the pair with :func:`score_pair`.
    3. If candidate-cheap-signals < 3 and ``fingerprint_fn`` supplied, the
       expensive fingerprint compare fires via the scorer.
    4. Emit three buckets based on ``(fired_count, confidence)``.
    """
    rb_list = list(rb_tracks)
    dj_list = list(dj_tracks)

    # Build indices.
    path_to_rb: dict[str, Any] = {}
    isrc_to_rb: dict[str, Any] = {}
    key_to_rb: dict[str, list[Any]] = {}
    for rb in rb_list:
        path_key = _nfc_abs(getattr(rb, "file_path", None))
        if path_key:
            path_to_rb.setdefault(path_key, rb)
        isrc = (getattr(rb, "isrc", "") or "").strip().upper()
        if isrc:
            isrc_to_rb.setdefault(isrc, rb)
        mkey = _make_match_key(
            getattr(rb, "title", "") or "", getattr(rb, "artist", "") or ""
        )
        key_to_rb.setdefault(mkey, []).append(rb)

    matched_rb_ids: set[str] = set()
    matched_dj_uuids: set[str] = set()
    result = MatchResult()

    for dj in dj_list:
        # Find candidate RB via path, then ISRC, then normalised key.
        candidate: Any | None = None
        dj_path_key = _nfc_abs(dj.file_path)
        if dj_path_key and dj_path_key in path_to_rb:
            candidate = path_to_rb[dj_path_key]
        elif dj.isrc and dj.isrc.strip().upper() in isrc_to_rb:
            candidate = isrc_to_rb[dj.isrc.strip().upper()]
        else:
            mkey = _make_match_key(dj.title, dj.artist)
            rb_candidates = key_to_rb.get(mkey, [])
            if rb_candidates:
                candidate = rb_candidates[0]

        if candidate is None:
            continue

        confidence, signals = score_pair(
            candidate, dj, fingerprint_fn=fingerprint_fn
        )
        fired = [s for s in signals if s.fired]
        fired_count = len(fired)
        fired_names = tuple(s.name for s in fired)

        pair = MatchedPair(
            rb_id=str(getattr(candidate, "id", "") or ""),
            djay_uuid=dj.uuid,
            rb_title=getattr(candidate, "title", "") or "",
            rb_artist=getattr(candidate, "artist", "") or "",
            djay_title=dj.title,
            djay_artist=dj.artist,
            confidence=round(confidence, 4),
            signals=fired_names,
            rationale=_build_rationale(signals),
            status="matched"
            if fired_count >= MIN_SIGNALS_FOR_ACCEPT
            and confidence >= MIN_CONFIDENCE_FOR_ACCEPT
            else "review"
            if fired_count >= 2
            else "drop",
        )

        if pair.status == "matched":
            result.matched.append(pair)
            matched_rb_ids.add(pair.rb_id)
            matched_dj_uuids.add(pair.djay_uuid)
        elif pair.status == "review":
            result.review.append(pair)
            # Review candidates stay in the unmatched sets for downstream
            # decisions but we still reserve the dj uuid.
            matched_dj_uuids.add(pair.djay_uuid)

    result.rb_only = [
        rb
        for rb in rb_list
        if str(getattr(rb, "id", "") or "") not in matched_rb_ids
    ]
    result.djay_only = [dj for dj in dj_list if dj.uuid not in matched_dj_uuids]

    result.stats = {
        "rb_total": len(rb_list),
        "dj_total": len(dj_list),
        "matched": len(result.matched),
        "review": len(result.review),
        "rb_only": len(result.rb_only),
        "djay_only": len(result.djay_only),
    }

    return result


__all__ = [
    "WEIGHTS",
    "MIN_SIGNALS_FOR_ACCEPT",
    "MIN_CONFIDENCE_FOR_ACCEPT",
    "MatchResult",
    "MatchedPair",
    "Signal",
    "UnifiedTrack",
    "_make_match_key",
    "_normalise_for_match",
    "match_tracks",
    "score_pair",
]
