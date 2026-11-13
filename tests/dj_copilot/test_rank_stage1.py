"""Stage 1 ranker tests (AI-01)."""

from __future__ import annotations

from pathlib import Path

import pytest

import apps.dj_copilot.rank_stage1 as rank_stage1_module
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
    candidates = [TrackFeature(f"t-{i}", f"A{i}", 120.0 + i * 0.1, "8A", 5) for i in range(10)]
    r1 = rank_stage1(current_track=cur, candidates=candidates)
    r2 = rank_stage1(current_track=cur, candidates=candidates)
    assert [s.stable_id for s in r1] == [s.stable_id for s in r2]


def test_top_n_limits() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    candidates = [TrackFeature(f"t-{i}", f"A{i}", 120.0, "8A", 5) for i in range(50)]
    ranked = rank_stage1(current_track=cur, candidates=candidates, top_n=10)
    assert len(ranked) == 10


def test_rationale_populated() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    cand = TrackFeature("t", "B", 121.0, "8A", 5)
    ranked = rank_stage1(current_track=cur, candidates=[cand])
    assert "camelot" in ranked[0].rationale
    assert "bpm" in ranked[0].rationale
    assert "energy" in ranked[0].rationale


@pytest.mark.requirement("INTEL-12031")
def test_tight_bpm_key_neighbour_outscores_distant() -> None:
    """[if] tight and distant BPM/key pairs compete [then] tight scores higher, [else stop]."""
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    tight = TrackFeature("tight", "B", 121.0, "8A", 5)
    distant = TrackFeature("far", "C", 150.0, "12B", 2)
    ranked = rank_stage1(current_track=cur, candidates=[distant, tight])
    assert ranked[0].stable_id == "tight"


def test_no_literal_neutral_in_source() -> None:
    text = Path(rank_stage1_module.__file__).read_text(encoding="utf-8")
    assert "0.5" not in text


def test_fully_present_scores_unchanged_golden() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    candidates = [
        TrackFeature("t-hit", "B", 121.0, "8A", 5),
        TrackFeature("t-adj", "C", 121.0, "9A", 5),
        TrackFeature("t-far", "D", 140.0, "12B", 2),
    ]
    ranked = rank_stage1(current_track=cur, candidates=candidates)
    assert [(s.stable_id, s.score) for s in ranked] == [
        ("t-hit", 0.997917),
        ("t-adj", 0.947917),
        ("t-far", 0.1375),
    ]
    hit, adj, far = ranked
    assert hit.rationale == {"camelot": 1.0, "bpm": 0.9930555555555556, "energy": 1.0}
    assert adj.rationale == {"camelot": 0.9, "bpm": 0.9930555555555556, "energy": 1.0}
    assert far.rationale == {"camelot": 0.1, "bpm": 0.0, "energy": 0.4375}
    assert all(s.unscored == () for s in ranked)


def test_unknown_key_ranks_below_adjacent_key() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    candidates = [
        TrackFeature("adj", "B", 120.0, "9A", 5),
        TrackFeature("nokey", "C", 120.0, None, 5),
    ]
    ranked = rank_stage1(current_track=cur, candidates=candidates)
    assert [s.stable_id for s in ranked] == ["adj", "nokey"]
    adj, nokey = ranked
    assert adj.unscored == ()
    assert nokey.unscored == ("key",)
    assert "camelot" not in nokey.rationale
    assert adj.score != nokey.score


def test_current_track_without_key_marks_every_candidate() -> None:
    cur = TrackFeature("cur", "A", 120.0, None, 5)
    candidates = [
        TrackFeature("a", "B", 120.0, "8A", 5),
        TrackFeature("b", "C", 121.0, "9A", 5),
    ]
    ranked = rank_stage1(current_track=cur, candidates=candidates)
    assert all(s.unscored == ("key",) for s in ranked)
    assert ranked[0].score >= ranked[1].score


def test_missing_energy_and_bpm_named() -> None:
    cur = TrackFeature("cur", "A", 120.0, "8A", 5)
    candidates = [
        TrackFeature("partial", "B", None, "8A", None),
        TrackFeature("nokey", "C", 120.0, None, 5),
    ]
    ranked = rank_stage1(current_track=cur, candidates=candidates)
    assert ranked[0].stable_id == "nokey"
    partial = next(s for s in ranked if s.stable_id == "partial")
    assert partial.unscored == ("bpm", "energy")
