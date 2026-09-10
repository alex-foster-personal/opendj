"""Stage-2 arbitration features: aligner behavior over a finding's sheet span.

Round 2 (specs/lyrics-version-check.md): text alone cannot adjudicate
missing-kind findings, so the case's SHEET is force-aligned to the stem and
the aligner's own behavior over the finding's span testifies. A sheet block
the audio truly lacks starves the aligner there (bad scores, words crammed or
stretched); a block ASR merely failed to hear aligns healthily.

Decision rules were CALIBRATED (scripts/calibrate_arbitration.py, Thu 28 Aug
2026, 148 REAL phantom repeats vs 46 FALSE none-case findings):
- repeat findings (a twin occurrence exists in the sheet): REAL if the score
  asymmetry between the two occurrences exceeds 1.5 -- a phantom starves one
  twin while the other aligns healthily; an ASR-missed repeat is healthy on
  both. Balanced acc 79.1% (TPR 66.9%, TNR 91.3%). OR-combos with score_delta
  measured and rejected (no gain over spread alone).
- no-twin missing findings: REAL if score_delta < -1.9. TNR measured against
  none-case false alarms (80%); TPR is PROVISIONAL -- the synthetic set holds
  no real unique-verse drops (that needs track B / audio edits).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from difflib import SequenceMatcher

REPEAT_SPREAD_THRESHOLD: float = 1.5
MISSING_SCORE_DELTA_THRESHOLD: float = -1.9
TWIN_SIM: float = 0.6

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class SpanFeatures:
    n_words: int
    span_mean_score: float  # aligner log-prob, higher (closer to 0) = confident
    rest_median_score: float
    score_delta: float  # span_mean - rest_median; strongly negative = span worse
    span_wps: float  # words per second of SUNG time inside the span
    rest_wps: float
    wps_ratio: float  # span_wps / rest_wps; far from 1 = crammed or stretched
    span_duration_s: float


def extract_span_features(aligned_words: list[dict], lo: int, hi: int) -> SpanFeatures:
    """Features for sheet-word span [lo, hi] inclusive vs the rest of the song.

    aligned_words follows the aligner contract: [{word, start_s, end_s, score}]
    covering EVERY sheet word in order (forced alignment).
    """
    if not (0 <= lo <= hi < len(aligned_words)):
        raise ValueError(f"span [{lo}, {hi}] out of range for {len(aligned_words)} words")
    span = aligned_words[lo : hi + 1]
    rest = aligned_words[:lo] + aligned_words[hi + 1 :]
    if not rest:
        raise ValueError("span covers the whole song -- nothing to compare against")

    def _sung_seconds(words: list[dict]) -> float:
        total = sum(w["end_s"] - w["start_s"] for w in words)
        return max(total, 1e-3)  # a fully collapsed span still yields finite wps

    span_wps = len(span) / _sung_seconds(span)
    rest_wps = len(rest) / _sung_seconds(rest)
    span_mean = sum(w["score"] for w in span) / len(span)
    rest_median = statistics.median(w["score"] for w in rest)
    return SpanFeatures(
        n_words=len(span),
        span_mean_score=span_mean,
        rest_median_score=rest_median,
        score_delta=span_mean - rest_median,
        span_wps=span_wps,
        rest_wps=rest_wps,
        wps_ratio=span_wps / rest_wps,
        span_duration_s=span[-1]["end_s"] - span[0]["start_s"],
    )


def find_twin_occurrence(sheet_norm: list[str], lo: int, hi: int) -> tuple[int, int] | None:
    """Best same-length window elsewhere in the sheet matching span [lo, hi]
    (sim >= TWIN_SIM): the repeat's twin. None when nothing matches."""
    n = hi - lo + 1
    span_text = sheet_norm[lo : hi + 1]
    best, best_sim = None, TWIN_SIM
    for start in range(len(sheet_norm) - n + 1):
        if abs(start - lo) < n:  # overlapping = same occurrence
            continue
        sim = SequenceMatcher(a=span_text, b=sheet_norm[start : start + n], autojunk=False).ratio()
        if sim >= best_sim:
            best, best_sim = (start, start + n - 1), sim
    return best


def arbitrated_verdict(v, aligned_words: list[dict], sheet_norm: list[str]):
    """Round-2 verdict: missing-kind findings stand or fall by the aligner's
    testimony; confirmed repeats become decisive, cleared findings vanish, so
    needs_acoustic_check disappears. unverifiable/wrong_song pass through.
    Returns a new Verdict (import deferred to avoid a module cycle)."""
    from apps.lyrics.verdict import Verdict

    if v.verdict in ("unverifiable", "wrong_song"):
        return v
    kept = tuple(
        f for f in v.findings
        if f.kind not in ("missing_in_audio", "sheet_repeat_unsupported")
        or arbitrate_missing_finding(aligned_words, sheet_norm, f.sheet_start, f.sheet_end)
    )
    verdict = "structure_mismatch" if kept else "matched"
    return Verdict(verdict, v.ref_match_rate, v.asr_match_rate, v.longest_anchor, kept)


def arbitrate_missing_finding(
    aligned_words: list[dict], sheet_norm: list[str], lo: int, hi: int
) -> bool:
    """True = the finding is REAL (the audio genuinely lacks this sheet block).

    Calibrated rules per the module docstring; aligned_words is the CASE
    SHEET's forced alignment against the stem.
    """
    feats = extract_span_features(aligned_words, lo, hi)
    twin = find_twin_occurrence(sheet_norm, lo, hi)
    if twin is not None:
        twin_feats = extract_span_features(aligned_words, twin[0], twin[1])
        spread = abs(feats.span_mean_score - twin_feats.span_mean_score)
        return spread > REPEAT_SPREAD_THRESHOLD
    return feats.score_delta < MISSING_SCORE_DELTA_THRESHOLD
