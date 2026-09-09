"""Line derivation + per-line quality. THE canonical grouping.

Words arrive flat with a ``line_final`` flag. Every surface that renders
LINES (deck one-liner, stage view, library tooltip, waveform second-row
packing) derives them HERE, server-side, so the deck's "conditional rules
based on line quality" and the tooltip's paragraph breaks can never disagree
between clients (agent-native parity: what the UI shows is what the endpoint
returns).

The input row is named STRUCTURALLY (:class:`WordRow`), not imported from a
storage module: under the CloudSync landing design (D13.2) word timings live
in the per-track ``karaoke_words`` artifact rather than a ``lyric_word``
table, so grouping must not be coupled to whichever reader produced the
words.

Paragraph breaks are NOT stored - public lyric text loses stanza structure
through the candidate pipeline - so they are derived from performance gaps:
a line followed by silence of at least PARAGRAPH_GAP_S before the next sung
word ends a paragraph. That is a presentational rule, not a claim about the
writer's stanzas; the constant lives here so the rule has one home.

Line quality is the share of the line's witness-judged words that are NOT
red (contradict|lost, apps/lyrics/crosscheck.WITNESS_RED_CLASSES - the 4.5x
error classes from round-5 calibration). Words with no witness verdict do
not count in the denominator (honest denominators: unjudged is not good).
Quality is None when NO word in the line was judged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from apps.lyrics.crosscheck import WITNESS_RED_CLASSES


@runtime_checkable
class WordRow(Protocol):
    """One aligned word, however it was read (artifact cache, DB row, test double)."""

    idx: int
    word: str
    start_s: float | None
    end_s: float | None
    witness: str | None
    line_final: bool


PARAGRAPH_GAP_S: float = 2.5

# Display bands for per-line quality, consumed by the deck line view's
# conditional rendering. good >= GOOD_MIN, bad < UNCERTAIN_MIN, else
# uncertain. None (no judged words) is its own band: 'unjudged'.
LINE_QUALITY_GOOD_MIN: float = 0.8
LINE_QUALITY_UNCERTAIN_MIN: float = 0.5

LineBand = str  # 'good' | 'uncertain' | 'bad' | 'unjudged'


@dataclass(frozen=True)
class LyricLine:
    first_idx: int
    last_idx: int
    text: str
    start_s: float | None
    end_s: float | None
    n_words: int
    n_red: int
    n_judged: int
    quality: float | None
    band: LineBand
    para_final: bool


def _band(quality: float | None) -> LineBand:
    if quality is None:
        return "unjudged"
    elif quality >= LINE_QUALITY_GOOD_MIN:  # noqa: RET505 - explicit elif is the house style
        return "good"
    elif quality >= LINE_QUALITY_UNCERTAIN_MIN:
        return "uncertain"
    else:
        return "bad"


def derive_lines(words: Sequence[WordRow]) -> list[LyricLine]:
    """Group flat words into lines on ``line_final``; trailing words with no
    closing flag still form a final line (real ingests end mid-line)."""
    lines: list[LyricLine] = []
    bucket: list[WordRow] = []
    for word in words:
        bucket.append(word)
        if word.line_final:
            lines.append(_close(bucket))
            bucket = []
    if bucket:
        lines.append(_close(bucket))
    return _stamp_paragraphs(lines)


def _close(bucket: Sequence[WordRow]) -> LyricLine:
    timed = [w for w in bucket if w.start_s is not None]
    judged = [w for w in bucket if w.witness is not None]
    n_red = sum(1 for w in judged if w.witness in WITNESS_RED_CLASSES)
    quality = (1.0 - n_red / len(judged)) if judged else None
    ends = [w.end_s for w in bucket if w.end_s is not None]
    return LyricLine(
        first_idx=bucket[0].idx,
        last_idx=bucket[-1].idx,
        text=" ".join(w.word for w in bucket),
        start_s=min(w.start_s for w in timed) if timed else None,
        end_s=max(ends) if ends else None,
        n_words=len(bucket),
        n_red=n_red,
        n_judged=len(judged),
        quality=quality,
        band=_band(quality),
        para_final=False,  # stamped in a second pass, needs the NEXT line
    )


def _stamp_paragraphs(lines: list[LyricLine]) -> list[LyricLine]:
    """A line ends a paragraph when the gap to the next line's first sung
    word is at least PARAGRAPH_GAP_S. The last line always closes one."""
    out: list[LyricLine] = []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if nxt is None:
            para = True
        elif line.end_s is not None and nxt.start_s is not None:
            para = (nxt.start_s - line.end_s) >= PARAGRAPH_GAP_S
        else:
            para = False  # no timing evidence: never invent a stanza break
        out.append(
            LyricLine(**{**line.__dict__, "para_final": para})
        )
    return out
