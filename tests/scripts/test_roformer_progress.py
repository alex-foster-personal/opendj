"""Regression tests for the RoFormer runner's agent-native progress reducer.

Everything here is pure arithmetic over hand-built Dict snapshots -- no Modal
import, no network, no GPU. The two functions that DO touch Modal
(read_state, containers_live_from_modal_cli) are thin I/O wrappers around
this math and are exercised by the live smoke test instead (see
scripts/bench/ROFORMER-SPIKE.md / the task's own live-smoke requirement).

Single-line intent, in the repo's regression style:
  - if reduce_state loses the run-meta entry then every downstream stat is meaningless
  - if a track counts toward the wrong phase bucket then done/in_flight/failed all lie
  - if pct or eta_s divide by zero on an empty run then status crashes instead of reading 0
  - if the percentile function's p50/p95 disagree with a hand-computed value then eta_s and
    the straggler warning are both wrong
  - if load_overhead_pct ignores the task's own sum(load_s)/sum(load_s+sep_s) formula then the
    warm-container warning fires on the wrong signal
  - if any of the three warnings fires off its stated threshold, or a fourth warning appears,
    then the status JSON accuses an agent of the wrong inefficiency
  - if containers_live_from_apps attributes another run's tasks to this app_id then the ETA
    and the under-parallelised warning are both borrowing someone else's concurrency
"""
from __future__ import annotations

import pytest

from scripts import roformer_progress as prog

RUN_ID = "20260731T000000-1"


def _snapshot(
    tracks: dict[str, dict], *, total: int | None = None, max_containers: int = 6
) -> prog.RunSnapshot:
    if total is None:
        total = len(tracks)
    raw = {
        prog.RUN_META_KEY: {
            "run_id": RUN_ID,
            "total": total,
            "config_tag": "verify-progress-smoke",
            "checkpoint": "model_mel_band_roformer_ep_3005_sdr_11.4360.ckpt",
            "max_containers": max_containers,
            "app_id": "ap-test123",
            "started_ts": 1000.0,
        },
        **tracks,
    }
    return prog.reduce_state(raw)


def _done(load_s: float, sep_s: float, encode_s: float, duration_s: float, gpu_id: str = "t-1") -> dict:
    return {
        "phase": "done", "ts": 1010.0, "gpu_id": gpu_id,
        "load_s": load_s, "sep_s": sep_s, "encode_s": encode_s, "duration_s": duration_s,
    }


# ----- reduce_state -------------------------------------------------------------


def test_reduce_state_requires_run_meta_entry() -> None:
    with pytest.raises(ValueError, match="__run__"):
        prog.reduce_state({"trk1": {"phase": "queued", "ts": 1.0}})


def test_reduce_state_splits_meta_from_tracks() -> None:
    snap = _snapshot({"trk1": {"phase": "queued", "ts": 1.0}})
    assert snap.run_id == RUN_ID
    assert snap.total == 1
    assert list(snap.tracks) == ["trk1"]
    assert prog.RUN_META_KEY not in snap.tracks


# ----- phase counting -------------------------------------------------------------


def test_phase_counts_and_settled() -> None:
    snap = _snapshot(
        {
            "a": {"phase": "queued", "ts": 1.0},
            "b": {"phase": "loading", "ts": 1.0, "gpu_id": "t-1"},
            "c": {"phase": "separating", "ts": 1.0, "gpu_id": "t-2"},
            "d": {"phase": "encoding", "ts": 1.0, "gpu_id": "t-2"},
            "e": _done(2.0, 30.0, 1.0, 60.0),
            "f": {"phase": "failed", "ts": 1.0, "gpu_id": "t-3", "error": "boom"},
        },
        total=6,
    )
    assert prog.done_count(snap) == 1
    assert prog.failed_count(snap) == 1
    assert prog.in_flight_count(snap) == 3  # loading + separating + encoding
    assert prog.settled_count(snap) == 2
    assert prog.pct_complete(snap) == pytest.approx(100.0 * 2 / 6, abs=0.05)


def test_pct_complete_on_empty_total_is_zero_not_a_crash() -> None:
    snap = _snapshot({}, total=0)
    assert prog.pct_complete(snap) == 0.0


# ----- percentile -------------------------------------------------------------


def test_percentile_matches_hand_computed_values() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert prog._percentile(values, 50) == 3.0
    assert prog._percentile(values, 0) == 1.0
    assert prog._percentile(values, 100) == 5.0
    assert prog._percentile(values, 95) == pytest.approx(4.8)


def test_percentile_empty_and_single_value() -> None:
    assert prog._percentile([], 50) == 0.0
    assert prog._percentile([7.0], 95) == 7.0


def test_percentile_rejects_out_of_range_pct() -> None:
    with pytest.raises(ValueError):
        prog._percentile([1.0, 2.0], 150)


# ----- eta_s -------------------------------------------------------------


def test_eta_s_zero_when_nothing_done_yet() -> None:
    snap = _snapshot({"a": {"phase": "loading", "ts": 1.0, "gpu_id": "t-1"}}, total=2)
    assert prog.eta_s(snap, containers_live=1) == 0.0


def test_eta_s_zero_when_nothing_remaining() -> None:
    snap = _snapshot({"a": _done(1.0, 10.0, 1.0, 60.0)}, total=1)
    assert prog.eta_s(snap, containers_live=1) == 0.0


def test_eta_s_uses_median_done_duration_over_live_concurrency() -> None:
    # Two done tracks: 12s and 20s total. Median = 16s. 4 remaining, 2 live
    # containers -> 4/2 * 16 = 32s.
    snap = _snapshot(
        {
            "a": _done(2.0, 9.0, 1.0, 60.0),  # 12s
            "b": _done(2.0, 17.0, 1.0, 60.0),  # 20s
        },
        total=6,
    )
    assert prog.eta_s(snap, containers_live=2) == pytest.approx(32.0)


def test_eta_s_falls_back_to_max_containers_when_none_live_yet() -> None:
    snap = _snapshot({"a": _done(2.0, 8.0, 0.0, 60.0)}, total=3, max_containers=5)
    # 2 remaining, no live containers reported yet -> fall back to max_containers=5
    assert prog.eta_s(snap, containers_live=0) == pytest.approx(2 / 5 * 10.0)


# ----- cost -------------------------------------------------------------


def test_gpu_s_spent_sums_only_done_tracks() -> None:
    snap = _snapshot(
        {
            "a": _done(2.0, 10.0, 1.0, 60.0),  # 13s
            "b": {"phase": "loading", "ts": 1.0, "gpu_id": "t-1", "load_s": 99.0},  # ignored
        },
        total=2,
    )
    assert prog.gpu_s_spent(snap) == pytest.approx(13.0)


def test_usd_spent_uses_h100_rate() -> None:
    assert prog.usd_spent(1000.0) == pytest.approx(1.097, abs=1e-6)


# ----- load_overhead_pct -------------------------------------------------------------


def test_load_overhead_pct_matches_task_formula() -> None:
    snap = _snapshot(
        {
            "a": _done(2.0, 8.0, 1.0, 60.0),  # load=2, sep=8
            "b": _done(3.0, 7.0, 1.0, 60.0),  # load=3, sep=7
        },
        total=2,
    )
    # sum(load)=5, sum(load+sep)=20 -> 25.0%
    assert prog.load_overhead_pct(snap) == pytest.approx(25.0)


def test_load_overhead_pct_zero_with_no_done_tracks() -> None:
    snap = _snapshot({"a": {"phase": "loading", "ts": 1.0, "gpu_id": "t-1"}}, total=1)
    assert prog.load_overhead_pct(snap) == 0.0


# ----- sep_s per audio minute -------------------------------------------------------------


def test_sep_s_per_audio_min_percentiles() -> None:
    # 60s of audio, 30s to separate -> 30 sep-s/audio-min.
    # 120s of audio, 30s to separate -> 15 sep-s/audio-min.
    snap = _snapshot(
        {
            "a": _done(1.0, 30.0, 1.0, 60.0),
            "b": _done(1.0, 30.0, 1.0, 120.0),
        },
        total=2,
    )
    assert prog.p50_sep_s_per_audio_min(snap) == pytest.approx(22.5)
    assert prog.p95_sep_s_per_audio_min(snap) == pytest.approx(29.25)


def test_sep_s_per_audio_min_zero_with_no_done_tracks() -> None:
    snap = _snapshot({}, total=0)
    assert prog.p50_sep_s_per_audio_min(snap) == 0.0
    assert prog.p95_sep_s_per_audio_min(snap) == 0.0


# ----- warnings -------------------------------------------------------------


def test_warning_fires_on_high_load_overhead() -> None:
    warnings = prog.build_warnings(
        load_overhead=31.0, containers_live=10, remaining=0, in_flight=0, p50=1.0, p95=1.0,
        stems_over_source=0, worst_ratio=1.0,
    )
    assert warnings == ["model load dominating, use warm containers (modal.Cls) or memory snapshots"]


def test_warning_does_not_fire_at_exactly_30_pct() -> None:
    warnings = prog.build_warnings(
        load_overhead=30.0, containers_live=10, remaining=0, in_flight=0, p50=1.0, p95=1.0,
        stems_over_source=0, worst_ratio=1.0,
    )
    assert warnings == []


def test_warning_fires_on_underparallelised_pool() -> None:
    # remaining=20, containers_live=3 < min(10, 20)=10, in_flight>0.
    warnings = prog.build_warnings(
        load_overhead=0.0, containers_live=3, remaining=20, in_flight=3, p50=1.0, p95=1.0,
        stems_over_source=0, worst_ratio=1.0,
    )
    assert warnings == ["under-parallelised vs 10-GPU concurrency cap"]


def test_warning_skips_underparallelised_when_nothing_in_flight() -> None:
    # Even with containers_live < pool cap, no warning if nothing is running
    # (e.g. between polls, or the run just hasn't dispatched yet).
    warnings = prog.build_warnings(
        load_overhead=0.0, containers_live=0, remaining=20, in_flight=0, p50=1.0, p95=1.0,
        stems_over_source=0, worst_ratio=1.0,
    )
    assert warnings == []


def test_warning_uses_remaining_not_pool_cap_when_remaining_is_smaller() -> None:
    # remaining=2 < H100_POOL_CAP=10: min(10, 2) = 2. containers_live=1 < 2.
    warnings = prog.build_warnings(
        load_overhead=0.0, containers_live=1, remaining=2, in_flight=1, p50=1.0, p95=1.0,
        stems_over_source=0, worst_ratio=1.0,
    )
    assert warnings == ["under-parallelised vs 10-GPU concurrency cap"]


def test_warning_fires_on_straggler_ratio() -> None:
    warnings = prog.build_warnings(
        load_overhead=0.0, containers_live=10, remaining=0, in_flight=0, p50=10.0, p95=31.0,
        stems_over_source=0, worst_ratio=1.0,
    )
    assert warnings == ["straggler tracks, investigate longest audio"]


def test_warning_skips_straggler_ratio_when_p50_is_zero() -> None:
    warnings = prog.build_warnings(
        load_overhead=0.0, containers_live=10, remaining=0, in_flight=0, p50=0.0, p95=0.0,
        stems_over_source=0, worst_ratio=1.0,
    )
    assert warnings == []


def test_warning_fires_on_stems_over_source_size() -> None:
    warnings = prog.build_warnings(
        load_overhead=0.0, containers_live=10, remaining=0, in_flight=0, p50=1.0, p95=1.0,
        stems_over_source=2, worst_ratio=1.034,
    )
    assert warnings == ["2 stems over source size, worst +3.4%"]


def test_warning_skips_stems_over_source_size_when_zero() -> None:
    warnings = prog.build_warnings(
        load_overhead=0.0, containers_live=10, remaining=0, in_flight=0, p50=1.0, p95=1.0,
        stems_over_source=0, worst_ratio=1.0,
    )
    assert warnings == []


def test_all_four_warnings_can_fire_together() -> None:
    warnings = prog.build_warnings(
        load_overhead=50.0, containers_live=1, remaining=5, in_flight=1, p50=10.0, p95=40.0,
        stems_over_source=1, worst_ratio=1.05,
    )
    assert len(warnings) == 4


# ----- size-vs-source target aggregation -------------------------------------


def test_stems_over_source_count_ignores_flac_control_tracks() -> None:
    """A flac/control track writes size_ratio_max=None (size is exempt
    there) -- it must not count as "fits" or "over"."""
    snap = _snapshot(
        {
            "a": {**_done(1.0, 10.0, 1.0, 60.0), "size_ratio_max": 1.05},
            "b": {**_done(1.0, 10.0, 1.0, 60.0), "size_ratio_max": None},
            "c": {**_done(1.0, 10.0, 1.0, 60.0), "size_ratio_max": 0.9},
        },
        total=3,
    )
    assert prog.stems_over_source_count(snap) == 1
    assert prog.worst_size_ratio(snap) == pytest.approx(1.05)


def test_worst_size_ratio_defaults_to_one_with_no_data() -> None:
    snap = _snapshot({}, total=0)
    assert prog.worst_size_ratio(snap) == 1.0
    assert prog.stems_over_source_count(snap) == 0


def test_build_status_surfaces_stems_over_source_fields() -> None:
    snap = _snapshot(
        {"a": {**_done(1.0, 10.0, 1.0, 60.0), "size_ratio_max": 1.1}}, total=1
    )
    status = prog.build_status(snap, containers_live=1)
    assert status["stems_over_source_size"] == 1
    assert status["worst_size_ratio_pct"] == pytest.approx(10.0)
    assert any("stems over source size" in w for w in status["warnings"])


# ----- containers_live_from_apps -------------------------------------------------------------


def test_containers_live_from_apps_matches_by_app_id() -> None:
    apps = [
        {"app_id": "ap-other", "tasks": "6", "state": "ephemeral"},
        {"app_id": "ap-mine", "tasks": "2", "state": "ephemeral"},
    ]
    assert prog.containers_live_from_apps(apps, "ap-mine") == 2


def test_containers_live_from_apps_zero_for_unknown_app_id() -> None:
    apps = [{"app_id": "ap-other", "tasks": "6", "state": "ephemeral"}]
    assert prog.containers_live_from_apps(apps, "ap-mine-not-listed") == 0


def test_containers_live_from_apps_does_not_borrow_another_runs_tasks() -> None:
    # Two concurrent runs of the SAME app (mdt-roformer-spike) must not
    # attribute each other's containers -- exactly why app_id, not app name,
    # scopes the count.
    apps = [
        {"app_id": "ap-run-a", "tasks": "6", "state": "ephemeral"},
        {"app_id": "ap-run-b", "tasks": "2", "state": "ephemeral"},
    ]
    assert prog.containers_live_from_apps(apps, "ap-run-b") == 2
    assert prog.containers_live_from_apps(apps, "ap-run-a") == 6


# ----- build_status (integration of the pure pieces) -------------------------------------------------------------


def test_build_status_shape_has_all_required_keys() -> None:
    snap = _snapshot(
        {
            "a": _done(2.0, 10.0, 1.0, 60.0),
            "b": {"phase": "separating", "ts": 1.0, "gpu_id": "t-2", "load_s": 2.0},
        },
        total=4,
    )
    status = prog.build_status(snap, containers_live=1)
    required = {
        "done", "total", "pct", "in_flight", "failed", "eta_s", "gpu_s_spent",
        "usd_spent", "containers_live", "load_overhead_pct",
        "p50_sep_s_per_audio_min", "p95_sep_s_per_audio_min", "warnings",
    }
    assert required <= status.keys()
    assert status["done"] == 1
    assert status["in_flight"] == 1
    assert status["failed"] == 0
    assert status["total"] == 4
    assert isinstance(status["warnings"], list)
