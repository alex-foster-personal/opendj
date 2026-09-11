"""Phrase lane: rekordbox PSSI vs own phrase analysis."""

from __future__ import annotations

import pytest

from apps.parity.figure import FORBIDDEN_DENOMINATOR_TOTALS, ForbiddenDenominator
from apps.parity.phrase import _kind_accuracy_at_half_second, _normalize_phrases
from apps.parity.score import render_report, score_payload
from tests.parity.payloads import (
    DJMD_CONTENT_LIVE_TOTAL,
    TRACKS_ROWS_TOTAL,
    payload,
    round1_phrase_fixture,
    track,
)

pytestmark = pytest.mark.requirement("PARITY-01")

_REFERENCE_PHRASES = [
    {"start_s": 0.0, "end_s": 16.0, "kind": 2, "mood": 2},
    {"start_s": 16.0, "end_s": 32.0, "kind": 4, "mood": 2},
]


def test_exact_phrase_sequence_is_agreement() -> None:
    """[if] own phrases match rekordbox PSSI at the 0.001 s storage grain
    [then] the track is an exact agreement.
    """
    data = payload(
        [
            track(
                "exact",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pssi=True,
                rb_phrases=_REFERENCE_PHRASES,
                own_phrases=_REFERENCE_PHRASES,
            )
        ]
    )
    phrase = score_payload(data).figure("phrase")
    assert phrase.status == "scored"
    assert phrase.exact_n == 1
    assert phrase.scored_n == 1
    assert phrase.denominator_n == 1
    assert phrase.agree_ids == ("exact",)
    assert phrase.boundary_f_0_5 == 1.0
    assert phrase.boundary_f_3_0 == 1.0
    assert phrase.label_acc == 1.0


def test_round1_fixture_phrase_cases() -> None:
    """The six-row phrase fixture exercises exact, shifted, kind-miss,
    no_own, and missing_pssi without using the beatgrid pool denominator.
    """
    phrase = score_payload(round1_phrase_fixture()).figure("phrase")
    assert phrase.denominator_n == 5
    assert phrase.scored_n == 4
    assert phrase.no_own_n == 1
    assert phrase.exact_n == 1
    assert phrase.agree_ids == ("phrase-exact",)
    assert set(phrase.disagree_ids) == {
        "phrase-shift-0.4",
        "phrase-shift-2.0",
        "phrase-kind-miss",
    }
    assert phrase.no_own_ids == ("phrase-no-own",)
    assert "phrase-missing-pssi" in phrase.ungradable_ids
    assert phrase.ungradable["missing_pssi"] == 1
    assert phrase.denominator_n not in FORBIDDEN_DENOMINATOR_TOTALS
    assert phrase.denominator_n != 250
    assert phrase.at_parity is False
    assert phrase.label_acc == 0.5


def test_phrase_kind_miss_has_zero_kind_accuracy() -> None:
    """[if] own phrase kinds differ at every 0.5 s-matched start [then] kind
    accuracy is 0 for that track while boundary F@0.5 stays 1.
    """
    data = payload(
        [
            track(
                "phrase-kind-miss",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pssi=True,
                rb_phrases=_REFERENCE_PHRASES,
                own_phrases=[
                    {"start_s": 0.0, "end_s": 16.0, "kind": 99, "mood": 2},
                    {"start_s": 16.0, "end_s": 32.0, "kind": 88, "mood": 2},
                ],
            )
        ]
    )
    phrase = score_payload(data).figure("phrase")
    rb = _normalize_phrases(_REFERENCE_PHRASES)
    own = _normalize_phrases(
        [
            {"start_s": 0.0, "end_s": 16.0, "kind": 99, "mood": 2},
            {"start_s": 16.0, "end_s": 32.0, "kind": 88, "mood": 2},
        ]
    )
    assert _kind_accuracy_at_half_second(rb, own) == 0.0
    assert phrase.boundary_f_0_5 == 1.0
    assert phrase.label_acc == 0.0
    assert phrase.disagree_ids == ("phrase-kind-miss",)


def test_missing_pssi_is_out_of_the_phrase_denominator() -> None:
    """[if] a track has no rekordbox PSSI [then] it is missing_pssi, never
    agreement and never a miss, and never in the phrase denominator.
    """
    data = payload(
        [
            track(
                "has-pssi",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pssi=True,
                rb_phrases=_REFERENCE_PHRASES,
                own_phrases=_REFERENCE_PHRASES,
            ),
            track(
                "no-pssi",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pssi=False,
            ),
        ]
    )
    phrase = score_payload(data).figure("phrase")
    assert phrase.denominator_n == 1
    assert "no-pssi" in phrase.ungradable_ids
    assert "no-pssi" not in phrase.agree_ids
    assert "no-pssi" not in phrase.disagree_ids


def test_absent_audio_with_pssi_is_skipped_not_scored() -> None:
    """Present=false rows are filtered before scoring; they are not
    ungradable and not in the phrase denominator.
    """
    data = payload(
        [
            track(
                "absent",
                present=False,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pssi=True,
                rb_phrases=_REFERENCE_PHRASES,
                own_phrases=_REFERENCE_PHRASES,
            )
        ]
    )
    phrase = score_payload(data).figure("phrase")
    assert phrase.denominator_n == 0
    assert phrase.scored_n == 0
    assert phrase.ungradable_ids == ()


def test_render_never_claims_parity_or_forbidden_denominators() -> None:
    """Phrase reporting bands must not read as a calibrated threshold or
    divide by the library-wide row totals.
    """
    text = render_report(score_payload(round1_phrase_fixture()))
    lowered = text.lower()
    assert "at parity" not in lowered
    assert "of 9986" not in text
    assert "of 10479" not in text
    assert "beatgrid pool" in lowered


def test_with_denominator_250_is_not_the_phrase_figure_denominator() -> None:
    """A scorer may record the beatgrid sampled pool in population metadata,
    but the phrase figure must not quote 250 as its denominator unless 250
    rows actually had PSSI.
    """
    phrase = score_payload(round1_phrase_fixture()).figure("phrase")
    assert phrase.denominator_n == 5
    assert 250 not in FORBIDDEN_DENOMINATOR_TOTALS
    with pytest.raises(ForbiddenDenominator):
        phrase.with_denominator(TRACKS_ROWS_TOTAL, "tracks rows")
    with pytest.raises(ForbiddenDenominator):
        phrase.with_denominator(DJMD_CONTENT_LIVE_TOTAL, "live djmdContent")
