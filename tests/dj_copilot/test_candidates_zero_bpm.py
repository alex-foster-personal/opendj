"""Regression: zero-BPM candidates must not crash the filter (e2e-gating).

Found live Wed 22 Jul 2026: POST /copilot/suggest-next 500'd with
ZeroDivisionError because the real library carries bpm=0.0 rows (unknown
tempo). Contract: bpm <= 0 means "unknown" (apps.shared.harmonic
convention) - the BPM window is skipped, never divided by.

- if a candidate has bpm=0.0 then filter_candidates raises -> broken.
- if a zero-bpm candidate is silently BPM-dropped -> broken (unknown is
  not "far"; ranking scores it 0.0 instead).
"""
from __future__ import annotations

import pytest

from apps.dj_copilot.candidates import filter_candidates
from apps.dj_copilot.session_context import PlayedTrack, SessionContext
from apps.shared.harmonic import TrackFeature
from tests.dj_copilot.conftest import FIXED_NOW

pytestmark = [pytest.mark.requirement("AI-01")]


def _ctx() -> SessionContext:
    return SessionContext(recent=[], source="manual", captured_at=FIXED_NOW)


def _cur(bpm: float | None = 120.0) -> PlayedTrack:
    return PlayedTrack(
        stable_id="cur", artist="Cur", bpm=bpm, key_camelot="8A",
        energy=5, played_at=FIXED_NOW,
    )


def test_zero_bpm_candidate_passes_window_not_crash() -> None:
    out, trace = filter_candidates(
        current_track=_cur(),
        library=[TrackFeature("t-zero", "Z", 0.0, "8A", 5)],
        context=_ctx(),
    )
    assert [t.stable_id for t in out] == ["t-zero"]
    assert trace.dropped_bpm == 0


def test_zero_bpm_current_skips_window() -> None:
    out, _ = filter_candidates(
        current_track=_cur(bpm=0.0),
        library=[TrackFeature("t-far", "F", 150.0, "8A", 5)],
        context=_ctx(),
    )
    assert [t.stable_id for t in out] == ["t-far"]


def test_normal_window_still_drops_far_bpm() -> None:
    out, trace = filter_candidates(
        current_track=_cur(),
        library=[TrackFeature("t-far", "F", 150.0, "8A", 5)],
        context=_ctx(),
    )
    assert out == []
    assert trace.dropped_bpm == 1
