"""Window-level pair scoring (``score_pair``).

Track-level compatibility already exists (``apps/shared/harmonic.py``
``track_compatibility``); this scores a PAIR OF WINDOWS, which is where clashes
actually happen: the key at those bars (key segments when the track has them),
the tempo gap Beat Sync has to close, vocals and basslines that would meet
inside the overlap, and the energy step from the outgoing window to the
incoming one.

Energy is relative within each track (see :mod:`apps.autodj.profile`), so the
energy delta compares "how hot is this part of its own track", not loudness
across tracks. The reasons say so rather than overclaim.

AGENT-33: a term that cannot be measured (no key, no tempo, no vocal map) is
UNSCORED, never filled with a neutral 0.5: the score is the weighted mean of
the terms that were measured, ``measured`` of ``TERMS`` says how many, and
``unscored`` names the rest with the reason. Ranking (:func:`rank_key`) puts
more measured terms first, so a pair whose unmeasured term could be the bad
one never outranks a pair measured on every term.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from apps.autodj.plan import STRONG_BASS, _overlaps, beat_sync_reach_pct, bpm_gap_pct, pair_beat_sync_fold
from apps.autodj.profile import VOCAL_BAR_THRESHOLD, TrackProfile, resolve_key_at_bar
from apps.autodj.windows import MixWindow, mix_windows
from apps.shared.harmonic import CamelotKey, camelot_distance, key_compat

#: Overlap assumed when scoring windows without a style chosen yet.
DEFAULT_OVERLAP_BARS = 16
#: The pair terms and their weights; a term not measured is left out of the mean, never valued.
TERM_WEIGHTS: dict[str, float] = {"harmony": 0.3, "tempo": 0.25, "vocals": 0.2, "windows": 0.25}


@dataclass(frozen=True)
class PairScore:
    score: float
    key_distance: int | None
    #: The Camelot rule the pair's keys make, out -> in (``clash`` when none does); None when a key is unknown.
    key_relation: str | None
    key_out: str | None
    key_in: str | None
    bpm_gap_pct: float | None
    beatmatchable: bool | None
    vocal_clash_bars: (
        int | None
    )  # bars where both maps are >= VOCAL_BAR_THRESHOLD; 0 is measured, None is a missing map
    bass_clash_bars: int | None
    energy_delta: float | None
    overlap_bars: int
    out_window: MixWindow
    in_window: MixWindow
    reasons: tuple[str, ...]
    #: Terms that could not be measured, each with why: they carry no value in ``score``.
    unscored: tuple[tuple[str, str], ...] = ()

    @property
    def measured(self) -> int:
        return len(TERM_WEIGHTS) - len(self.unscored)

    def summary(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "measured": f"{self.measured} of {len(TERM_WEIGHTS)}",
            "unscored": [{"term": term, "reason": reason} for term, reason in self.unscored],
            "key_out": self.key_out,
            "key_in": self.key_in,
            "key_distance": self.key_distance,
            "key_relation": self.key_relation,
            "bpm_gap_pct": None if self.bpm_gap_pct is None else round(self.bpm_gap_pct, 2),
            "beatmatchable": self.beatmatchable,
            "vocal_clash_bars": self.vocal_clash_bars,
            "bass_clash_bars": self.bass_clash_bars,
            "energy_delta": None if self.energy_delta is None else round(self.energy_delta, 2),
            "overlap_bars": self.overlap_bars,
            "out_window": self.out_window.summary(),
            "in_window": self.in_window.summary(),
            "reasons": list(self.reasons),
        }


def key_at(profile: TrackProfile, bar: int) -> str | None:
    """The key at a bar: a key segment covering it, else the track key."""
    for segment in profile.key_segments:
        start, end, key = segment.start_bar, segment.end_bar, segment.key
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and isinstance(key, str)
            and start <= bar <= end
        ):
            return key
    return profile.key


def _clashes(
    out_profile: TrackProfile, in_profile: TrackProfile, out_start: int, in_start: int, bars: int, fold: float = 1.0
) -> tuple[int | None, int | None]:
    """Clash counts over ``bars`` outgoing bars; the incoming deck advances ``fold`` of its bars per outgoing bar."""
    vocals_known = out_profile.bar_vocals is not None and in_profile.bar_vocals is not None
    bass_known = out_profile.bar_bass is not None and in_profile.bar_bass is not None
    vocal = 0
    bass = 0
    for offset in range(bars):
        out_bar, in_bar = out_start + offset, in_start + math.floor(offset * fold)
        if _overlaps(out_profile.bar_vocals, in_profile.bar_vocals, out_bar, in_bar, VOCAL_BAR_THRESHOLD):
            vocal += 1
        if _overlaps(out_profile.bar_bass, in_profile.bar_bass, out_bar, in_bar, STRONG_BASS):
            bass += 1
    return (vocal if vocals_known else None, bass if bass_known else None)


def rank_key(pair: PairScore, score: float | None = None) -> tuple[int, float]:
    """How pairs rank: more measured terms first, then the score (``score`` overrides the pair's own)."""
    return (pair.measured, pair.score if score is None else score)


def _weighted_mean(terms: dict[str, float]) -> float:
    """The weighted mean over the measured terms only."""
    weight = sum(TERM_WEIGHTS[name] for name in terms)
    return sum(TERM_WEIGHTS[name] * value for name, value in terms.items()) / weight


def _tempo_term(gap: float | None, reasons: list[str], reach_pct: float) -> tuple[bool | None, float | None]:
    """Whether Beat Sync reaches the gap, and the tempo term (None, unscored, when a tempo is unknown)."""
    if gap is None:
        reasons.append("a tempo is unknown: tempo not scored")
        return None, None
    beatmatchable = gap <= reach_pct
    reasons.append(f"tempo gap {gap:.1f}% ({'within' if beatmatchable else 'beyond'} Beat Sync's reach)")
    return beatmatchable, max(0.0, 1.0 - gap / (2 * reach_pct))


def _vocal_term(vocal_clash: int | None, bass_clash: int | None, bars: int, reasons: list[str]) -> float | None:
    """The vocals term (None, unscored, without a vocal map), noting the clashes in ``reasons``."""
    if vocal_clash is None:
        reasons.append("a vocal map is missing: vocals not scored")
    if vocal_clash:
        reasons.append(f"vocals meet for {vocal_clash} of {bars} overlap bars")
    if bass_clash:
        reasons.append(f"both basslines are strong for {bass_clash} bars (a bass swap fixes it)")
    return None if vocal_clash is None else 1.0 - vocal_clash / bars


def _append_region_sources(
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    out_sources: set[str],
    in_sources: set[str],
    reasons: list[str],
) -> None:
    for label, profile, sources in (
        ("out", out_profile, out_sources),
        ("in", in_profile, in_sources),
    ):
        if not profile.key_segments:
            reasons.append(f"{label} region.source global")
        elif "segment" in sources:
            reasons.append(f"{label} region.source segment")
        else:
            reasons.append(f"{label} region.source global")


def _harmony_over_overlap_span(
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    out_start: int,
    in_start: int,
    bars: int,
    fold: float,
    reasons: list[str],
) -> tuple[int | None, str | None, float | None, str | None, str | None]:
    worst_term: float | None = None
    worst_out: str | None = None
    worst_in: str | None = None
    out_sources: set[str] = set()
    in_sources: set[str] = set()
    for offset in range(bars):
        out_bar = out_start + offset
        in_bar = in_start + math.floor(offset * fold)
        resolved_out = resolve_key_at_bar(out_profile, out_bar)
        resolved_in = resolve_key_at_bar(in_profile, in_bar)
        if resolved_out.unscored or resolved_in.unscored:
            reasons.append("a key is unknown: harmony not scored")
            return None, None, None, None, None
        assert resolved_out.key and resolved_in.key
        assert resolved_out.source and resolved_in.source
        out_sources.add(resolved_out.source)
        in_sources.add(resolved_in.source)
        compat = key_compat(resolved_out.key, resolved_in.key)
        if worst_term is None or compat.score < worst_term:
            worst_term = compat.score
            worst_out, worst_in = resolved_out.key, resolved_in.key
    if worst_out is None or worst_in is None:
        reasons.append("a key is unknown: harmony not scored")
        return None, None, None, None, None
    key_distance, key_relation, key_term = _key_term(worst_out, worst_in, reasons)
    _append_region_sources(out_profile, in_profile, out_sources, in_sources, reasons)
    return key_distance, key_relation, key_term, worst_out, worst_in


def _key_term(
    key_out: str | None, key_in: str | None, reasons: list[str]
) -> tuple[int | None, str | None, float | None]:
    """Camelot distance, the directional rule (``key_compat``) and the harmony term, noting the rule in ``reasons``.

    All three are None (unscored) when a key is unknown.
    """
    if not (key_out and key_in):
        reasons.append("a key is unknown: harmony not scored")
        return None, None, None
    key_distance = camelot_distance(CamelotKey.from_string(key_out), CamelotKey.from_string(key_in))
    compat = key_compat(key_out, key_in)
    reasons.append(f"keys {key_out} -> {key_in} {compat.relation} ({compat.interval})")
    return key_distance, compat.relation, compat.score


def score_windows(
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    out_window: MixWindow,
    in_window: MixWindow,
    *,
    overlap_bars: int | None = None,
) -> PairScore:
    # An explicit overlap is capped to both windows: bars past either window are not heard, so they cannot clash.
    bars = min(overlap_bars or DEFAULT_OVERLAP_BARS, out_window.max_bars, in_window.max_bars)
    bars = max(1, bars)
    reasons: list[str] = []
    gap = bpm_gap_pct(out_profile.judged_bpm, in_profile.judged_bpm)
    reach = beat_sync_reach_pct(in_profile)
    fold = pair_beat_sync_fold(out_profile.judged_bpm, in_profile.judged_bpm, reach)
    key_distance, key_relation, key_term, key_out, key_in = _harmony_over_overlap_span(
        out_profile,
        in_profile,
        out_window.start_bar,
        in_window.start_bar,
        bars,
        fold,
        reasons,
    )
    beatmatchable, tempo_term = _tempo_term(gap, reasons, reach)
    vocal_clash, bass_clash = _clashes(
        out_profile,
        in_profile,
        out_window.start_bar,
        in_window.start_bar,
        bars,
        fold,
    )
    vocal_term = _vocal_term(vocal_clash, bass_clash, bars, reasons)
    energy_delta = None
    if out_window.energy is not None and in_window.energy is not None:
        energy_delta = in_window.energy - out_window.energy
        reasons.append(f"relative energy {out_window.energy:.2f} -> {in_window.energy:.2f} (within each track)")
    window_term = (out_window.score + in_window.score) / 2.0
    candidates = {"harmony": key_term, "tempo": tempo_term, "vocals": vocal_term, "windows": window_term}
    measured = {name: value for name, value in candidates.items() if value is not None}
    why = {"harmony": "a key is unknown", "tempo": "a tempo is unknown", "vocals": "a vocal map is missing"}
    unscored = tuple((name, why[name]) for name, value in candidates.items() if value is None)
    return PairScore(
        score=_weighted_mean(measured),
        unscored=unscored,
        key_distance=key_distance,
        key_relation=key_relation,
        key_out=key_out,
        key_in=key_in,
        bpm_gap_pct=gap,
        beatmatchable=beatmatchable,
        vocal_clash_bars=vocal_clash,
        bass_clash_bars=bass_clash,
        energy_delta=energy_delta,
        overlap_bars=bars,
        out_window=out_window,
        in_window=in_window,
        reasons=tuple(reasons),
    )


def score_pair(
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    *,
    out_window: MixWindow | None = None,
    in_window: MixWindow | None = None,
    min_out_bar: float | None = None,
    overlap_bars: int | None = None,
) -> PairScore:
    """Score the best window pair (or the windows given), with the overlap asked for when one is."""
    outs = (
        [out_window]
        if out_window is not None
        else [
            window
            for window in mix_windows(out_profile, "out", limit=8)
            if min_out_bar is None or window.start_bar > min_out_bar
        ]
    )
    if not outs:
        raise ValueError("no outgoing window is left after the current position")
    ins = [in_window] if in_window is not None else mix_windows(in_profile, "in", limit=5)
    scored = [score_windows(out_profile, in_profile, o, i, overlap_bars=overlap_bars) for o in outs for i in ins]
    return max(scored, key=rank_key)


def segment_overlay_notes(pair: PairScore, overlay: Mapping[str, Any] | None) -> PairScore:
    """Attach phrase voice/instrument overlay reasons without a whole-track compatibility score."""
    if overlay is None:
        return pair
    extra = tuple(str(reason) for reason in overlay.get("reasons", ()))
    if not extra:
        return pair
    return PairScore(
        score=pair.score,
        key_distance=pair.key_distance,
        key_relation=pair.key_relation,
        key_out=pair.key_out,
        key_in=pair.key_in,
        bpm_gap_pct=pair.bpm_gap_pct,
        beatmatchable=pair.beatmatchable,
        vocal_clash_bars=pair.vocal_clash_bars,
        bass_clash_bars=pair.bass_clash_bars,
        energy_delta=pair.energy_delta,
        overlap_bars=pair.overlap_bars,
        out_window=pair.out_window,
        in_window=pair.in_window,
        reasons=pair.reasons + extra,
        unscored=pair.unscored,
    )
