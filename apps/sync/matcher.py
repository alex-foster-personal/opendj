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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.shared.djay_db import DjayTrack
from apps.shared.tag_reader import TagReadError, read_tags

# NOTE: Weight sums to 1.30, not 1.00. This is deliberate and NOT a
# probability distribution. Each signal independently contributes evidence;
# overlapping high-value signals (ISRC + chromaprint) are rewarded. The
# 3-signals-AND-confidence rule below (see MIN_SIGNALS_FOR_ACCEPT and
# MIN_CONFIDENCE_FOR_ACCEPT) is the real accept gate. Do not rescale to 1.0
# without also re-tuning the thresholds; existing test corpora are calibrated
# to these values. A 2-signal ISRC+chromaprint hit (0.65 confidence, below the
# 0.70 gate) falls to the review bucket by design: review bucket is cheap and
# the goal is zero false auto-accepts on the first live pass. See
# 02-CONTEXT.md D2 and 02-REVIEW.md [I2] for the deliberation.
WEIGHTS: dict[str, float] = {
    "isrc_exact": 0.35,
    "filename_exact": 0.20,
    "filename_fuzzy": 0.15,
    "duration": 0.15,
    "id3": 0.15,
    "chromaprint": 0.30,
}

#: Minimum number of signals that must fire for an auto-accept.
#: Combined with MIN_CONFIDENCE_FOR_ACCEPT; both gates must pass.
MIN_SIGNALS_FOR_ACCEPT: int = 3

#: Minimum confidence threshold (summed weight) for an auto-accept.
#: Combined with MIN_SIGNALS_FOR_ACCEPT; both gates must pass.
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
    """Return ``(title, artist)`` from the file's tags, or ``None`` if unreadable.

    Unreadable means the signal does not fire; it never stops matching.
    """
    if path is None:
        return None
    try:
        tags = read_tags(path)
    except TagReadError:
        return None
    return (tags.title or "", tags.artist or "")


def _signal_isrc(rb: Any, dj: DjayTrack) -> Signal:
    rb_isrc = (getattr(rb, "isrc", "") or "").strip().upper()
    dj_isrc = (getattr(dj, "isrc", "") or "").strip().upper()
    fired = bool(rb_isrc) and bool(dj_isrc) and rb_isrc == dj_isrc
    return Signal(
        name="isrc_exact",
        weight=WEIGHTS["isrc_exact"],
        fired=fired,
        detail=rb_isrc if fired else "",
    )


def _signal_filename_exact(rb: Any, dj: DjayTrack) -> tuple[Signal, str | None, str | None]:
    """Returns (signal, rb_base, dj_base) so fuzzy variant can reuse bases."""
    rb_base = _basename_nfc(getattr(rb, "file_path", None))
    dj_base = _basename_nfc(getattr(dj, "file_path", None))
    fired = bool(rb_base) and bool(dj_base) and rb_base == dj_base
    signal = Signal(
        name="filename_exact",
        weight=WEIGHTS["filename_exact"],
        fired=fired,
        detail=(rb_base or "") if fired else "",
    )
    return signal, rb_base, dj_base


def _signal_filename_fuzzy(
    rb_base: str | None, dj_base: str | None, exact_fired: bool
) -> Signal:
    ratio = 0.0
    fired = False
    if rb_base and dj_base and not exact_fired:
        ratio = difflib.SequenceMatcher(None, rb_base, dj_base).ratio()
        fired = ratio >= 0.85
    return Signal(
        name="filename_fuzzy",
        weight=WEIGHTS["filename_fuzzy"],
        fired=fired,
        detail=f"{ratio:.2f}" if fired else "",
    )


def _signal_duration(rb: Any, dj: DjayTrack) -> Signal:
    rb_dur = getattr(rb, "duration_s", None)
    dj_dur = getattr(dj, "duration_s", None)
    fired = (
        rb_dur is not None
        and dj_dur is not None
        and abs(float(rb_dur) - float(dj_dur)) <= 0.5
    )
    return Signal(
        name="duration",
        weight=WEIGHTS["duration"],
        fired=fired,
        detail=f"{rb_dur}~{dj_dur}" if fired else "",
    )


def _signal_id3(rb: Any, dj: DjayTrack) -> Signal:
    fired = False
    detail = ""
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
            fired = True
            detail = f"t={t_ratio:.2f},a={a_ratio:.2f}"
    return Signal(
        name="id3", weight=WEIGHTS["id3"], fired=fired, detail=detail
    )


def _signal_chromaprint(
    rb: Any,
    dj: DjayTrack,
    cheap_signals: list[Signal],
    *,
    fingerprint_fn: Callable[[Any, DjayTrack], Signal | None] | None,
    skip: bool,
) -> Signal:
    fp_signal: Signal | None = None
    fired_cheap = sum(1 for s in cheap_signals if s.fired)
    if (
        not skip
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
    return fp_signal


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
    isrc_sig = _signal_isrc(rb, dj)
    fn_exact_sig, rb_base, dj_base = _signal_filename_exact(rb, dj)
    fn_fuzzy_sig = _signal_filename_fuzzy(rb_base, dj_base, fn_exact_sig.fired)
    dur_sig = _signal_duration(rb, dj)
    id3_sig = _signal_id3(rb, dj)
    cheap = [isrc_sig, fn_exact_sig, fn_fuzzy_sig, dur_sig, id3_sig]
    fp_sig = _signal_chromaprint(
        rb, dj, cheap, fingerprint_fn=fingerprint_fn, skip=_skip_fingerprint
    )
    signals = [*cheap, fp_sig]
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


def _build_rb_indices(
    rb_list: list[Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[Any]]]:
    """Build O(1) lookup indices for RB tracks: by path, ISRC, normalised key."""
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
    return path_to_rb, isrc_to_rb, key_to_rb


def _pick_best_by_key(
    rb_candidates: list[Any],
    dj: DjayTrack,
    *,
    fingerprint_fn: Callable[[Any, DjayTrack], Signal | None] | None,
) -> tuple[Any | None, tuple[float, list[Signal]] | None]:
    """Score all RB candidates sharing the normalised key; return the best.

    Tie-break: confidence first, then fired-signal count. Preserves the
    Codex P02-F1 fix (score ALL candidates, pick best; never just the first
    one) -- do NOT collapse back to ``rb_candidates[0]``.
    """
    best_rb: Any | None = None
    best_score: tuple[float, int] = (-1.0, -1)
    best_sigs: list[Signal] = []
    best_conf: float = 0.0
    for rb in rb_candidates:
        conf_i, sigs_i = score_pair(rb, dj, fingerprint_fn=fingerprint_fn)
        fired_i = sum(1 for s in sigs_i if s.fired)
        score_i = (conf_i, fired_i)
        if score_i > best_score:
            best_score = score_i
            best_rb = rb
            best_sigs = sigs_i
            best_conf = conf_i
    if best_rb is None:
        return None, None
    return best_rb, (best_conf, best_sigs)


def _find_candidate(
    dj: DjayTrack,
    path_to_rb: dict[str, Any],
    isrc_to_rb: dict[str, Any],
    key_to_rb: dict[str, list[Any]],
    *,
    fingerprint_fn: Callable[[Any, DjayTrack], Signal | None] | None,
) -> tuple[Any | None, tuple[float, list[Signal]] | None]:
    """Find the best RB candidate for a djay track via path / ISRC / key."""
    dj_path_key = _nfc_abs(dj.file_path)
    if dj_path_key and dj_path_key in path_to_rb:
        return path_to_rb[dj_path_key], None
    if dj.isrc and dj.isrc.strip().upper() in isrc_to_rb:
        return isrc_to_rb[dj.isrc.strip().upper()], None
    mkey = _make_match_key(dj.title, dj.artist)
    rb_candidates = key_to_rb.get(mkey, [])
    if rb_candidates:
        return _pick_best_by_key(
            rb_candidates, dj, fingerprint_fn=fingerprint_fn
        )
    return None, None


def _classify_status(fired_count: int, confidence: float) -> str:
    if (
        fired_count >= MIN_SIGNALS_FOR_ACCEPT
        and confidence >= MIN_CONFIDENCE_FOR_ACCEPT
    ):
        return "matched"
    if fired_count >= 2:
        return "review"
    return "drop"


def _build_matched_pair(
    candidate: Any,
    dj: DjayTrack,
    confidence: float,
    signals: list[Signal],
) -> MatchedPair:
    fired = [s for s in signals if s.fired]
    fired_count = len(fired)
    fired_names = tuple(s.name for s in fired)
    return MatchedPair(
        rb_id=str(getattr(candidate, "id", "") or ""),
        djay_uuid=dj.uuid,
        rb_title=getattr(candidate, "title", "") or "",
        rb_artist=getattr(candidate, "artist", "") or "",
        djay_title=dj.title,
        djay_artist=dj.artist,
        confidence=round(confidence, 4),
        signals=fired_names,
        rationale=_build_rationale(signals),
        status=_classify_status(fired_count, confidence),
    )


def _match_one_dj(
    dj: DjayTrack,
    path_to_rb: dict[str, Any],
    isrc_to_rb: dict[str, Any],
    key_to_rb: dict[str, list[Any]],
    *,
    fingerprint_fn: Callable[[Any, DjayTrack], Signal | None] | None,
) -> MatchedPair | None:
    """Locate the best RB candidate for one djay track and build its pair."""
    candidate, precomputed = _find_candidate(
        dj, path_to_rb, isrc_to_rb, key_to_rb, fingerprint_fn=fingerprint_fn
    )
    if candidate is None:
        return None
    if precomputed is not None:
        confidence, signals = precomputed
    else:
        confidence, signals = score_pair(
            candidate, dj, fingerprint_fn=fingerprint_fn
        )
    return _build_matched_pair(candidate, dj, confidence, signals)


def _finalize_result(
    result: MatchResult,
    rb_list: list[Any],
    dj_list: list[DjayTrack],
    matched_rb_ids: set[str],
    matched_dj_uuids: set[str],
) -> None:
    """Populate rb_only, djay_only, and stats on ``result`` in place."""
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

    path_to_rb, isrc_to_rb, key_to_rb = _build_rb_indices(rb_list)

    matched_rb_ids: set[str] = set()
    matched_dj_uuids: set[str] = set()
    result = MatchResult()

    for dj in dj_list:
        pair = _match_one_dj(
            dj,
            path_to_rb,
            isrc_to_rb,
            key_to_rb,
            fingerprint_fn=fingerprint_fn,
        )
        if pair is None:
            continue
        if pair.status == "matched":
            result.matched.append(pair)
            matched_rb_ids.add(pair.rb_id)
            matched_dj_uuids.add(pair.djay_uuid)
        elif pair.status == "review":
            result.review.append(pair)
            # Review candidates stay in the unmatched sets for downstream
            # decisions but we still reserve the dj uuid.
            matched_dj_uuids.add(pair.djay_uuid)

    _finalize_result(result, rb_list, dj_list, matched_rb_ids, matched_dj_uuids)
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
