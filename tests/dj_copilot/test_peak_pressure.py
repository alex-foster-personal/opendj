"""AI-05 peak energy pressure coach tests."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from apps.dj_copilot.peak_pressure import (
    BUILD_BELOW,
    MIN_SCORED,
    RELEASE_AT,
    PeakPlay,
    compute_peak_pressure,
)

pytestmark = pytest.mark.requirement("AI-05")

_MODULE = Path(__file__).resolve().parents[2] / "apps" / "dj_copilot" / "peak_pressure.py"


def _play(stable_id: str, energy: int | None, *tags: str) -> PeakPlay:
    return PeakPlay(stable_id=stable_id, energy=energy, tags=tuple(tags))


def test_empty_timeline_is_unknown() -> None:
    result = compute_peak_pressure([])
    assert result.score == 0.0
    assert result.cue == "unknown"
    assert result.scored_tracks == 0
    assert result.skipped_unknown == 0


def test_one_peak_play_is_unknown() -> None:
    result = compute_peak_pressure([_play("a", 8)])
    assert result.cue == "unknown"
    assert result.scored_tracks < MIN_SCORED


def test_four_high_energy_plays_release() -> None:
    plays = [_play(f"p{i}", 8) for i in range(4)]
    result = compute_peak_pressure(plays)
    assert result.cue == "release"
    assert result.score >= RELEASE_AT
    assert result.cue_label == "time to release a little"


def test_four_energy_nine_also_release() -> None:
    plays = [_play(f"p{i}", 9) for i in range(4)]
    result = compute_peak_pressure(plays)
    assert result.cue == "release"
    assert result.score >= RELEASE_AT


def test_four_mid_energy_keep_building() -> None:
    plays = [_play(f"p{i}", 5) for i in range(4)]
    result = compute_peak_pressure(plays)
    assert result.cue == "keep_building"
    assert result.score < BUILD_BELOW


def test_four_peak_then_two_low_not_release() -> None:
    plays = [_play(f"p{i}", 8) for i in range(4)] + [_play("l1", 4), _play("l2", 4)]
    result = compute_peak_pressure(plays)
    assert result.cue != "release"


def test_peak_tag_without_energy_counts() -> None:
    result = compute_peak_pressure([_play("a", None, "peak"), _play("b", None, "peak")])
    assert result.scored_tracks == 2
    assert result.cue == "keep_building"


def test_all_unknown_energy_no_tags() -> None:
    plays = [_play("a", None), _play("b", None), _play("c", None)]
    result = compute_peak_pressure(plays)
    assert result.cue == "unknown"
    assert result.skipped_unknown == len(plays)


def test_unknown_plays_in_middle_are_skipped() -> None:
    plays = [_play("a", 8), _play("u", None), _play("b", 8)]
    result = compute_peak_pressure(plays)
    assert result.scored_tracks == 2
    assert result.skipped_unknown == 1


def test_advisory_and_limitation_strings() -> None:
    result = compute_peak_pressure([_play("a", 8), _play("b", 8)])
    assert result.advisory == "Educational coach from track metadata, not the room."
    assert result.limitation == "Metadata energy is not crowd response."


def test_source_has_no_camera_dependency() -> None:
    source = _MODULE.read_text(encoding="utf-8")
    assert re.search(r"(?i)camera|cv2|webcam", source) is None
