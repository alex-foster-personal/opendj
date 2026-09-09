"""Cross-check forced-alignment word times against an independent ASR transcript.

Round 4a core, extended in round 5. The forced aligner grades its own work
(Round 3a: weak per-word, meaningless once the path detaches); Whisper on the
same vocal stem is the independent witness. Three products:

- VERSION CHECK: token-stream similarity between lyric text and ASR text
  (crosscheck). Low similarity = the fetched lyrics do not match what is sung
  (wrong version/edit, remix cut, or empty vocal).
- PER-WORD FLAGS: lyric words whose aligner time disagrees with the matched
  ASR word's time by more than a threshold, plus lyric words the ASR never
  heard at all (flagged_indices). This is the round-4a triage signal.
- WITNESS VERDICTS: the 6-class per-word verdicts the listen page's dot
  colors are painted from (witness_verdicts, round 5): LOCAL-FIRST matching
  (unique same-token ASR occurrence within a time window) with the global
  diff as fallback, because chorus repeats bend difflib's brackets and orphan
  verbatim-transcribed verses. Calibrated against Jamendo ground truth via
  `python -m apps.lyrics witness-eval`; numbers in the spec's round-5 table.

Diff matching is difflib.SequenceMatcher over NORMALIZED token streams with
autojunk OFF -- repeated tokens ("pa pa pa", chorus lines) are exactly the
hard case, and autojunk silently discards frequent tokens.

Honest denominators: lyric tokens that normalize to nothing (bare punctuation)
are "unmatchable" and excluded from match_ratio's denominator, reported
separately, never flagged.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import median
from typing import Any, Literal

_EDGE_JUNK = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)

# WITNESS verdict thresholds -- the ONE canonical home (the manifest generator and the listen
# page both mirror these; the page refuses manifests computed under different values).
# agree/red tuned in round 4a on Jamendo ground truth; the local window added in round 5 after
# the Jamming verse false-red autopsy: difflib's global diff orphaned a verbatim-transcribed
# verse because chorus repeats bent its recursion brackets, so per-word evidence now comes from
# a time-window search FIRST and the global diff is only a fallback.
WITNESS_AGREE_S: float = 0.5
WITNESS_RED_DELTA_S: float = 1.0
WITNESS_MIN_RUN: int = 5
# 5.0 won the round-5 A/B (2/3/5s tried): the uniqueness gate makes a WIDER window stricter,
# so 5s had the best red recall (72.6%) and a green error rate at baseline (0.9% vs 0.8%).
WITNESS_LOCAL_WINDOW_S: float = 5.0
# The suspect classes. Round-5 calibration: a word in one of these carries ~4.5x
# the base error rate. Import this everywhere red is decided (store consumers,
# line quality, UI tinting via the API) -- never re-declare the pair.
WITNESS_RED_CLASSES: frozenset[str] = frozenset({"contradict", "lost"})

#-----------------------------------------------------------------------------


def normalize_token(token: str) -> str:
    """Casefold, unify curly apostrophes, strip non-word edges ('Hey,' -> 'hey')."""
    # RUF001 is suppressed below: the curly apostrophe is the INPUT being
    # normalized away, not a typo in this source file.
    return _EDGE_JUNK.sub("", token.casefold().replace("’", "'"))  # noqa: RUF001


@dataclass(frozen=True)
class WordVerdict:
    index: int
    word: str
    status: Literal["matched", "unmatched", "unmatchable"]
    aligner_start_s: float
    asr_start_s: float | None
    delta_s: float | None  # aligner - asr; positive = aligner late


@dataclass(frozen=True)
class CrossCheckReport:
    verdicts: list[WordVerdict]
    n_matchable: int
    n_matched: int
    n_unmatchable: int
    match_ratio: float  # matched / matchable
    version_similarity: float  # SequenceMatcher ratio over normalized streams
    median_abs_delta_s: float | None  # over matched words; None if none matched


def crosscheck(
    lyric_words: list[str],
    aligner_starts_s: list[float],
    asr_words: list[dict[str, Any]],
) -> CrossCheckReport:
    """Diff lyric tokens against ASR tokens and time-compare the matches.

    asr_words follow scripts/modal_asr_spike.py's contract:
    [{word, start_s, end_s, prob}].
    """
    if len(lyric_words) != len(aligner_starts_s):
        raise ValueError(
            f"lyric_words ({len(lyric_words)}) and aligner_starts_s "
            f"({len(aligner_starts_s)}) must be 1:1"
        )
    if not lyric_words:
        raise ValueError("no lyric words to cross-check")

    lyric_norm_all = [normalize_token(w) for w in lyric_words]
    lyric_idx = [i for i, n in enumerate(lyric_norm_all) if n]
    lyric_norm = [lyric_norm_all[i] for i in lyric_idx]
    asr_norm_all = [normalize_token(w["word"]) for w in asr_words]
    asr_idx = [i for i, n in enumerate(asr_norm_all) if n]
    asr_norm = [asr_norm_all[i] for i in asr_idx]

    matcher = SequenceMatcher(a=lyric_norm, b=asr_norm, autojunk=False)
    matched_asr_for_lyric: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            matched_asr_for_lyric[lyric_idx[block.a + k]] = asr_idx[block.b + k]

    verdicts: list[WordVerdict] = []
    deltas: list[float] = []
    for i, word in enumerate(lyric_words):
        if not lyric_norm_all[i]:
            verdicts.append(WordVerdict(i, word, "unmatchable", aligner_starts_s[i], None, None))
        elif i in matched_asr_for_lyric:
            asr_start = float(asr_words[matched_asr_for_lyric[i]]["start_s"])
            delta = aligner_starts_s[i] - asr_start
            deltas.append(abs(delta))
            verdicts.append(WordVerdict(i, word, "matched", aligner_starts_s[i], asr_start, delta))
        else:
            verdicts.append(WordVerdict(i, word, "unmatched", aligner_starts_s[i], None, None))

    n_matchable = len(lyric_idx)
    n_matched = len(matched_asr_for_lyric)
    return CrossCheckReport(
        verdicts=verdicts,
        n_matchable=n_matchable,
        n_matched=n_matched,
        n_unmatchable=len(lyric_words) - n_matchable,
        match_ratio=(n_matched / n_matchable) if n_matchable else 0.0,
        version_similarity=matcher.ratio(),
        median_abs_delta_s=median(deltas) if deltas else None,
    )


WitnessClass = Literal["agree", "drift", "contradict", "lost", "unheard", "unmatchable"]


@dataclass(frozen=True)
class WitnessVerdict:
    index: int
    word: str
    verdict: WitnessClass
    delta_s: float | None  # aligner - asr for the evidence used; None when no ASR word matched
    source: Literal["local", "diff", "none"]  # which matcher produced the evidence


def _nearest_local_delta(
    occurrences: dict[str, list[float]], norm: str, t: float, local_window_s: float
) -> float | None:
    """Delta to the UNIQUE in-window ASR occurrence of this token, else None.

    Uniqueness is the ambiguity gate (round-5 A/B): without it, dense repeated tokens
    ("jammin'" x50, "la") always find SOME occurrence nearby, laundering genuinely detached
    words into agree -- measured as green error 0.8% -> 2.1% and red recall 76.6% -> 60.3%.
    A token that occurs twice inside the window proves nothing about WHICH one the aligner
    meant, so ambiguous evidence falls through to the global diff instead.
    """
    starts = occurrences.get(norm)
    if starts is None:
        return None
    lo = bisect_left(starts, t - local_window_s)
    hi = bisect_left(starts, t + local_window_s)
    in_window = starts[lo:hi]
    if len(in_window) != 1:
        return None
    return t - in_window[0]


def _witness_class(delta: float) -> WitnessClass:
    """Band a corroborating delta: agree, drift, or contradict."""
    if abs(delta) <= WITNESS_AGREE_S:
        return "agree"
    elif abs(delta) <= WITNESS_RED_DELTA_S:  # noqa: RET505 - explicit elif is the house style
        return "drift"
    else:
        return "contradict"


def witness_verdicts(
    lyric_words: list[str],
    aligner_starts_s: list[float],
    asr_words: list[dict[str, Any]],
    local_window_s: float = WITNESS_LOCAL_WINDOW_S,
) -> list[WitnessVerdict]:
    """Per-word witness classes for display/QC, from local-first evidence.

    Round-5 matching order (each lyric word, in isolation):
    1. LOCAL: the nearest ASR occurrence of the SAME normalized token within
       +-local_window_s of the aligner's predicted time. Occurrence identity is
       irrelevant for onset correctness -- if anyone sings this token where the
       aligner put it, the timestamp is corroborated -- and this is immune to
       the global diff's bracket collapse under chorus repetition (the Jamming
       verse false-red class: verbatim ASR words 0.3s away left "unmatched").
       local_window_s=0 disables local matching (round-4 behavior, for A/B).
    2. DIFF fallback: the round-4a global SequenceMatcher match, which still
       catches uniformly-shifted regions whose delta exceeds the local window.
    3. Neither: unheard (lone) or lost (inside a run of >= WITNESS_MIN_RUN
       consecutive evidence-free words; unmatchable tokens break runs).

    Classes: agree (|delta| <= WITNESS_AGREE_S), drift (<= WITNESS_RED_DELTA_S),
    contradict (beyond), lost, unheard, unmatchable. Red = contradict | lost.
    """
    report = crosscheck(lyric_words, aligner_starts_s, asr_words)

    occurrences: dict[str, list[float]] = {}
    for asr_word in asr_words:
        norm = normalize_token(asr_word["word"])
        if norm:
            occurrences.setdefault(norm, []).append(float(asr_word["start_s"]))
    for starts in occurrences.values():
        starts.sort()

    verdicts: list[WitnessVerdict] = []
    run: list[int] = []  # indices awaiting lost-vs-unheard, exactly flagged_indices's run rule

    def _close_run() -> None:
        nonlocal run
        verdict: WitnessClass = "lost" if len(run) >= WITNESS_MIN_RUN else "unheard"
        verdicts.extend(
            WitnessVerdict(i, lyric_words[i], verdict, None, "none") for i in run
        )
        run = []

    for v in report.verdicts:
        norm = normalize_token(v.word)
        if v.status == "unmatchable":
            _close_run()
            verdicts.append(WitnessVerdict(v.index, v.word, "unmatchable", None, "none"))
            continue
        local_delta = _nearest_local_delta(
            occurrences, norm, v.aligner_start_s, local_window_s
        )
        if local_delta is not None:
            _close_run()
            verdicts.append(
                WitnessVerdict(v.index, v.word, _witness_class(local_delta), local_delta, "local")
            )
        elif v.status == "matched":
            assert v.delta_s is not None
            _close_run()
            verdicts.append(
                WitnessVerdict(v.index, v.word, _witness_class(v.delta_s), v.delta_s, "diff")
            )
        else:
            run.append(v.index)
    _close_run()

    verdicts.sort(key=lambda w: w.index)
    if [w.index for w in verdicts] != list(range(len(lyric_words))):
        raise AssertionError("witness_verdicts lost or duplicated a word index")
    return verdicts


def flagged_indices(
    report: CrossCheckReport, delta_threshold_s: float, min_unmatched_run: int = 5
) -> set[int]:
    """Lyric-word indices the cross-check distrusts (Round 4a tuned rule):

    - matched with |aligner - asr| over delta_threshold_s, always;
    - unmatched words only in RUNS of >= min_unmatched_run consecutive
      unmatched (a lone unmatched word is usually the ASR mishearing a word in
      a fine alignment; a run is a region the ASR never heard = a lost region).
      min_unmatched_run=0 disables unmatched flagging entirely.

    Unmatchable tokens are never flagged and BREAK an unmatched run (they carry
    no evidence either way). Measured on Jamendo R2 vs ground truth
    (Fri 28 Aug 2026): delta>1.0 alone = 38% precision / 39% recall / 4.5x
    lift; delta>1.0 + run>=5 = 29% precision / 79% recall / 3.5x lift
    (self-score baseline: 3.2x lift at 31.6% recall).
    """
    flagged: set[int] = set()
    run: list[int] = []

    def _close_run() -> None:
        nonlocal run
        if min_unmatched_run > 0 and len(run) >= min_unmatched_run:
            flagged.update(run)
        run = []

    for v in report.verdicts:
        if v.status == "unmatched":
            run.append(v.index)
        elif v.status == "matched":
            _close_run()
            assert v.delta_s is not None
            if abs(v.delta_s) > delta_threshold_s:
                flagged.add(v.index)
        elif v.status == "unmatchable":
            _close_run()
        else:
            raise AssertionError(f"unhandled status {v.status!r}")
    _close_run()
    return flagged
