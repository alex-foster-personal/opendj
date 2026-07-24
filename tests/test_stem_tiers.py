"""Regression tests for the S/M/L separation tiers.

One-line intent per test, in the house format:
  if a tier names a preset the farm cannot run, config is broken
  if a tier's model is not baked into the farm image, config is broken
  if an unbenchmarked (tier, gpu) pair returns a number, the estimator is broken
  if the estimator interpolates from a neighbouring card, the estimator is broken
  if wall and gpu clocks are conflated, the cost maths is broken
  if the farm's mirrored constants drift from tiers.py, the mirror is broken
  if the batch estimate ignores the concurrency cap, the packer is broken
"""

from __future__ import annotations

import pytest

from apps.stems import tiers as tiercfg


# ----- helpers ---------------------------------------------------------------
def _fake_throughput(tier_key: str, gpu: str) -> tiercfg.Throughput:
    return tiercfg.Throughput(
        tier_key=tier_key,
        gpu=gpu,
        wall_fixed_s=20.0,
        wall_s_per_audio_minute=10.0,
        gpu_fixed_s=8.0,
        gpu_s_per_audio_minute=6.0,
        n_tracks=3,
        duration_span_s=[150.0, 330.0, 600.0],
        r_squared=0.99,
        measured_by="test",
        measured_at="2026-07-24T00:00:00+00:00",
    )


@pytest.fixture
def measured(monkeypatch):
    """Install one measured pair, M@H100, and nothing else."""
    monkeypatch.setattr(
        tiercfg, "THROUGHPUT", {"M@H100": _fake_throughput("M", "H100")}
    )
    return tiercfg


# ----- config coherence ------------------------------------------------------
def test_three_tiers_exist_and_are_distinct():
    """if the tier table has fewer than three distinct products, config broken"""
    assert set(tiercfg.TIERS) == {"S", "M", "L"}
    tags = [t.preset_tag for t in tiercfg.TIERS.values()]
    assert len(set(tags)) == 3, f"tiers share a preset tag: {tags}"


def test_default_tier_is_a_real_tier():
    """if the default names a tier that does not exist, config broken"""
    assert tiercfg.DEFAULT_TIER in tiercfg.TIERS


def test_every_tier_preset_is_runnable_by_the_farm():
    """if a tier names a preset the farm cannot run, config broken"""
    modal = pytest.importorskip("modal")  # noqa: F841
    from scripts.modal_vocal_farm import PRESETS

    for tier in tiercfg.TIERS.values():
        assert tier.preset_tag in PRESETS, (
            f"tier {tier.key} names {tier.preset_tag!r}, not in PRESETS"
        )
        rung = PRESETS[tier.preset_tag]
        assert (rung.model, rung.overlap, rung.shifts) == (
            tier.model, tier.overlap, tier.shifts
        )


def test_every_tier_model_is_baked_into_the_image():
    """if a tier's model is not baked, every cold container re-downloads it"""
    pytest.importorskip("modal")
    from scripts.modal_vocal_farm import BAKED_MODELS

    for tier in tiercfg.TIERS.values():
        assert tier.model in BAKED_MODELS, (
            f"tier {tier.key} needs {tier.model!r}, baked: {BAKED_MODELS}"
        )


def test_farm_mirror_matches_tier_config():
    """if the farm's mirrored constants drift from tiers.py, mirror broken"""
    pytest.importorskip("modal")
    import scripts.modal_vocal_farm as farm

    assert farm.GPU_USD_PER_S == tiercfg.GPU_USD_PER_S
    assert farm.DEFAULT_GPU_KIND == tiercfg.DEFAULT_GPU
    assert farm.DEFAULT_MAX_CONTAINERS == tiercfg.MAX_CONCURRENT_GPUS
    farm._assert_tier_mirror_matches()


def test_blackwell_cards_are_not_selectable():
    """if B200/B300 appear as options, the pinned torch would fail to launch"""
    assert "B200" not in tiercfg.SUPPORTED_GPUS
    assert "B300" not in tiercfg.SUPPORTED_GPUS
    assert tiercfg.DEFAULT_GPU in tiercfg.SUPPORTED_GPUS


# ----- estimator refuses to guess --------------------------------------------
def test_unmeasured_pair_raises_rather_than_guessing(measured):
    """if an unbenchmarked pair returns a number, the estimator is broken"""
    with pytest.raises(tiercfg.ThroughputNotMeasured) as exc:
        tiercfg.estimate_seconds(240.0, "L", "H100")
    assert "L on H100" in str(exc.value)
    assert "tier_throughput" in str(exc.value), "error must name the fix"


def test_does_not_interpolate_from_a_neighbouring_card(measured):
    """if a measurement on one card answers for another, estimator is broken"""
    assert tiercfg.estimate_seconds(240.0, "M", "H100") > 0  # measured pair works
    with pytest.raises(tiercfg.ThroughputNotMeasured):
        tiercfg.estimate_seconds(240.0, "M", "L4")


def test_unknown_tier_key_raises():
    """if a typo'd tier silently resolves, config lookup is broken"""
    with pytest.raises(KeyError):
        tiercfg.get_tier("XL")


# ----- estimator arithmetic --------------------------------------------------
def test_estimate_is_fixed_plus_linear(measured):
    """if the estimate is not fixed + minutes * slope, the fit is misapplied"""
    # 240s = 4 audio-minutes -> 20 + 4 * 10 = 60
    assert tiercfg.estimate_seconds(240.0, "M", "H100") == pytest.approx(60.0)
    # 600s = 10 audio-minutes -> 20 + 10 * 10 = 120
    assert tiercfg.estimate_seconds(600.0, "M", "H100") == pytest.approx(120.0)


def test_cost_uses_billed_seconds_not_wall(measured):
    """if wall clock is billed, cost is overstated by the cold start"""
    # billed = 8 + 4 * 6 = 32s; wall would be 60s. They must differ.
    expected = 32.0 * tiercfg.GPU_USD_PER_S["H100"]
    assert tiercfg.estimate_usd(240.0, "M", "H100") == pytest.approx(expected)
    wall_priced = 60.0 * tiercfg.GPU_USD_PER_S["H100"]
    assert tiercfg.estimate_usd(240.0, "M", "H100") < wall_priced


# ----- batch packing ---------------------------------------------------------
def test_batch_respects_the_concurrency_cap(measured):
    """if the batch estimate ignores the cap, it promises impossible throughput"""
    durations = [240.0] * 20  # 20 tracks, 60s each, cap 10 -> two waves
    total = tiercfg.estimate_batch_seconds(
        durations, "M", "H100", max_concurrent=10
    )
    assert total == pytest.approx(120.0)


def test_batch_of_one_equals_single_estimate(measured):
    """if a one-track batch differs from a single estimate, the packer is broken"""
    assert tiercfg.estimate_batch_seconds(
        [240.0], "M", "H100"
    ) == pytest.approx(tiercfg.estimate_seconds(240.0, "M", "H100"))


def test_empty_batch_is_zero(measured):
    """if an empty batch costs time, the packer is broken"""
    assert tiercfg.estimate_batch_seconds([], "M", "H100") == 0.0


# ----- API parity ------------------------------------------------------------
def test_api_reports_unmeasured_rather_than_a_number(measured):
    """if the API renders a guess as a number, an assumption becomes truth"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    client = TestClient(create_app())
    body = client.get("/api/v1/stems/estimate", params={"seconds": 240}).json()
    by_tier = {row["tier"]: row for row in body["tiers"]}
    assert by_tier["L"]["measured"] is False
    assert by_tier["L"]["seconds"] is None
    assert by_tier["L"]["unavailable_reason"]


def test_api_lists_every_tier_with_its_evidence():
    """if a tier ships without recorded evidence, the choice is unreviewable"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    client = TestClient(create_app())
    rows = client.get("/api/v1/stems/tiers").json()
    assert {r["key"] for r in rows} == {"S", "M", "L"}
    for row in rows:
        assert row["evidence"].strip(), f"tier {row['key']} has no evidence"
        assert row["evidence_strength"] in {"MEASURED", "PARTIAL", "UNMEASURED"}
