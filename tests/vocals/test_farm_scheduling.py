"""Regression tests for batch ordering and the round-trip latency split.

Two independent defects motivated these. First, ``.starmap`` publishes in INPUT
order, so one 63.3-minute mix mid-batch held 8.8% of a run's container time and
stalled everything queued behind it. Second, every timer in the pipeline lived
INSIDE the container, leaving upload, queue wait, cold start, the trip home and
the ordering delay as one unmeasured residual that three rival hypotheses each
claimed.

Single-line intent, in the repo's regression style:
  - if the batch is not longest-first then one long track stalls everything behind it
  - if ordering is not deterministic then two runs of the same gap are not comparable
  - if duration is missing and size is ignored then a long untagged track still stalls
  - if the round trip does not decompose exactly then the residual is still unmeasured
  - if clock skew is clamped away then a skewed split reads as a real measurement
  - if the farm's prefetch constants drift from the module then flags lie about defaults
"""
from __future__ import annotations

import pytest

pytest.importorskip("modal", reason="needs the optional modal package")

from scripts.modal_vocal_farm import (
    PREFETCH_DEPTH,
    PREFETCH_MAX_BYTES,
    PREFETCH_WORKERS,
    FarmTally,
    GapTrack,
    _record_latencies,
    sort_longest_first,
)


def _track(stable_id: str, duration_ms: int = 0, size: int = 0) -> GapTrack:
    from pathlib import Path

    return GapTrack(
        stable_id=stable_id,
        audio_path=Path(f"/tmp/{stable_id}.mp3"),
        duration_ms=duration_ms,
        size_bytes=size,
    )


def test_longest_track_goes_first() -> None:
    """if the batch is not longest-first then one long track stalls the rest"""
    batch = [
        _track("short", duration_ms=180_000),
        _track("themix", duration_ms=3_798_000),  # the real 63.3-minute mix
        _track("medium", duration_ms=600_000),
    ]
    assert [t.stable_id for t in sort_longest_first(batch)] == [
        "themix", "medium", "short",
    ]


def test_order_is_deterministic() -> None:
    """if ordering is not deterministic then two runs are not comparable"""
    batch = [_track(name, duration_ms=200_000) for name in ("c", "a", "b")]
    assert [t.stable_id for t in sort_longest_first(batch)] == ["a", "b", "c"]
    assert sort_longest_first(batch) == sort_longest_first(list(reversed(batch)))


def test_size_breaks_ties_when_duration_is_missing() -> None:
    """if duration is missing and size ignored then a long untagged track stalls"""
    batch = [
        _track("tagged", duration_ms=120_000, size=4_000_000),
        _track("untagged_big", duration_ms=0, size=54_800_000),
        _track("untagged_small", duration_ms=0, size=3_000_000),
    ]
    ordered = [t.stable_id for t in sort_longest_first(batch)]
    # A tagged duration always outranks an untagged row, but among untagged
    # rows the big file must still come before the small one.
    assert ordered[0] == "tagged"
    assert ordered.index("untagged_big") < ordered.index("untagged_small")


def test_round_trip_decomposes_exactly() -> None:
    """if the round trip does not decompose exactly then the residual survives"""
    tally = FarmTally()
    result = {"stable_id": "x", "wall_span": [1000.0, 1008.0], "timings": {}}
    _record_latencies(result, yielded_at=998.5, received_at=1010.25, tally=tally)

    timings = result["timings"]
    assert timings["dispatch_s"] == pytest.approx(1.5)
    assert timings["return_s"] == pytest.approx(2.25)
    assert timings["round_trip_s"] == pytest.approx(11.75)
    container_s = 1008.0 - 1000.0
    assert timings["dispatch_s"] + container_s + timings["return_s"] == pytest.approx(
        timings["round_trip_s"]
    )
    assert tally.clock_skew_calls == 0


def test_ordering_delay_shows_up_as_return_time() -> None:
    """if a buffered result does not show large return_s then head-of-line hides"""
    tally = FarmTally()
    # A short call that finished at once but was published 40s later, stuck
    # behind a longer call in starmap's ordering buffer.
    result = {"stable_id": "x", "wall_span": [100.0, 105.0], "timings": {}}
    _record_latencies(result, yielded_at=99.0, received_at=145.0, tally=tally)
    assert result["timings"]["return_s"] == pytest.approx(40.0)
    assert result["timings"]["dispatch_s"] == pytest.approx(1.0)


def test_clock_skew_is_counted_not_clamped() -> None:
    """if skew is clamped away then a skewed split reads as a real measurement"""
    tally = FarmTally()
    # Container clock ahead of the Mac: its start precedes our own yield.
    result = {"stable_id": "x", "wall_span": [90.0, 98.0], "timings": {}}
    _record_latencies(result, yielded_at=100.0, received_at=112.0, tally=tally)
    assert tally.clock_skew_calls == 1
    assert result["timings"]["dispatch_s"] < 0
    # The sum stays exact regardless of skew, which is the point.
    assert tally.round_trip_s == pytest.approx(12.0)


def test_farm_prefetch_constants_match_the_module() -> None:
    """if the farm's constants drift from the module then flags lie about defaults"""
    from apps.vocals import prefetch

    # Duplicated only because Modal imports the farm inside a container with no
    # `apps` package, so these cannot be imported at module scope there.
    assert PREFETCH_DEPTH == prefetch.DEFAULT_DEPTH
    assert PREFETCH_WORKERS == prefetch.DEFAULT_WORKERS
    assert PREFETCH_MAX_BYTES == prefetch.DEFAULT_MAX_BYTES


def test_serial_control_arm_is_reachable() -> None:
    """if depth 1 is rejected then the prefetch has no control arm to A/B against"""
    from pathlib import Path

    from apps.vocals.prefetch import read_ahead

    # Not a perf assertion, just that the serial configuration is legal.
    stream = read_ahead([Path(__file__)], depth=1, workers=1)
    assert next(stream).path == Path(__file__)
    stream.close()


#----- input-pipeline benchmark projection ------------------------------------
# The arm rates in that benchmark are a 100%-evicted worst case. These pin the
# projection onto the REAL gap, which is what corrected the headline claim from
# "the feeder rate is binding" to "the feeder freezes the whole pipeline".


def test_serial_feeder_already_clears_the_cap_on_the_real_mix() -> None:
    """if the mixed-population rate is not projected then a worst case reads as typical"""
    from scripts.bench.input_pipeline_bench import project_gap_impact

    # Measured: ~2.2s per cold evicted file, ~0.005s per resident file.
    out = project_gap_impact(
        cold_item_s=2.24, warm_item_s=0.0046,
        evicted=174, resident=815, gap_bytes=9_160_000_000,
    )
    assert out["evicted_pct"] == pytest.approx(17.6, abs=0.1)
    # The point of the whole projection: rate was never the constraint here.
    assert out["serial_implied_containers_mixed"] > 10


def test_freeze_scales_with_evicted_count_not_batch_size() -> None:
    """if freeze tracked batch size then adding resident files would fake a cost"""
    from scripts.bench.input_pipeline_bench import project_gap_impact

    few = project_gap_impact(2.0, 0.005, evicted=10, resident=100, gap_bytes=1)
    many_resident = project_gap_impact(
        2.0, 0.005, evicted=10, resident=10_000, gap_bytes=1
    )
    assert few["pipeline_freeze_s"] == many_resident["pipeline_freeze_s"] == 20.0
    more_evicted = project_gap_impact(
        2.0, 0.005, evicted=100, resident=100, gap_bytes=1
    )
    assert more_evicted["pipeline_freeze_s"] == 200.0


def test_empty_gap_is_refused_not_divided_by_zero() -> None:
    """if an empty gap divides by zero then the projection dies mid-report"""
    from scripts.bench.input_pipeline_bench import project_gap_impact

    with pytest.raises(SystemExit, match="empty gap"):
        project_gap_impact(2.0, 0.005, evicted=0, resident=0, gap_bytes=0)


#----- production preset -------------------------------------------------------
# Switching the default model is cheap to get wrong in two specific ways: the
# weights silently not being in the image (a per-container download), and the
# cache stamp naming a model that did not run.


def test_default_preset_model_is_baked() -> None:
    """if the default model is not baked then every cold container downloads it"""
    from scripts.modal_vocal_farm import BAKED_MODELS, DEFAULT_PRESET, PRESETS

    assert PRESETS[DEFAULT_PRESET].model in BAKED_MODELS


def test_unbaked_model_is_refused_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """if an unbaked model runs unflagged then cold downloads are paid silently"""
    from scripts import modal_vocal_farm as farm

    # Exercised against a bake list with the model removed, NOT against whichever
    # preset happens to be unbaked today. Naming one is what rotted this test:
    # it pinned "htdemucs_ft-ov0.25", dfde3eb3 baked htdemucs_ft, the guard then
    # correctly stopped firing, and the assertion quietly became untrue. Nothing
    # noticed because the module needs `modal` and CI never collected the file.
    tag = "htdemucs_ft-ov0.25"
    model = farm.PRESETS[tag].model
    monkeypatch.setattr(
        farm, "BAKED_MODELS", tuple(m for m in farm.BAKED_MODELS if m != model)
    )

    with pytest.raises(SystemExit, match="not baked into the image"):
        farm._resolve_preset(tag, allow_unbaked=False)
    # ...and is still reachable when the caller accepts the cost.
    assert farm._resolve_preset(tag, allow_unbaked=True).model == model


def test_every_baked_model_has_a_preset() -> None:
    """if a model is baked with no preset then image weight is paid for nothing"""
    from scripts.modal_vocal_farm import BAKED_MODELS, PRESETS

    have = {preset.model for preset in PRESETS.values()}
    assert set(BAKED_MODELS) <= have


# NOTE: an earlier revision added test_every_preset_model_is_baked here, pinning
# {preset models} == set(BAKED_MODELS). It was removed on review. Combined with
# test_every_baked_model_has_a_preset it outlawed the very design --allow-unbaked-model
# exists to serve: before dfde3eb3, scripts/modal_vocal_farm.py deliberately shipped
# htdemucs_ft presets with htdemucs_ft UNBAKED ("htdemucs_ft is deliberately absent"),
# gated behind that flag. The invariant would have been RED before dfde3eb3 and turned
# GREEN by it -- the opposite of the drift-detection it was claimed to provide -- and
# would have made the flag permanently unreachable in production. Whether to retire
# --allow-unbaked-model is a design decision, not a side effect of a test repair.


def test_old_rungs_stay_selectable() -> None:
    """if the old ladder is dropped then a quality regression cannot be A/B tested"""
    from scripts.modal_vocal_farm import PRESETS, _resolve_preset

    for tag in ("htdemucs-ov0.1", "htdemucs-ov0.25", "htdemucs-ov0.5"):
        assert tag in PRESETS
        assert _resolve_preset(tag, allow_unbaked=False).tag == tag


def test_preset_stamp_records_the_model_that_actually_ran() -> None:
    """if the stamp does not name the real model then selective re-runs are blind"""
    from scripts.modal_vocal_farm import (
        DEFAULT_PRESET,
        PRESETS,
        region_params,
    )

    preset = PRESETS[DEFAULT_PRESET]
    # cache 'source' is a family marker frozen at "demucs-htdemucs", so these
    # two are the only honest record of which model produced the regions.
    assert preset.stamp()["model"] == "hdemucs_mmi"
    assert region_params(preset)["model"] == "hdemucs_mmi"


def test_off_ladder_presets_do_not_invent_a_rung() -> None:
    """if an off-ladder model claims a rung then it fakes a ladder measurement"""
    from scripts.modal_vocal_farm import PRESETS

    for preset in PRESETS.values():
        if preset.model == "hdemucs_mmi":
            assert preset.rung == 0, "hdemucs_mmi was never on the overlap ladder"
        else:
            assert preset.rung > 0


def test_benchmark_refuses_to_consume_evicted_files_by_default() -> None:
    """if the benchmark runs unflagged then a non-renewable pool is spent silently"""
    from scripts.bench.input_pipeline_bench import assert_consumption_allowed

    with pytest.raises(SystemExit, match="refusing to run"):
        assert_consumption_allowed(consume_evicted=False)
    assert_consumption_allowed(consume_evicted=True)  # explicit opt-in works


def test_gpu_floor_uses_audio_minutes_when_available() -> None:
    """if the floor scales by track count then it overstates by ~14%"""
    from scripts.bench.input_pipeline_bench import project_gap_impact

    # The real gap: 989 tracks, 4753.7 audio-min, verified against state.db.
    by_audio = project_gap_impact(
        2.24, 0.0046, evicted=174, resident=815, gap_bytes=9_160_000_000,
        gap_audio_s=4753.7 * 60,
    )
    by_tracks = project_gap_impact(
        2.24, 0.0046, evicted=174, resident=815, gap_bytes=9_160_000_000,
    )
    assert by_audio["gpu_floor_basis"] == "audio-seconds"
    assert by_audio["gpu_floor_min"] == pytest.approx(11.7, abs=0.2)
    assert by_tracks["gpu_floor_min"] == pytest.approx(13.3, abs=0.2)
    # Track-count scaling assumes the sample's mean duration and overstates.
    assert by_tracks["gpu_floor_min"] > by_audio["gpu_floor_min"]
