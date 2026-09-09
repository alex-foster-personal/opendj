"""Non-lexical vocalization-run handling for lyric alignment (round 4b).

Problem (spec: specs/karaoke-lyrics-alignment.md, Round 3a Pure_Mids autopsy):
vocalization blocks ("oh ah ah", "pa papapa") match the audio badly, the CTC
path overshoots them, and monotonicity makes the overshoot permanent. Fix:
detect such runs in the REFERENCE word list, replace them with <star> wildcard
tokens during alignment so the surrounding lexical words anchor cleanly, then
linearly interpolate timings across the run from the anchored neighbours.

Pure functions, stdlib only -- this module is loaded by file path from the
standalone PEP 723 aligner scripts (scripts/spike_align_jamendo.py and its
Modal port scripts/modal_align_spike.py), so it must not import apps.* or any
third-party package. Both scripts MUST call these same functions; a second
implementation would break local-vs-Modal parity.
"""

from __future__ import annotations

import re
import statistics

STAR_TOKEN = "<star>"
MIN_RUN_LEN = 4  # shorter vocalization bursts align acceptably; runs derail

# Canonical vocalization syllables. Elongations ("ohhh", "aaah") reach these
# via letter-run collapse; chants ("papapa", "lalala") via repetition of a
# member; hyphenations ("oh-oh") via per-part checks. Deliberately excluded:
# "yeah" (distinct phonemes, aligns fine), "ja"/"a"/"ay" (lexical collisions).
VOCALIZATION_TOKENS: frozenset[str] = frozenset({
    "oh", "ah", "aw", "eh", "uh", "huh",
    "la", "na", "pa", "ba", "da", "sha", "ya",
    "ohh", "ahh", "aww", "ooh", "ahha",
    "mmh", "hmm", "mm", "hm", "mh", "m",
    "whoa", "woah", "hey", "ho", "wo",
})

#-----------------------------------------------------------------------------


def _collapse_letter_runs(part: str) -> str:
    """'ohhh' -> 'oh', 'aaah' -> 'ah', 'mmm' -> 'm'."""
    return re.sub(r"(.)\1+", r"\1", part)


def _is_repetition_of_vocalization(part: str) -> bool:
    """'papapa' == 'pa'*3, 'lalala' == 'la'*3, 'ohoh' == 'oh'*2."""
    return any(
        len(part) > len(unit) and len(part) % len(unit) == 0
        and part == unit * (len(part) // len(unit))
        for unit in VOCALIZATION_TOKENS
    )


def _is_vocalization(word: str) -> bool:
    token = "".join(ch for ch in word.lower() if ch.isalpha() or ch == "-")
    parts = [p for p in token.split("-") if p]
    if not parts:
        return False
    return all(
        part in VOCALIZATION_TOKENS
        or _collapse_letter_runs(part) in VOCALIZATION_TOKENS
        or _is_repetition_of_vocalization(part)
        or _is_repetition_of_vocalization(_collapse_letter_runs(part))
        for part in parts
    )


def detect_nonlexical_runs(words: list[str]) -> list[range]:
    """Maximal runs of >= MIN_RUN_LEN consecutive vocalization tokens.

    Returned ranges are 0-based, end-exclusive, non-overlapping, in order, and
    always separated by at least one non-vocalization word (maximality).
    """
    runs: list[range] = []
    i = 0
    while i < len(words):
        if _is_vocalization(words[i]):
            j = i
            while j < len(words) and _is_vocalization(words[j]):
                j += 1
            if j - i >= MIN_RUN_LEN:
                runs.append(range(i, j))
            i = j
        else:
            i += 1
    return runs


#-----------------------------------------------------------------------------


def replace_run_tokens_with_stars(
    tokens_starred: list[str],
    text_starred: list[str],
    n_words: int,
    star_frequency: str,
    runs: list[range],
) -> tuple[list[str], list[str]]:
    """Replace run words with <star> in preprocess_text's starred outputs.

    Must run AFTER ctc_forced_aligner.preprocess_text (a literal '<star>' in
    the input text would be normalized+romanized into the word 'star').
    postprocess_results then skips these entries, so replaced words produce no
    stamps -- interpolate_run_words fills them back in.
    """
    if star_frequency == "segment":  # ['<star>', w0, '<star>', w1, ...]
        expected_len = 2 * n_words
        positions = [2 * i + 1 for run in runs for i in run]
    elif star_frequency == "edges":  # ['<star>', w0, ..., wN, '<star>']
        expected_len = n_words + 2
        positions = [i + 1 for run in runs for i in run]
    else:
        raise ValueError(f"unknown star_frequency {star_frequency!r}")
    if len(tokens_starred) != expected_len or len(text_starred) != expected_len:
        raise ValueError(
            f"starred lists have {len(tokens_starred)}/{len(text_starred)} entries, "
            f"expected {expected_len} for {n_words} words ({star_frequency})"
        )
    if runs and not (runs[0].start >= 0 and runs[-1][-1] < n_words):
        raise ValueError(f"run indices out of range for {n_words} words: {runs}")
    tokens_out, text_out = list(tokens_starred), list(text_starred)
    for pos in positions:
        tokens_out[pos] = STAR_TOKEN
        text_out[pos] = STAR_TOKEN
    return tokens_out, text_out


def interpolate_run_words(
    words: list[str],
    kept_stamps: list[dict],
    runs: list[range],
    audio_duration_s: float,
) -> tuple[list[dict], list[str]]:
    """Rebuild the full per-word output, interpolating the starred-out runs.

    kept_stamps: aligner stamps ({'start','end','score'} seconds) for the
    non-run words only, in word order. Each run is spread linearly across
    [end of last anchored word before, start of first anchored word after]:
    n equal slots, word k = slot k. A run touching a song edge has only ONE
    anchor; it is PACKED against that anchor in a window of n * (per-song
    median aligned word duration), clamped to the audio bounds -- NOT
    stretched to the file edge, which would smear a trailing oh/ah block
    across an instrumental outro (the exact round-2 pathology). The fallback
    is named in the returned notes, never silent. A run with no anchor on
    either side is a hard error.

    Returns (words_out, notes). Interpolated entries carry
    {'interpolated': True, 'score': None}; kept entries keep aligner values.
    """
    replaced = {i for run in runs for i in run}
    if len(kept_stamps) != len(words) - len(replaced):
        raise ValueError(
            f"{len(kept_stamps)} kept stamps for {len(words)} words "
            f"with {len(replaced)} replaced -- counts must add up"
        )
    words_out: list[dict | None] = [None] * len(words)
    stamps_iter = iter(kept_stamps)
    for i, word in enumerate(words):
        if i not in replaced:
            stamp = next(stamps_iter)
            words_out[i] = {
                "word": word, "start_s": stamp["start"],
                "end_s": stamp["end"], "score": stamp["score"],
            }

    notes: list[str] = []
    for run in runs:
        span = f"words {run.start}-{run[-1]}"
        if run.start - 1 in replaced or run.stop in replaced:
            raise ValueError(f"{span}: adjacent to another run -- runs must be maximal")
        has_left = run.start > 0
        has_right = run.stop < len(words)
        if not has_left and not has_right:
            raise RuntimeError(
                f"{span}: the whole song is one non-lexical run -- no anchor on either side"
            )
        elif has_left and has_right:  # noqa: RET506 - explicit elif is the house style
            t0 = words_out[run.start - 1]["end_s"]  # type: ignore[index]
            t1 = words_out[run.stop]["start_s"]  # type: ignore[index]
            if t1 < t0:
                raise RuntimeError(f"{span}: anchors reversed ({t0:.3f}s > {t1:.3f}s)")
        else:  # song-edge run: pack against the single anchor, explicitly
            med_dur = statistics.median(s["end"] - s["start"] for s in kept_stamps)
            window = len(run) * med_dur
            if has_left:  # run ends the song
                t0 = words_out[run.start - 1]["end_s"]  # type: ignore[index]
                t1 = min(t0 + window, audio_duration_s)
                notes.append(
                    f"{span}: run touches song END -- packed after the left anchor, "
                    f"{t0:.2f}-{t1:.2f}s ({med_dur:.2f}s median word duration)"
                )
            else:  # run opens the song
                t1 = words_out[run.stop]["start_s"]  # type: ignore[index]
                t0 = max(t1 - window, 0.0)
                notes.append(
                    f"{span}: run touches song START -- packed before the right anchor, "
                    f"{t0:.2f}-{t1:.2f}s ({med_dur:.2f}s median word duration)"
                )
        slot = (t1 - t0) / len(run)
        for k, i in enumerate(run):
            words_out[i] = {
                "word": words[i],
                "start_s": t0 + k * slot,
                "end_s": t0 + (k + 1) * slot,
                "score": None,
                "interpolated": True,
            }
    assert all(w is not None for w in words_out)
    return words_out, notes  # type: ignore[return-value]
