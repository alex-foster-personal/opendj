"""Waveform preview, detail, and tri-band PARITY-01 scoring.

[if] own waveform envelopes vs rekordbox PWAV/PWV3/PWV6 [then] median r is scored, [else stop].
"""

from __future__ import annotations

import pytest

from apps.parity.score import render_report, score_payload
from tests.parity.payloads import (
    DJMD_CONTENT_LIVE_TOTAL,
    MEASURED_AT,
    TRACKS_ROWS_TOTAL,
    _ramp,
    _triband,
    payload,
    round1_waveform_fixture,
    track,
)

pytestmark = pytest.mark.requirement("PARITY-01")


def test_identity_envelopes_score_exact_without_claiming_parity() -> None:
    data = payload(
        [
            track(
                "wave-preview-exact",
                include_waveform=True,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=_ramp(),
                own_preview=_ramp(),
            )
        ],
        round_n=1,
    )
    report = score_payload(data)
    figure = report.figure("waveform_preview")
    assert figure.status == "scored"
    assert figure.exact_n == 1
    assert figure.median_r == 1.0
    assert figure.at_parity is False


def test_reversed_preview_is_disagreement_not_agreement() -> None:
    ramp = _ramp()
    data = payload(
        [
            track(
                "wave-preview-reversed",
                include_waveform=True,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=ramp,
                own_preview=list(reversed(ramp)),
            )
        ],
        round_n=1,
    )
    report = score_payload(data)
    figure = report.figure("waveform_preview")
    assert figure.median_r is not None
    assert figure.median_r < 1.0
    assert "wave-preview-reversed" in figure.disagree_ids
    assert "wave-preview-reversed" not in figure.agree_ids


def test_missing_preview_truth_is_ungradable_without_scoring_other_lanes() -> None:
    triband = _triband()
    data = payload(
        [
            track(
                "wave-missing-preview",
                include_waveform=True,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=None,
                rb_pwv2=None,
                own_preview=_ramp(),
                rb_pwv3=_ramp(start=0.2),
                own_detail=_ramp(start=0.2),
                rb_pwv6=triband,
                own_triband=triband,
            )
        ],
        round_n=1,
    )
    report = score_payload(data)
    preview = report.figure("waveform_preview")
    detail = report.figure("waveform_detail")
    triband_figure = report.figure("waveform_triband")
    assert "wave-missing-preview" in preview.ungradable_ids
    assert preview.ungradable["missing_rb_preview"] == 1
    assert "wave-missing-preview" not in preview.agree_ids
    assert "wave-missing-preview" not in preview.disagree_ids
    assert detail.scored_n == 1
    assert triband_figure.scored_n == 1


def test_unreadable_ext_excludes_detail_only() -> None:
    triband = _triband()
    data = payload(
        [
            track(
                "wave-unreadable-ext",
                include_waveform=True,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_ext_readable=False,
                rb_pwav=_ramp(),
                own_preview=_ramp(),
                rb_pwv3=_ramp(start=0.2),
                own_detail=_ramp(start=0.2),
                rb_pwv6=triband,
                own_triband=triband,
            )
        ],
        round_n=1,
    )
    report = score_payload(data)
    detail = report.figure("waveform_detail")
    preview = report.figure("waveform_preview")
    triband_figure = report.figure("waveform_triband")
    assert "wave-unreadable-ext" in detail.ungradable_ids
    assert detail.ungradable["unreadable_ext"] == 1
    assert "wave-unreadable-ext" not in detail.agree_ids
    assert preview.scored_n == 1
    assert triband_figure.scored_n == 1


def test_missing_detail_truth_is_ungradable() -> None:
    data = payload(
        [
            track(
                "wave-missing-detail",
                include_waveform=True,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=_ramp(),
                own_preview=_ramp(),
                rb_pwv3=None,
                rb_pwv4_luminance=None,
                rb_pwv5=None,
                own_detail=_ramp(start=0.2),
                rb_pwv6=_triband(),
                own_triband=_triband(),
            )
        ],
        round_n=1,
    )
    report = score_payload(data)
    detail = report.figure("waveform_detail")
    assert detail.ungradable["missing_rb_detail"] == 1
    assert "wave-missing-detail" in detail.ungradable_ids


def test_missing_triband_truth_leaves_phrase_not_scored() -> None:
    data = payload(
        [
            track(
                "wave-missing-triband",
                include_waveform=True,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=_ramp(),
                own_preview=_ramp(),
                rb_pwv3=_ramp(start=0.2),
                own_detail=_ramp(start=0.2),
                rb_pwv6=None,
                rb_pwv7=None,
                own_triband=_triband(),
            )
        ],
        round_n=1,
    )
    report = score_payload(data)
    triband = report.figure("waveform_triband")
    phrase = report.figure("phrase")
    assert triband.ungradable["missing_rb_triband"] == 1
    assert phrase.status == "scored"
    assert "wave-missing-triband" in phrase.ungradable_ids
    assert "wave-missing-triband" not in phrase.agree_ids
    assert "wave-missing-triband" not in phrase.disagree_ids


def test_truth_without_own_stays_in_denominator_as_no_own() -> None:
    data = payload(
        [
            track(
                "wave-no-own",
                include_waveform=True,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=_ramp(),
                own_preview=None,
            )
        ],
        round_n=1,
    )
    report = score_payload(data)
    figure = report.figure("waveform_preview")
    assert figure.no_own_n == 1
    assert figure.denominator_n == 1
    assert figure.scored_n == 0
    assert "wave-no-own" in figure.no_own_ids


def test_pwv4_rgb_trap_field_is_ignored() -> None:
    luminance = _ramp(start=0.2)
    rgb_trap = _triband()
    data = payload(
        [
            track(
                "pwv4-luminance",
                include_waveform=True,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwv3=None,
                rb_pwv4_luminance=luminance,
                own_detail=luminance,
                rb_pwv4_rgb=rgb_trap,
            )
        ],
        round_n=1,
    )
    report = score_payload(data)
    figure = report.figure("waveform_detail")
    assert figure.exact_n == 1
    assert figure.median_r == 1.0


def test_denominators_are_fixture_counts_not_library_totals() -> None:
    report = score_payload(round1_waveform_fixture())
    for lane in ("waveform_preview", "waveform_detail", "waveform_triband"):
        figure = report.figure(lane)
        assert figure.denominator_n not in {
            TRACKS_ROWS_TOTAL,
            DJMD_CONTENT_LIVE_TOTAL,
            250,
            229,
        }


def test_render_report_names_waveform_lanes_and_median_r() -> None:
    text = render_report(score_payload(round1_waveform_fixture()))
    assert MEASURED_AT in text
    assert "waveform_preview" in text
    assert "waveform_detail" in text
    assert "waveform_triband" in text
    assert "median r" in text
    assert "at parity" not in text.lower()
    assert "called at parity" not in text.lower()


def test_triband_partial_match_is_disagreement_not_exact() -> None:
    report = score_payload(round1_waveform_fixture())
    triband = report.figure("waveform_triband")
    assert "wave-triband-partial" in triband.disagree_ids
    assert "wave-triband-partial" not in triband.agree_ids
