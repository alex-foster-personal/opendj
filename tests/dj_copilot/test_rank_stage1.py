"""Stage 1 ranker tests (AI-01)."""
from __future__ import annotations

import pytest

from apps.dj_copilot.rank_stage1 import rank_stage1
from apps.shared.harmonic import TrackFeature

pytestmark = pytest.mark.requirement("AI-01")


def test_identical_track_ranks_top() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    candidates = [
        TrackFeature("t-other", "B", 130.0, "12B", 2),
        TrackFeature("t-twin", "C", 120.0, "8A", 5),
    ]
    ranked = rank_stage1(current_track=cur, candidates=candidates)
    assert ranked[0].stable_id == "t-twin"


def test_deterministic() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    candidates = [
        TrackFeature(f"t-{i}", f"A{i}", 120.0 + i * 0.1, "8A", 5)
        for i in range(10)
    ]
    r1 = rank_stage1(current_track=cur, candidates=candidates)
    r2 = rank_stage1(current_track=cur, candidates=candidates)
    assert [s.stable_id for s in r1] == [s.stable_id for s in r2]


def test_top_n_limits() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    candidates = [
        TrackFeature(f"t-{i}", f"A{i}", 120.0, "8A", 5) for i in range(50)
    ]
    ranked = rank_stage1(current_track=cur, candidates=candidates, top_n=10)
    assert len(ranked) == 10


def test_rationale_populated() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    cand = TrackFeature("t", "B", 121.0, "8A", 5)
    ranked = rank_stage1(current_track=cur, candidates=[cand])
    assert "camelot" in ranked[0].rationale
    assert "bpm" in ranked[0].rationale
    assert "energy" in ranked[0].rationale
