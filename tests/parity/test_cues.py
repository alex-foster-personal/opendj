"""PARITY-01 cues_db and cues_anlz lane scoring.

[if] own cues vs rekordbox cues within tolerance [then] matches and misses are scored, [else stop].
"""

from __future__ import annotations

import pytest

from apps.parity.cues import refuse_forbidden_cue_banklist_keys
from apps.parity.score import score_payload
from tests.parity.payloads import hot_cue, payload, track

pytestmark = pytest.mark.requirement("PARITY-01")


def test_cues_db_exact_match_within_20ms() -> None:
    cue = hot_cue(1000, index=0)
    cue_near = hot_cue(1015, index=0)
    data = payload(
        [
            track(
                "exact",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cues_db=[cue],
                own_cues=[cue_near],
            )
        ]
    )
    figure = score_payload(data).figure("cues_db")
    assert figure.status == "scored"
    assert "exact" in figure.agree_ids
    assert figure.denominator_n == 1


def test_cues_db_21ms_outside_tolerance_is_disagree() -> None:
    cue = hot_cue(1000, index=0)
    cue_far = hot_cue(1021, index=0)
    data = payload(
        [
            track(
                "disagree",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cues_db=[cue],
                own_cues=[cue_far],
            )
        ]
    )
    figure = score_payload(data).figure("cues_db")
    assert "disagree" in figure.disagree_ids
    assert "disagree" not in figure.agree_ids


def test_missing_djmd_cue_is_ungradable_not_agree_or_disagree() -> None:
    data = payload(
        [
            track(
                "no-cue",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[hot_cue(1000)],
            )
        ]
    )
    figure = score_payload(data).figure("cues_db")
    assert figure.status == "scored"
    assert "no-cue" in figure.ungradable_ids
    assert figure.ungradable["missing_djmd_cue"] == 1
    assert "no-cue" not in figure.agree_ids
    assert "no-cue" not in figure.disagree_ids
    assert figure.denominator_n == 0


def test_no_own_stays_in_denominator() -> None:
    cue = hot_cue(1000, index=0)
    data = payload(
        [
            track(
                "no-own",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cues_db=[cue],
                own_cues=None,
            )
        ]
    )
    figure = score_payload(data).figure("cues_db")
    assert "no-own" in figure.no_own_ids
    assert figure.denominator_n == 1
    assert "no-own" not in figure.agree_ids
    assert "no-own" not in figure.disagree_ids


def test_empty_own_vs_empty_rb_is_exact() -> None:
    data = payload(
        [
            track(
                "both-empty",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[],
                rb_cues_pcob=[],
                rb_cues_pco2=[],
            )
        ]
    )
    figure = score_payload(data).figure("cues_anlz")
    assert "both-empty" in figure.agree_ids


def test_extra_own_cue_is_disagree() -> None:
    cue = hot_cue(1000, index=0)
    extra = hot_cue(5000, index=1)
    data = payload(
        [
            track(
                "extra-own",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cues_db=[cue],
                own_cues=[cue, extra],
            )
        ]
    )
    figure = score_payload(data).figure("cues_db")
    assert "extra-own" in figure.disagree_ids


def test_anlz_pcob_scored_when_ext_unreadable() -> None:
    cue = hot_cue(2000, index=0)
    data = payload(
        [
            track(
                "unreadable",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[cue],
                rb_cues_pcob=[cue],
                rb_ext_readable=False,
            )
        ]
    )
    figure = score_payload(data).figure("cues_anlz")
    assert figure.status == "scored"
    assert "unreadable" in figure.ungradable_ids
    assert figure.ungradable["unreadable_ext"] == 1
    assert figure.details["pcob_n"] == 1
    assert figure.details["pco2_n"] == 0
    assert "unreadable" in figure.agree_ids


def test_anlz_pco2_counted_when_readable() -> None:
    cue = hot_cue(2000, index=0)
    data = payload(
        [
            track(
                "readable",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[cue],
                rb_cues_pcob=[cue],
                rb_cues_pco2=[cue],
            )
        ]
    )
    figure = score_payload(data).figure("cues_anlz")
    assert figure.details["pco2_n"] == 1


def test_anlz_empty_pcob_is_gradable() -> None:
    data = payload(
        [
            track(
                "empty",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[],
                rb_cues_pcob=[],
                rb_cues_pco2=[],
            )
        ]
    )
    figure = score_payload(data).figure("cues_anlz")
    assert figure.denominator_n == 1
    assert "empty" in figure.agree_ids


def test_banklist_key_raises() -> None:
    data = payload([track("one")])
    data["djmdSongHotCueBanklist"] = []
    with pytest.raises(ValueError, match="djmdSongHotCueBanklist"):
        refuse_forbidden_cue_banklist_keys(data)


def test_anlz_pco2_name_does_not_conflict_when_pcob_name_null() -> None:
    cue = hot_cue(2000, index=0)
    cue_named = hot_cue(2000, index=0, name="drop")
    data = payload(
        [
            track(
                "pcob-exact",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[cue],
                rb_cues_pcob=[cue],
                rb_cues_pco2=[cue_named],
            )
        ]
    )
    figure = score_payload(data).figure("cues_anlz")
    assert "pcob-exact" in figure.agree_ids


def test_cues_db_details_carry_cue_level_counts() -> None:
    cue = hot_cue(1000, index=0)
    extra = hot_cue(5000, index=1)
    data = payload(
        [
            track(
                "one",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cues_db=[cue],
                own_cues=[cue, extra],
            )
        ]
    )
    figure = score_payload(data).figure("cues_db")
    assert figure.details["matched_cues_n"] == 1
    assert figure.details["rb_cues_n"] == 1
    assert figure.details["own_only_cues_n"] == 1
    assert figure.details["conflicts_n"] == 0
