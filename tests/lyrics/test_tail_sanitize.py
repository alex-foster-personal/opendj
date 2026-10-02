"""Tail sanitizer for phantom karaoke words (LYR-08, issue #3995).

[if] produce writes track words [then] phantom tail and past-duration words drop, [else stop].
"""
from __future__ import annotations

import pytest

from apps.lyrics.tail_sanitize import TAIL_MARGIN_S, sanitize_words_for_artifact

from .conftest import word

pytestmark = pytest.mark.requirement("LYR-08")


def test_tail_thank_you_after_line_final_with_weak_witness_dropped() -> None:
    """[if] weak thank-you tail after line_final [then] dropped [else stop]."""
    words = [
        word("sing", start_s=1.0, end_s=1.5, witness="agree", line_final=True),
        word("thank", start_s=200.0, end_s=200.5, witness="contradict", line_final=False),
        word("you", start_s=200.6, end_s=201.0, witness="lost", line_final=False),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=210.0)
    assert [w["word"] for w in kept] == ["sing"]
    assert len(report.dropped) == 2


def test_word_past_duration_margin_dropped() -> None:
    """[if] start past duration margin [then] dropped [else stop]."""
    words = [
        word("end", start_s=98.0, end_s=98.5, witness="agree", line_final=True),
        word("ghost", start_s=99.9, end_s=100.0, witness="contradict", line_final=False),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert [w["word"] for w in kept] == ["end"]
    assert report.dropped[0][2] == "past_duration"


def test_legitimate_final_word_before_end_kept() -> None:
    """[if] final lyric before track end with agree [then] kept [else stop]."""
    words = [
        word("goodbye", start_s=98.0, end_s=98.5, witness="agree", line_final=True),
    ]
    kept, _report = sanitize_words_for_artifact(words, duration_s=100.0)
    assert len(kept) == 1
    assert kept[0]["word"] == "goodbye"


def test_mid_track_thank_you_with_agree_not_dropped() -> None:
    """[if] thank-you mid-track with agree witness [then] kept [else stop]."""
    words = [
        word("say", start_s=10.0, end_s=10.2, witness="agree", line_final=False),
        word("thank", start_s=10.3, end_s=10.5, witness="agree", line_final=False),
        word("you", start_s=10.6, end_s=10.8, witness="agree", line_final=True),
        word("friend", start_s=11.0, end_s=11.2, witness="agree", line_final=False),
    ]
    kept, report = sanitize_words_for_artifact(words, duration_s=120.0)
    assert [w["word"] for w in kept] == ["say", "thank", "you", "friend"]
    assert report.dropped == ()


def test_duration_unknown_skips_past_duration_rule() -> None:
    """[if] duration unknown [then] past-duration rule skipped [else stop]."""
    words = [
        word("late", start_s=999.0, end_s=999.5, witness="contradict", line_final=True),
    ]
    kept, _report = sanitize_words_for_artifact(words, duration_s=None)
    assert len(kept) == 1


def test_tail_margin_constant() -> None:
    assert TAIL_MARGIN_S == 0.25
