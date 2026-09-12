"""LATENCY-04 live-stems machine capability contract.

Regression lines:
- [if] a pre-M1 machine receives a usable live-stems tier [then] a live set
  fails only after the user starts it
- [if] M1 selects more than the lowest tier or M3 does not select high quality
  [then] the named hardware policy has drifted
- [if] four decks report the two-deck latency lookahead [then] the extra
  live-stems latency budget is hidden
- [if] an installed capability record is missing from HTTP or CLI [then]
  browser and agent flows disagree about what this machine can sustain
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.stems.live_capability import (
    BENCHMARK_MIN_HASHES_PER_SECOND,
    LIVE_STEMS_CAPABILITY_PATH,
    LiveStemsCapabilityError,
    LiveStemsQuality,
    assess_install_once,
    assess_machine,
    load_capability,
    main,
    persist_capability,
    plan_live_stems,
    reassess_persisted_capability,
)
from apps.stems.live_capability_api import router


@pytest.mark.requirement("LATENCY-04")
def test_hardware_thresholds_disable_pre_m1_and_tier_apple_silicon() -> None:
    """[if] hardware is classified [then] pre-M1 disables, M1 is low, M3 is high, [else stop]."""
    pre_m1 = assess_machine("Intel Mac", BENCHMARK_MIN_HASHES_PER_SECOND)
    m1 = assess_machine("Apple M1", BENCHMARK_MIN_HASHES_PER_SECOND)
    m3 = assess_machine("Apple M3 Max", BENCHMARK_MIN_HASHES_PER_SECOND)

    assert pre_m1.enabled is False
    assert "Intel Mac" in pre_m1.reason
    assert m1.quality is LiveStemsQuality.LOW
    assert m3.quality is LiveStemsQuality.HIGH


@pytest.mark.requirement("LATENCY-04")
def test_a_failed_benchmark_refuses_before_live_stems_start() -> None:
    """[if] the benchmark is below floor [then] capability disables before start, [else stop]."""
    capability = assess_machine("Apple M3", BENCHMARK_MIN_HASHES_PER_SECOND - 1)

    assert capability.enabled is False
    assert "benchmark" in capability.reason.lower()


@pytest.mark.requirement("LATENCY-04")
def test_four_decks_receive_the_named_eight_bar_lookahead() -> None:
    """[if] four decks are planned [then] lookahead is eight bars, [else stop]."""
    capability = assess_machine("Apple M3", BENCHMARK_MIN_HASHES_PER_SECOND)

    two_decks = plan_live_stems(capability, deck_count=2, bpm=120)
    four_decks = plan_live_stems(capability, deck_count=4, bpm=120)

    assert two_decks.lookahead_bars == 4
    assert two_decks.lookahead_ms == 8_000
    assert four_decks.lookahead_bars == 8
    assert four_decks.lookahead_ms == 16_000


@pytest.mark.requirement("LATENCY-04")
def test_install_record_is_shared_by_http_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a record is persisted [then] HTTP GET and CLI print the same quality, [else stop]."""
    capability = assess_machine("Apple M3 Pro", BENCHMARK_MIN_HASHES_PER_SECOND)
    persist_capability(tmp_path, capability)

    stored = load_capability(tmp_path)
    assert stored == capability

    app = FastAPI()
    app.state.live_stems_capability = stored
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.get(LIVE_STEMS_CAPABILITY_PATH)
    assert response.status_code == 200
    assert response.json()["quality"] == "high"

    assert main(["--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["quality"] == "high"


@pytest.mark.requirement("LATENCY-04")
def test_first_install_assessment_is_real_and_runs_once(tmp_path: Path) -> None:
    """[if] assessment runs twice [then] the second returns the first record, [else stop]."""
    first = assess_install_once(tmp_path)
    second = assess_install_once(tmp_path)

    assert first == second
    assert load_capability(tmp_path) == first


@pytest.mark.requirement("LATENCY-04")
def test_machine_class_change_reassesses_a_copied_install_record(tmp_path: Path) -> None:
    """[if] a copied record differs in class [then] reassessment replaces it, [else stop]."""
    persist_capability(
        tmp_path, assess_machine("Apple M3 Max", BENCHMARK_MIN_HASHES_PER_SECOND)
    )

    refreshed = reassess_persisted_capability(
        tmp_path,
        machine_name="Intel Mac",
        benchmark_hashes_per_second=BENCHMARK_MIN_HASHES_PER_SECOND,
    )

    assert refreshed.enabled is False
    assert refreshed.machine_name == "Intel Mac"


@pytest.mark.requirement("LATENCY-04")
@pytest.mark.parametrize("bpm", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_bpm_is_a_named_refusal(bpm: float) -> None:
    """[if] BPM is NaN or infinite [then] plan raises LiveStemsCapabilityError, [else stop]."""
    capability = assess_machine("Apple M3", BENCHMARK_MIN_HASHES_PER_SECOND)

    with pytest.raises(LiveStemsCapabilityError, match="BPM must be finite"):
        plan_live_stems(capability, deck_count=2, bpm=bpm)


@pytest.mark.requirement("LATENCY-04")
def test_committed_engine_openapi_exposes_live_capability() -> None:
    """[if] the OpenAPI is read [then] GET /stems/live-capability is in paths, [else stop]."""
    schema_path = Path(__file__).resolve().parents[2] / "apps/webui/openapi.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert LIVE_STEMS_CAPABILITY_PATH in schema["paths"]
