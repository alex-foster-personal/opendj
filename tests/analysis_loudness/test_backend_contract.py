"""Real acceptance coverage for the own_loudness.backfill lane producer.

The AnalyzerBackend wrapper is registered in apps.analysis.backends; these
tests still call produce_lane_result directly so the LaneResult contract is
checked independently of record construction, against both a real fixture
and a silent one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis.lanes import validate_lane_result
from apps.analysis_loudness.backfill import produce_lane_result
from apps.analysis_loudness.contract import PRODUCER_BACKEND
from tests.analysis_loudness.ffmpeg_fixtures import write_silence_wav, write_sine_wav


@pytest.mark.requirement("NATIVE-07")
def test_own_loudness_backfill_registers_as_a_lane_result(tmp_path: Path) -> None:
    """[if] own_loudness.backfill is unresolvable or invalid [then] fail, [else stop]."""
    assert PRODUCER_BACKEND == "own_loudness.backfill"
    result = produce_lane_result(write_sine_wav(tmp_path / "target.wav"))
    validate_lane_result("loudness", result)
    assert result.status == "ok"
    assert result.payload["integrated_lufs"] == pytest.approx(-23.0, abs=0.1)


@pytest.mark.requirement("NATIVE-07")
def test_own_loudness_backfill_rejects_a_silent_file(tmp_path: Path) -> None:
    """[if] a silent file's peak and RMS measure -inf [then] status is failed, [else stop]."""
    result = produce_lane_result(write_silence_wav(tmp_path / "silent.wav"))
    validate_lane_result("loudness", result)
    assert result.status == "failed"
    assert result.reason == "non_finite_measurement"
    assert result.payload == {}
