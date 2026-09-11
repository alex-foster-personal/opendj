"""Stage-3 text matching: ASR word stream vs a reference lyric sheet, NO time.

The version check runs on text alone (specs/lyrics-version-check.md): difflib
sequence alignment over normalized tokens. Round 0 uses it with the TRUE sheet
to measure the ASR leg's ceiling per language; later rounds feed perturbed
sheets and read the diff shape as the version verdict.

Match rates are directional and both denominators are named:
- ref_match_rate: matched ref tokens / n_ref ("how much of the sheet did ASR
  support, in order") -- low = sheet holds words that were never heard.
- asr_match_rate: matched asr tokens / n_asr -- low = ASR heard words the
  sheet does not have (extra section, ad-libs, wrong song).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

_EDGE_PUNCT = re.compile(r"^\W+|\W+$", re.UNICODE)

#-----------------------------------------------------------------------------


def normalize_word(word: str) -> str:
    """Casefold and strip surrounding punctuation; internal punctuation stays
    (French elisions like l'hiver are single tokens in both streams)."""
    return _EDGE_PUNCT.sub("", word.casefold())


@dataclass(frozen=True)
class MatchReport:
    n_ref: int
    n_asr: int
    n_matched: int
    ref_match_rate: float
    asr_match_rate: float
    longest_ref_gap: int  # longest run of consecutive unmatched ref tokens
    longest_asr_gap: int


def match_words(ref_words: list[str], asr_words: list[str]) -> MatchReport:
    if not ref_words:
        raise ValueError("no reference words")
    if not asr_words:
        raise ValueError("no ASR words")
    ref_norm = [normalize_word(w) for w in ref_words]
    asr_norm = [normalize_word(w) for w in asr_words]
    matcher = SequenceMatcher(a=ref_norm, b=asr_norm, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]
    n_matched = sum(b.size for b in blocks)

    ref_matched = [False] * len(ref_norm)
    asr_matched = [False] * len(asr_norm)
    for b in blocks:
        for k in range(b.size):
            ref_matched[b.a + k] = True
            asr_matched[b.b + k] = True
    return MatchReport(
        n_ref=len(ref_norm),
        n_asr=len(asr_norm),
        n_matched=n_matched,
        ref_match_rate=n_matched / len(ref_norm),
        asr_match_rate=n_matched / len(asr_norm),
        longest_ref_gap=_longest_false_run(ref_matched),
        longest_asr_gap=_longest_false_run(asr_matched),
    )


def format_match_report(label: str, r: MatchReport) -> str:
    return (
        f"{label:<44s} ref n={r.n_ref:>5d}  asr n={r.n_asr:>5d}  "
        f"ref-match {r.ref_match_rate:6.1%}  asr-match {r.asr_match_rate:6.1%}  "
        f"gaps ref={r.longest_ref_gap:>4d} asr={r.longest_asr_gap:>4d}"
    )


#-----------------------------------------------------------------------------


def _longest_false_run(flags: list[bool]) -> int:
    longest = run = 0
    for f in flags:
        run = run + 1 if not f else 0
        longest = max(longest, run)
    return longest
