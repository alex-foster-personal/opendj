"""BPM lane: djmdContent.BPM (integer, BPM x100) vs own analysis.bpm.

[if] own BPM is compared to rekordbox BPM x100 [then] it is exact, octave, or a miss, [else stop].
"""

from __future__ import annotations

import pytest

from apps.parity.score import score_payload
from tests.parity.payloads import payload, track

pytestmark = pytest.mark.requirement("PARITY-01")


def test_exact_match_is_the_rekordbox_storage_granularity() -> None:
    """[if] own BPM rounds to the same BPM x100 integer rekordbox stored [then]
    that track is an exact match, because 0.01 BPM is the storage grain.
    """
    data = payload(
        [
            track("exact", rb_bpm_x100=12800, own_bpm=128.0),
            track("rounds-in", rb_bpm_x100=12800, own_bpm=128.004),
        ]
    )
    bpm = score_payload(data).figure("bpm")
    assert bpm.exact_n == 2
    assert bpm.scored_n == 2
    assert bpm.denominator_n == 2
    assert set(bpm.agree_ids) == {"exact", "rounds-in"}


def test_within_band_counts_are_named_not_a_hidden_threshold() -> None:
    """Bands 0.1 and 1.0 BPM are reporting grains, never a parity claim.

    128.08 vs 128.00 is inside 0.1 and not exact. 128.9 vs 128.00 is inside
    1.0 and outside 0.1. The figure names both counts; it does not pick one
    as the pass line.
    """
    data = payload(
        [
            track("tight", rb_bpm_x100=12800, own_bpm=128.08),
            track("loose", rb_bpm_x100=12800, own_bpm=128.9),
        ]
    )
    bpm = score_payload(data).figure("bpm")
    assert bpm.exact_n == 0
    assert bpm.within_0_1_n == 1
    assert bpm.within_1_0_n == 2
    assert bpm.at_parity is False


def test_octave_error_is_classified_separately_from_a_plain_miss() -> None:
    """[if] own BPM is half or double the rekordbox value [then] the row is
    an octave disagreement, not a plain miss and not agreement.
    """
    data = payload(
        [
            track("half", rb_bpm_x100=12800, own_bpm=64.0),
            track("double", rb_bpm_x100=6400, own_bpm=128.0),
            track("wrong", rb_bpm_x100=12800, own_bpm=140.0),
        ]
    )
    bpm = score_payload(data).figure("bpm")
    assert set(bpm.octave_ids) == {"half", "double"}
    assert "wrong" in bpm.disagree_ids
    assert "wrong" not in bpm.octave_ids
    assert "half" not in bpm.agree_ids


def test_zero_rekordbox_bpm_is_ungradable() -> None:
    """BPM 0 is not a tempo rekordbox measured; folding it into the
    denominator would score a missing analysis as a miss.
    """
    data = payload([track("zero", rb_bpm_x100=0, own_bpm=128.0)])
    bpm = score_payload(data).figure("bpm")
    assert bpm.denominator_n == 0
    assert bpm.ungradable["missing_rb_bpm"] == 1
    assert bpm.scored_n == 0


def test_no_own_bpm_is_excluded_from_agreement_and_from_miss() -> None:
    """A track with rekordbox BPM and no own analysis cannot be scored yet.

    It stays in the lane's RB-GT denominator and is counted in `no_own_analysis`,
    never as agreement and never as a miss.
    """
    data = payload(
        [
            track("scored", rb_bpm_x100=12800, own_bpm=128.0),
            track("waiting", rb_bpm_x100=12000, own_bpm=None),
        ]
    )
    bpm = score_payload(data).figure("bpm")
    assert bpm.denominator_n == 2
    assert bpm.scored_n == 1
    assert bpm.no_own_n == 1
    assert "waiting" not in bpm.agree_ids
    assert "waiting" not in bpm.disagree_ids
    assert "waiting" in bpm.no_own_ids
