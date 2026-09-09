"""Tests for :mod:`scripts.mik_volume_identify` subset selection.

P2 regression (PR #383 review, fresh evidence): a resolved MIK row with no
analysis date at all (``ZANALYSISDATE`` NULL) cannot be proven to be the
bytes MIK actually analysed, so it must not enter the ``mtime_before_analysis``
subset just because there was nothing to compare against.
"""
from __future__ import annotations

from scripts.mik_volume_identify import AudioLoudness, mtime_before_analysis


def _row(pk: int, *, analysis_date_iso: str | None, reencoded: bool) -> AudioLoudness:
    return AudioLoudness(
        mik_pk=pk,
        path=f"/audio/{pk}.flac",
        tier="bookmark",
        volume=-10.0,
        analysis_date_iso=analysis_date_iso,
        reencoded_after_analysis=reencoded,
        sample_peak_dbfs=-3.0,
        true_peak_dbtp=-2.5,
        rms_db=-14.0,
        integrated_lufs=-13.0,
        loudness_range_lu=5.0,
    )


def test_a_row_with_no_analysis_date_is_excluded():
    """reencoded_after_analysis reads False for it only because there was
    nothing to compare against, not because the comparison passed."""
    unknown = _row(1, analysis_date_iso=None, reencoded=False)

    assert mtime_before_analysis([unknown]) == []


def test_a_row_with_a_known_pre_reencode_date_is_kept():
    """Positive control: a genuinely provable row must not be swept up by
    the unknown-date guard."""
    known = _row(2, analysis_date_iso="2024-01-01T00:00:00+00:00", reencoded=False)

    assert mtime_before_analysis([known]) == [known]


def test_a_reencoded_row_with_a_known_date_is_still_excluded():
    known_but_reencoded = _row(3, analysis_date_iso="2024-01-01T00:00:00+00:00", reencoded=True)

    assert mtime_before_analysis([known_but_reencoded]) == []
