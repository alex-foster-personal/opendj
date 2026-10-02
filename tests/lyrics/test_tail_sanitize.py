"""Tail sanitizer for phantom karaoke words (LYR-08, issue #3995).

[if] produce writes track words [then] the Whisper silence hallucination and
past-duration words drop, real lyrics inside the duration stay, [else stop].
"""
from __future__ import annotations

import pytest

from apps.lyrics.tail_sanitize import HALLUCINATION_GAP_S, sanitize_words_for_artifact

from .conftest import word

pytestmark = pytest.mark.requirement("LYR-08")


def _texts(kept: list) -> list[str]:
    return [str(w["word"]) for w in kept]


# --- removal direction -------------------------------------------------------


def test_thank_you_tail_after_silence_with_weak_witness_dropped() -> None:
    """[if] weak thank-you after a long silence past the last line [then] dropped [else stop]."""
    words = [
        word("sing", start_s=1.0, end_s=1.5, witness="agree", line_final=True),
        word("thank", start_s=200.0, end_s=200.5, witness="contradict"),
        word("you", start_s=200.6, end_s=201.0, witness="lost"),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=210.0)
    assert _texts(kept) == ["sing"]
    assert [(i, r) for i, _w, r in report.dropped] == [
        (1, "post_final_hallucination"),
        (2, "post_final_hallucination"),
    ]


def test_repeated_thanks_for_watching_tail_dropped() -> None:
    """[if] repeated hallucination phrases with evidence [then] whole span dropped [else stop]."""
    words = [
        word("home", start_s=50.0, end_s=50.5, witness="agree", line_final=True),
        word("Thanks", start_s=60.0, end_s=60.3, witness="unheard"),
        word("for", start_s=60.3, end_s=60.5, witness="unheard"),
        word("watching!", start_s=60.5, end_s=61.0, witness="unheard"),
        word("Thank", start_s=62.0, end_s=62.3, witness="lost"),
        word("you.", start_s=62.3, end_s=62.6, witness="lost"),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=120.0)
    assert _texts(kept) == ["home"]
    assert len(report.dropped) == 5


def test_word_starting_after_duration_dropped() -> None:
    """[if] word starts after the track ends [then] dropped as past_duration [else stop]."""
    words = [
        word("end", start_s=98.0, end_s=98.5, witness="agree", line_final=True),
        word("ghost", start_s=100.4, end_s=100.9, witness="agree"),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert _texts(kept) == ["end"]
    assert report.dropped == ((1, "ghost", "past_duration"),)


def test_word_starting_exactly_at_duration_dropped() -> None:
    """[if] word starts at the duration [then] no audio under it, dropped [else stop]."""
    words = [
        word("end", start_s=98.0, end_s=98.5, witness="agree", line_final=True),
        word("ghost", start_s=100.0, end_s=100.2, witness="agree", line_final=True),
    ]
    kept, _report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert _texts(kept) == ["end"]


# --- keep direction ----------------------------------------------------------


def test_real_word_in_final_quarter_second_kept() -> None:
    """[if] real word in the last 0.25 s inside the duration [then] kept [else stop]."""
    words = [
        word("end", start_s=98.0, end_s=98.5, witness="agree", line_final=False),
        word("now", start_s=99.9, end_s=100.0, witness="agree", line_final=True),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert _texts(kept) == ["end", "now"]
    assert report.dropped == ()


def test_weak_witness_word_inside_duration_without_phrase_kept() -> None:
    """[if] weak non-phrase word late inside the duration [then] kept [else stop]."""
    words = [
        word("end", start_s=10.0, end_s=10.5, witness="agree", line_final=True),
        word("ghost", start_s=99.9, end_s=100.0, witness="contradict"),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert _texts(kept) == ["end", "ghost"]
    assert report.dropped == ()


def test_repeated_chorus_outro_after_last_line_kept() -> None:
    """[if] long repetitive outro inside the duration [then] every word kept [else stop]."""
    words = [word("verse", start_s=1.0, end_s=1.5, witness="agree", line_final=True)]
    t = 2.0
    for i in range(30):
        text = "oh" if i % 3 else "baby"
        witness = "agree" if i % 2 else "contradict"
        words.append(word(text, start_s=t, end_s=t + 0.3, witness=witness))
        t += 0.4
    kept, report = sanitize_words_for_artifact(words, duration_s=60.0)
    assert len(kept) == len(words)
    assert report.dropped == ()


def test_sung_thank_you_chorus_repeated_at_end_kept() -> None:
    """[if] a sung "thank you" chorus repeats at the end with agree witness [then] kept [else stop]."""
    words = [word("so", start_s=1.0, end_s=1.2, witness="agree", line_final=True)]
    t = 10.0
    for _ in range(6):
        words.append(word("thank", start_s=t, end_s=t + 0.3, witness="agree"))
        words.append(word("you", start_s=t + 0.3, end_s=t + 0.6, witness="agree"))
        t += 1.0
    kept, report = sanitize_words_for_artifact(words, duration_s=30.0)
    assert len(kept) == len(words)
    assert report.dropped == ()


def test_weak_thank_you_without_silence_gap_kept() -> None:
    """[if] weak thank-you directly after the last line, no silence [then] kept [else stop]."""
    words = [
        word("sing", start_s=1.0, end_s=1.5, witness="agree", line_final=True),
        word("thank", start_s=1.6, end_s=1.9, witness="contradict"),
        word("you", start_s=1.9, end_s=2.2, witness="contradict"),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert _texts(kept) == ["sing", "thank", "you"]
    assert report.dropped == ()


def test_gap_threshold_boundary() -> None:
    """[if] gap just under the threshold [then] kept; at the threshold [then] dropped [else stop]."""
    def _case(gap: float) -> list[str]:
        words = [
            word("sing", start_s=1.0, end_s=2.0, witness="agree", line_final=True),
            word("thanks", start_s=2.0 + gap, end_s=2.5 + gap, witness="lost"),
        ]
        kept, _ = sanitize_words_for_artifact(words, duration_s=100.0)
        return _texts(kept)

    assert _case(HALLUCINATION_GAP_S - 0.01) == ["sing", "thanks"]
    assert _case(HALLUCINATION_GAP_S) == ["sing"]


def test_unmeasurable_gap_kept() -> None:
    """[if] the gap has a missing timestamp [then] cannot measure, kept [else stop]."""
    words = [
        word("sing", start_s=None, end_s=None, witness="agree", line_final=True),
        word("thank", start_s=200.0, end_s=200.5, witness="contradict"),
        word("you", start_s=200.6, end_s=201.0, witness="lost"),
    ]
    kept, _report = sanitize_words_for_artifact(words, duration_s=210.0)
    assert _texts(kept) == ["sing", "thank", "you"]


def test_bare_you_tail_is_not_a_phrase_kept() -> None:
    """[if] a lone weak "you" after silence [then] not a known phrase, kept [else stop]."""
    words = [
        word("sing", start_s=1.0, end_s=1.5, witness="agree", line_final=True),
        word("you", start_s=20.0, end_s=20.5, witness="lost"),
    ]
    kept, _report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert _texts(kept) == ["sing", "you"]


def test_mixed_tail_only_phrase_suffix_dropped() -> None:
    """[if] weak lyric then silence then weak thank-you [then] only the phrase drops [else stop]."""
    words = [
        word("sing", start_s=1.0, end_s=1.5, witness="agree", line_final=True),
        word("hey", start_s=2.0, end_s=2.3, witness="contradict"),
        word("thank", start_s=40.0, end_s=40.3, witness="contradict"),
        word("you", start_s=40.3, end_s=40.6, witness="contradict"),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert _texts(kept) == ["sing", "hey"]
    assert [i for i, _w, _r in report.dropped] == [2, 3]


def test_mid_track_thank_you_with_agree_not_dropped() -> None:
    """[if] thank-you mid-track with agree witness [then] kept [else stop]."""
    words = [
        word("say", start_s=10.0, end_s=10.2, witness="agree", line_final=False),
        word("thank", start_s=10.3, end_s=10.5, witness="agree", line_final=False),
        word("you", start_s=10.6, end_s=10.8, witness="agree", line_final=True),
        word("friend", start_s=11.0, end_s=11.2, witness="agree", line_final=False),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=120.0)
    assert _texts(kept) == ["say", "thank", "you", "friend"]
    assert report.dropped == ()


def test_line_final_thank_you_is_a_sung_line_kept() -> None:
    """[if] the thank-you is itself a sung line (line_final) [then] kept [else stop]."""
    words = [
        word("sing", start_s=1.0, end_s=1.5, witness="agree", line_final=True),
        word("thank", start_s=30.0, end_s=30.3, witness="contradict"),
        word("you", start_s=30.3, end_s=30.6, witness="contradict", line_final=True),
    ]
    kept, _report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert _texts(kept) == ["sing", "thank", "you"]


def test_duration_unknown_skips_past_duration_rule() -> None:
    """[if] duration unknown [then] past-duration rule skipped [else stop]."""
    words = [
        word("late", start_s=999.0, end_s=999.5, witness="contradict", line_final=True),
    ]
    kept, _report = sanitize_words_for_artifact(words, duration_s=None)
    assert len(kept) == 1


@pytest.mark.parametrize("bad", [0, -1.0, "100", True])
def test_malformed_duration_raises(bad: object) -> None:
    with pytest.raises(ValueError):
        sanitize_words_for_artifact([], duration_s=bad)  # type: ignore[arg-type]
