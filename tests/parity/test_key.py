"""Key lane: djmdContent.KeyID -> djmdKey vs own camelot/openkey."""

from __future__ import annotations

import pytest

from apps.parity.score import score_payload
from tests.parity.payloads import payload, track

pytestmark = pytest.mark.requirement("PARITY-01")


def test_exact_key_match_accepts_spelled_or_camelot_rekordbox_scale_name() -> None:
    """[if] own camelot/openkey canonicalize to the same (pitch class, mode)
    as rekordbox ScaleName [then] the row is an exact key match.

    Real djmdKey.ScaleName rows mix spelled names and bare Camelot codes.
    """
    data = payload(
        [
            track("spelled", rb_key_scale_name="Am", own_key_camelot="8A", own_key_openkey="1m"),
            track("camelot-rb", rb_key_scale_name="8A", own_key_camelot="8A", own_key_openkey="1m"),
            track("c-major", rb_key_scale_name="C", own_key_camelot="8B", own_key_openkey="1d"),
        ]
    )
    key = score_payload(data).figure("key")
    assert key.exact_n == 3
    assert key.scored_n == 3
    assert key.denominator_n == 3


def test_relative_major_minor_is_not_an_exact_match() -> None:
    """C major vs A minor is a relative pair. PARITY-01 reports it as a
    related disagreement (MIREX 0.3), never as agreement and never as a
    second, divergent key metric from the analysis_bench key scorer -- it
    reuses that scorer's weighted_score.
    """
    data = payload(
        [
            track(
                "relative",
                rb_key_scale_name="C",
                own_key_camelot="8A",
                own_key_openkey="1m",
            )
        ]
    )
    key = score_payload(data).figure("key")
    assert key.exact_n == 0
    assert key.related_n == 1
    assert "relative" in key.disagree_ids
    assert key.mirex_mean == pytest.approx(0.3)


def test_scale_name_all_is_ungradable() -> None:
    """rekordbox ScaleName literal 'All' is a sentinel, not a key. One
    observed row in the library; it must not enter the key denominator.
    """
    data = payload(
        [
            track("sentinel", rb_key_scale_name="All", own_key_camelot="8A", own_key_openkey="1m"),
            track("real", rb_key_scale_name="Am"),
        ]
    )
    key = score_payload(data).figure("key")
    assert key.denominator_n == 1
    assert key.ungradable["missing_rb_key"] == 1
    assert "sentinel" not in key.agree_ids
    assert "sentinel" not in key.disagree_ids


def test_inconsistent_own_camelot_and_openkey_is_a_failed_own_answer() -> None:
    """[if] own camelot and openkey parse to different keys [then] the row
    is a failed own answer, not agreement with whichever side happened to
    match rekordbox.
    """
    data = payload(
        [
            track(
                "split",
                rb_key_scale_name="Am",
                own_key_camelot="8A",
                own_key_openkey="1d",  # C major, not A minor
            )
        ]
    )
    key = score_payload(data).figure("key")
    assert key.scored_n == 0
    assert key.failed_own_n == 1
    assert "split" not in key.agree_ids
