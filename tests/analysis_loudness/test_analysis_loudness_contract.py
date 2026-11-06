"""apps.analysis_loudness is reached through its contract (ADR-NEW-module-contracts).

[if] build_service() returns the shipped implementation [then] it satisfies the
Protocol and stamps the lane's canonical producer identity [else failed].
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from apps.analysis.lanes import own_backend
from apps.analysis_loudness import contract
from apps.analysis_loudness.backfill import produce_lane_result
from apps.analysis_loudness.http import build_service
from apps.loudness.scan import LoudnessError
from tests.analysis_loudness.ffmpeg_fixtures import write_sine_wav


def test_service_satisfies_the_protocol() -> None:
    service = build_service()
    assert isinstance(service, contract.LoudnessAnalysisService)


def test_an_object_without_the_operation_is_not_a_service() -> None:
    assert not isinstance(object(), contract.LoudnessAnalysisService)


def test_contract_producer_identity_matches_the_lane_naming_scheme() -> None:
    assert own_backend("loudness", "backfill") == contract.PRODUCER_BACKEND
    assert contract.PRODUCER_VERSION == "1.0.0"
    assert contract.CONTRACT_VERSION == "analysis_loudness/1"


def _ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        # Raised, not called: mypy in the quality gate has no pytest stubs, so
        # it cannot see that pytest.skip() never returns.
        raise pytest.skip.Exception("needs an ffmpeg executable on PATH to decode the fixture")
    return ffmpeg


def test_service_measures_through_the_real_producer(tmp_path: Path) -> None:
    ffmpeg = _ffmpeg()
    wav = write_sine_wav(tmp_path / "sine.wav", duration_s=3.0)
    via_contract = build_service().produce_lane_result(wav, binary=ffmpeg)
    assert via_contract.status == "ok"
    assert via_contract == produce_lane_result(wav, binary=ffmpeg)
    assert math.isfinite(via_contract.payload["integrated_lufs"])


def test_service_fails_loudly_like_the_producer(tmp_path: Path) -> None:
    # Bytes ffmpeg cannot decode: the service must raise, never report a result.
    ffmpeg = _ffmpeg()
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not audio at all" * 64)
    with pytest.raises(LoudnessError) as via_contract:
        build_service().produce_lane_result(junk, binary=ffmpeg)
    with pytest.raises(LoudnessError) as direct:
        produce_lane_result(junk, binary=ffmpeg)
    # ffmpeg prints a per-run pointer in its message, so compare the first line only.
    assert str(via_contract.value).splitlines()[0] == str(direct.value).splitlines()[0]
