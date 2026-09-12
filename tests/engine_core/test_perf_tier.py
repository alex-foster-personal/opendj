"""PERFMODE-01 tier classification, override, HTTP, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.host_info import HOST_INFO_STATE_ATTR, HostIdentity, HostInfoOut
from apps.engine_core.perf_tier_api import PERF_TIER_PATH, add_perf_tier_route
from apps.shared.perf_tier import (
    CanaryThresholdsUnmeasured,
    HostFacts,
    HostInfoUnavailable,
    InvalidPerfTierOverride,
    PerfTier,
    background_worker_count,
    classify_auto,
    classify_from_canary,
    local_stems_tier_refusal,
    main,
    parse_override,
    resolve_tier,
    scalers_for,
    stem_decode_eagerness,
    tier_wire,
)

GiB = 1024**3


@pytest.mark.requirement("PERFMODE-01")
@pytest.mark.parametrize(
    ("ram", "cpus", "expected"),
    [
        (16 * GiB, 8, PerfTier.HIGH),
        (16 * GiB, 7, PerfTier.STANDARD),
        (8 * GiB, 4, PerfTier.STANDARD),
        (8 * GiB, 3, PerfTier.LOW),
        (int(7.9 * GiB), 64, PerfTier.LOW),
    ],
)
def test_classify_auto_boundaries(ram: int, cpus: int, expected: PerfTier) -> None:
    assert classify_auto(HostFacts(logical_cpus=cpus, ram_bytes=ram)) == expected


@pytest.mark.requirement("PERFMODE-01")
def test_classify_from_canary_raises() -> None:
    with pytest.raises(CanaryThresholdsUnmeasured):
        classify_from_canary(20_000)


@pytest.mark.requirement("PERFMODE-01")
def test_override_low_wins_over_high_facts() -> None:
    facts = HostFacts(logical_cpus=16, ram_bytes=32 * GiB)
    assert resolve_tier(facts=facts, override="low") == PerfTier.LOW


@pytest.mark.requirement("PERFMODE-01")
def test_invalid_override_raises() -> None:
    with pytest.raises(InvalidPerfTierOverride):
        parse_override("AUTO")


@pytest.mark.requirement("PERFMODE-01")
def test_scalers_for_locked_table() -> None:
    low = scalers_for(PerfTier.LOW)
    assert low["prefetch_tracks"] == 2
    assert low["prefetch_bytes"] == 24 * 1024 * 1024
    assert low["anlz_entries"] == 8
    assert stem_decode_eagerness(PerfTier.STANDARD) == "mix-first"


@pytest.mark.requirement("PERFMODE-01")
def test_background_worker_count_halves_on_low() -> None:
    assert background_worker_count(16, PerfTier.LOW) == 8
    assert background_worker_count(16, PerfTier.STANDARD) == 16


@pytest.mark.requirement("PERFMODE-01")
def test_tier_wire_cli_shape() -> None:
    wire = tier_wire(facts=HostFacts(logical_cpus=10, ram_bytes=16 * GiB))
    assert wire["tier"] == "HIGH"
    assert "scalers" in wire
    assert wire["host"] == {"logical_cpus": 10, "ram_bytes": 16 * GiB}


@pytest.mark.requirement("PERFMODE-01")
def test_perf_tier_http_200_with_override_when_host_failed(tmp_path: Path) -> None:
    prefs = tmp_path / "state" / "ui-prefs.json"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"perf_tier": "low"}) + "\n", encoding="utf-8")
    app = FastAPI()
    setattr(
        app.state,
        HOST_INFO_STATE_ATTR,
        HostIdentity(info=None, failure="psutil broke", logical_cpus=None, ram_bytes=None),
    )
    add_perf_tier_route(app, data_dir=tmp_path)
    client = TestClient(app)
    response = client.get(PERF_TIER_PATH)
    assert response.status_code == 200
    assert response.json()["tier"] == "LOW"


@pytest.mark.requirement("PERFMODE-01")
def test_perf_tier_http_503_when_auto_and_host_failed(tmp_path: Path) -> None:
    app = FastAPI()
    setattr(
        app.state,
        HOST_INFO_STATE_ATTR,
        HostIdentity(info=None, failure="psutil broke", logical_cpus=None, ram_bytes=None),
    )
    add_perf_tier_route(app, data_dir=tmp_path)
    client = TestClient(app)
    response = client.get(PERF_TIER_PATH)
    assert response.status_code == 503


@pytest.mark.requirement("PERFMODE-01")
def test_tier_wire_auto_without_host_raises() -> None:
    with pytest.raises(HostInfoUnavailable):
        tier_wire(facts=None, host_failure="missing", override="auto")


@pytest.mark.requirement("PERFMODE-01")
def test_resolve_tier_reads_low_from_prefs(tmp_path: Path) -> None:
    prefs = tmp_path / "state" / "ui-prefs.json"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"perf_tier": "low"}) + "\n", encoding="utf-8")
    facts = HostFacts(logical_cpus=16, ram_bytes=32 * GiB)
    assert resolve_tier(facts=facts, data_dir=tmp_path) == PerfTier.LOW


@pytest.mark.requirement("PERFMODE-01")
def test_local_stems_refusal_honors_prefs_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefs = tmp_path / "state" / "ui-prefs.json"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"perf_tier": "low"}) + "\n", encoding="utf-8")
    monkeypatch.setattr("apps.shared.paths.DATA_DIR", tmp_path)
    assert local_stems_tier_refusal() is not None


@pytest.mark.requirement("PERFMODE-01")
def test_cli_exit_two_when_host_unreadable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom() -> HostFacts:
        raise HostInfoUnavailable("psutil.cpu_count returned None")

    monkeypatch.setattr("apps.shared.perf_tier.read_host_facts", _boom)
    assert main() == 2
    assert "None" in capsys.readouterr().err
