"""Stage-3 verdict: read the DIFF SHAPE between a lyric sheet and an ASR stream.

Round-0 finding (specs/lyrics-version-check.md): raw match rate alone must
never be the verdict -- hard-for-Whisper songs (Quiero_y_Puedo) sit at 26% on
a CORRECT sheet. So the classifier reads block structure from difflib opcodes
and reserves explicit "unverifiable" and "wrong_song" verdicts, and every
finding carries its word ranges so edit-op recovery can be scored.

Finding kinds (sheet-centric):
- missing_in_audio: a sheet block the audio never sings. If its text repeats
  elsewhere in the sheet, sub-kind sheet_repeat_unsupported (the sheet claims
  an extra chorus repeat this version does not have).
- extra_in_audio: the audio sings a block the sheet lacks (dropped verse in
  the sheet, ad-libs, extra section in an extended mix).
- moved_block: a missing block and an extra block with matching text --
  structure reordered, content intact.

Round-1 calibration (see spec experiment log):
- A repeat-count dispute is UNDECIDABLE from text alone ("sheet says chorus x3,
  ASR heard x2" reads identically whether ASR missed one or the sheet invented
  one), so repeat-only findings yield verdict needs_acoustic_check -- stage 2's
  aligner confidence over the span settles it, never this module.
- A non-repeat missing block whose text fuzzily occurs in the ASR stream was
  HEARD, just garbled -- rescued (suppressed), a real dropped verse cannot be.
- extra_in_audio blocks are prob-gated when per-word ASR probabilities are
  supplied: hallucinated garble is low-prob, truly sung verses are not.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from apps.lyrics.asr_match import normalize_word

MIN_BLOCK_WORDS: int = 8  # smaller unmatched runs are ASR noise, not structure
REPEAT_SIM: float = 0.6  # similarity for "this block's text exists elsewhere"
MOVE_PAIR_SIM: float = 0.45  # looser: a move has structural evidence on both sides
HEARD_GARBLED_SIM: float = 0.5  # missing block fuzzily present in ASR = heard, not missing
EXTRA_MIN_MEAN_PROB: float = 0.5  # extra_in_audio below this mean word prob = garble
WRONG_SONG_MATCH_CEIL: float = 0.2  # both rates under this + no anchor = wrong song
MIN_ANCHOR_WORDS: int = 6  # a matched run this long anchors "right song"
UNVERIFIABLE_ASR_FRACTION: float = 0.3  # ASR heard under 30% of sheet length

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    kind: str  # missing_in_audio | sheet_repeat_unsupported | extra_in_audio | moved_block
    sheet_start: int  # word indices into the sheet; (-1, -1) for extra_in_audio
    sheet_end: int  # inclusive
    asr_start: int  # word indices into the ASR stream; (-1, -1) for missing kinds
    asr_end: int  # inclusive
    n_words: int
    text: str  # normalized text of the block (for human reading + move pairing)


@dataclass(frozen=True)
class Verdict:
    verdict: str  # matched | structure_mismatch | needs_acoustic_check | wrong_song | unverifiable
    ref_match_rate: float
    asr_match_rate: float
    longest_anchor: int  # longest exactly-matched run (words)
    findings: tuple[Finding, ...]


def classify(
    sheet_words: list[str], asr_words: list[str], asr_probs: list[float] | None = None,
) -> Verdict:
    if not sheet_words:
        raise ValueError("no sheet words")
    if not asr_words:
        raise ValueError("no ASR words")
    if asr_probs is not None and len(asr_probs) != len(asr_words):
        raise ValueError("asr_probs must align 1:1 with asr_words")
    sheet_norm = [normalize_word(w) for w in sheet_words]
    asr_norm = [normalize_word(w) for w in asr_words]
    matcher = SequenceMatcher(a=sheet_norm, b=asr_norm, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]
    n_matched = sum(b.size for b in blocks)
    ref_rate = n_matched / len(sheet_norm)
    asr_rate = n_matched / len(asr_norm)
    longest_anchor = max((b.size for b in blocks), default=0)

    missing, extra = _block_findings(matcher, sheet_norm, asr_norm, asr_probs)

    # Pairing BEFORE the garbled-rescue: a moved verse also "occurs fuzzily in
    # ASR" (at its new position) -- rescuing first would eat every move signature.
    paired = _pair_moves(missing, extra)
    findings = tuple(
        _directional_repeat(f, sheet_norm) for f in paired
        if not (f.kind == "missing_in_audio" and _occurs_fuzzily(f.text, asr_norm))
        # heard-but-garbled rescue: a truly dropped verse cannot appear in ASR at all
    )

    decisive = [f for f in findings if f.kind != "sheet_repeat_unsupported"]
    if len(asr_norm) < UNVERIFIABLE_ASR_FRACTION * len(sheet_norm):
        verdict = "unverifiable"
    elif ref_rate < WRONG_SONG_MATCH_CEIL and asr_rate < WRONG_SONG_MATCH_CEIL \
            and longest_anchor < MIN_ANCHOR_WORDS:
        verdict = "wrong_song"
    elif decisive:
        verdict = "structure_mismatch"
    elif findings:  # repeat-count disputes only: text cannot adjudicate, stage 2 must
        verdict = "needs_acoustic_check"
    else:
        verdict = "matched"
    return Verdict(verdict, ref_rate, asr_rate, longest_anchor, tuple(findings))


#-----------------------------------------------------------------------------


def _block_findings(
    matcher: SequenceMatcher,
    sheet_norm: list[str],
    asr_norm: list[str],
    asr_probs: list[float] | None,
) -> tuple[list[Finding], list[Finding]]:
    """Turn the diff opcodes into (missing-from-audio, extra-in-audio) blocks.

    Blocks shorter than MIN_BLOCK_WORDS are noise. An extra block whose ASR
    words average below EXTRA_MIN_MEAN_PROB is garble the recognizer invented,
    not a sung block the sheet lacks, so it is dropped rather than reported.
    """
    missing: list[Finding] = []
    extra: list[Finding] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace") and (i2 - i1) >= MIN_BLOCK_WORDS:
            text = " ".join(sheet_norm[i1:i2])
            kind = (
                "sheet_repeat_unsupported"
                if _text_occurs_elsewhere(text, sheet_norm, exclude=(i1, i2))
                else "missing_in_audio"
            )
            missing.append(Finding(kind, i1, i2 - 1, -1, -1, i2 - i1, text))
        if tag in ("insert", "replace") and (j2 - j1) >= MIN_BLOCK_WORDS:
            if asr_probs is not None and sum(asr_probs[j1:j2]) / (j2 - j1) < EXTRA_MIN_MEAN_PROB:
                continue  # low-confidence garble, not a sung block the sheet lacks
            extra.append(
                Finding(
                    "extra_in_audio", -1, -1, j1, j2 - 1, j2 - j1, " ".join(asr_norm[j1:j2])
                )
            )
    return missing, extra


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(a=a.split(), b=b.split(), autojunk=False).ratio()


def _text_occurs_elsewhere(text: str, sheet_norm: list[str], exclude: tuple[int, int]) -> bool:
    """Does this block's text appear (approximately) in the sheet OUTSIDE the
    excluded range? Windows of the block's own length, stepped by half-lengths."""
    words = text.split()
    n = len(words)
    step = max(1, n // 2)
    for start in range(0, len(sheet_norm) - n + 1, step):
        if exclude[0] <= start < exclude[1] or exclude[0] < start + n <= exclude[1]:
            continue
        if _similarity(text, " ".join(sheet_norm[start : start + n])) >= REPEAT_SIM:
            return True
    return False


def _directional_repeat(f: Finding, sheet_norm: list[str]) -> Finding:
    """extra_in_audio whose text matches sheet content elsewhere = the audio
    sings a repeat the sheet does not list (radio sheet, extended audio).
    Directional mirror of sheet_repeat_unsupported; ASR positively heard it
    (prob-gated), so it stays decisive rather than needs-acoustic-check."""
    if f.kind != "extra_in_audio":
        return f
    words = f.text.split()
    step = max(1, len(words) // 2)
    for start in range(0, max(1, len(sheet_norm) - len(words) + 1), step):
        if _similarity(f.text, " ".join(sheet_norm[start : start + len(words)])) >= REPEAT_SIM:
            return Finding("audio_repeat_unlisted", f.sheet_start, f.sheet_end,
                           f.asr_start, f.asr_end, f.n_words, f.text)
    return f


def _occurs_fuzzily(text: str, stream_norm: list[str]) -> bool:
    """Does this block's text approximately appear anywhere in the stream?"""
    words = text.split()
    n = len(words)
    step = max(1, n // 2)
    return any(
        _similarity(text, " ".join(stream_norm[start : start + n])) >= HEARD_GARBLED_SIM
        for start in range(0, max(1, len(stream_norm) - n + 1), step)
    )


def _pair_moves(missing: list[Finding], extra: list[Finding]) -> tuple[Finding, ...]:
    """A missing block and an extra block with matching text = one moved block."""
    used_extra: set[int] = set()
    out: list[Finding] = []
    for m in missing:
        paired = None
        for k, e in enumerate(extra):
            if k in used_extra:
                continue
            if _similarity(m.text, e.text) >= MOVE_PAIR_SIM:
                paired = k
                break
        if paired is not None:
            used_extra.add(paired)
            e = extra[paired]
            out.append(Finding("moved_block", m.sheet_start, m.sheet_end,
                               e.asr_start, e.asr_end, m.n_words, m.text))
        else:
            out.append(m)
    out.extend(e for k, e in enumerate(extra) if k not in used_extra)
    return tuple(out)
