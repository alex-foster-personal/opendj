"""perf-lab analyzer: the planted controls are scored against what was planted,
OS windows count churn honestly, and latency percentiles need every rep and a
silent idle control before an audible time counts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.perf.perflab_support import LAB, _load

an = _load("analyze")


def test_burner_share_uses_every_renderer_main_sample() -> None:
    planted = {"planted": [{"kind": "cpu_burner", "burned_ms": 250, "window_ms": 1000}]}
    # 100 renderer-main samples (idle included), 25 of them in the burner
    js: dict[str, Any] = {"_named": {"perflabPlantedBurner x": 25}, "main_thread_all_samples": 100}
    score = lambda: an.score_controls({"phases": {"control-burner": {"js": js, "secs": 10}}}, planted)["cpu_burner"]  # noqa: E731
    assert score()["measured_share"] == 0.25 and score()["score"] == 1.0  # control: found, right size
    js["_named"] = {"perflabPlantedBurner x": 50}
    assert score()["score"] == 0.0  # twice the planted share is wrong by 100%
    js["_named"] = {"otherFunction x": 25}
    assert score()["found_by_name"] is False and score()["score"] == 0.0
    js.pop("main_thread_all_samples")
    assert score()["score"] is None  # no denominator: unmeasured, not diluted


def test_retained_buffer_is_scored_on_the_array_buffer_allocator() -> None:
    r = an.score_retained_arraybuffer({"partition_alloc/partitions/array_buffer": 64.0, "v8/main/heap": 120.0}, 64.0)
    assert r["score"] == 1.0 and r["array_buffer_growth"] == ("partition_alloc/partitions/array_buffer", 64.0)
    assert an.score_retained_arraybuffer({"partition_alloc/partitions/array_buffer": 32.0}, 64.0)["score"] == 0.5
    assert (
        an.score_retained_arraybuffer({"v8/main/heap": 64.0}, 64.0)["score"] is None
    )  # unrelated growth earns nothing


def test_js_leak_is_scored_by_name_and_count() -> None:
    planted = {"planted": [{"kind": "js_leak", "count_per_cycle": 20000, "cycles_between_A_and_C": 12}]}
    found = [{"name": "Array", "count": 9}, {"name": "PerflabPlantedLeak", "count": 120000}]
    r = an.score_controls({"heap": {"leak_candidates": found}}, planted)["js_leak"]
    assert r == {"found_by_name": True, "rank": 2, "count": 120000, "expected_count": 120000, "score": 1.0}
    missed = an.score_controls({"heap": {"leak_candidates": found[:1]}}, planted)["js_leak"]
    assert missed["score"] == 0.0 and missed["found_by_name"] is False
    assert an.score_controls({"heap": {}}, planted)["js_leak"]["score"] is None  # no heap diff: unmeasured


def test_pci_is_discounted_by_control_recovery() -> None:
    phase = {"os": {"roles": {"chrome-renderer": {"cpu_pct": 50.0, "pss_mb": 100.0}}}}
    analysis: dict[str, Any] = {"phases": {"four-playing": phase}, "latency": {}, "heap": {}}
    planted = {"planted": [{"kind": "cpu_burner"}, {"kind": "js_leak"}]}
    full = an.coverage({**analysis, "controls": {"cpu_burner": {"score": 1.0}, "js_leak": {"score": 1.0}}}, planted)
    half = an.coverage({**analysis, "controls": {"cpu_burner": {"score": 1.0}, "js_leak": {"score": 0.5}}}, planted)
    lost = an.coverage({**analysis, "controls": {"cpu_burner": {"score": 1.0}, "js_leak": {"score": None}}}, planted)
    assert full["control_factor"] == 1.0 and half["control_factor"] == 0.5
    # an unrecovered planted control earns no credit, so PCI cannot read OK on a failed positive control
    assert lost["control_factor"] == 0.0 and lost["PCI"] == 0.0
    assert any(n.startswith("Control js_leak not recovered") for n in lost["notes"]), lost["notes"]
    assert full["PCI"] > 0 and half["PCI"] == pytest.approx(full["PCI"] / 2, abs=1e-3)
    none = an.coverage(analysis, {})
    assert none["PCI"] == pytest.approx(full["PCI"] * 0.8, abs=1e-3) and "No controls planted" in none["notes"][0]


def test_slice_self_time_is_thread_cpu_when_the_slice_has_it() -> None:
    assert an.self_cpu_ms(500_000_000, 500_000_000, 100_000_000) == (100.0, "thread_cpu")
    assert an.self_cpu_ms(500_000_000, 500_000_000, None) == (500.0, "wall")  # control: says it is wall
    assert an.self_cpu_ms(250_000_000, 500_000_000, 100_000_000) == (50.0, "thread_cpu")


def _proc(cpu: float, role: str = "chrome-renderer", child: float | None = 0.0) -> dict:
    return {"role": role, "cpu_s": cpu, "child_cpu_s": child, "threads": {"1": cpu}, "pss": 1048576}


def test_os_window_counts_births_and_voids_a_role_with_an_exit() -> None:
    ticks: list[dict[str, Any]] = [
        {"t": 0.0, "procs": {"10": _proc(0.0), "20": _proc(5.0), "40": _proc(1.0, "chrome-gpu")}},
        {"t": 1.0, "procs": {"10": _proc(0.5), "20": _proc(5.5), "30": _proc(0.25), "40": _proc(1.2, "chrome-gpu")}},
        {"t": 2.0, "procs": {"10": _proc(1.0), "30": _proc(0.5), "40": _proc(1.4, "chrome-gpu")}},
    ]
    w = an.os_window(ticks, {"interval_s": 1.0}, 0.0, 2.0)
    assert w["roles"]["chrome-renderer"]["cpu_s"] == 2.0  # 10: 1.0, 20: 0.5 seen, 30: born with 0.5
    assert w["roles"]["chrome-renderer"]["cpu_pct"] is None  # 20 exited: its tail is unseen
    assert abs(w["roles"]["chrome-gpu"]["cpu_pct"] - 20.0) < 1e-9  # control: an intact role is measured
    assert w["roles"]["chrome-renderer"]["pss_mb"] == 2.0
    ticks[-1]["procs"]["30"] = {**_proc(0.5), "pss": None}
    assert an.os_window(ticks, {"interval_s": 1.0}, 0.0, 2.0)["roles"]["chrome-renderer"]["pss_mb"] is None


@pytest.mark.parametrize("case", ["steady", "reaped", "unreported"])
def test_os_window_voids_cpu_when_a_helper_lived_and_died_between_ticks(case: str) -> None:
    end = {"10": _proc(1.0), "40": _proc(1.4, "chrome-gpu")}
    if case == "reaped":
        end["10"] = _proc(1.0, child=0.3)  # a parent reaped a child no tick saw
    elif case == "unreported":
        end["40"] = _proc(1.4, "chrome-gpu", child=None)
    ticks = [{"t": 0.0, "procs": {"10": _proc(0.0), "40": _proc(1.0, "chrome-gpu")}}, {"t": 2.0, "procs": end}]
    pct = {k: v["cpu_pct"] for k, v in an.os_window(ticks, {"interval_s": 1.0}, 0.0, 2.0)["roles"].items()}
    if case == "steady":  # control
        assert pct == pytest.approx({"chrome-renderer": 50.0, "chrome-gpu": 20.0})
    else:
        assert set(pct.values()) == {None}, pct


@pytest.mark.parametrize(
    ("ticks", "meta", "measured"),
    [
        ((0.0, 5.0, 10.0), {"interval_s": 5.0}, True),  # control
        ((4.0, 5.0), {"interval_s": 1.0}, False),  # started late and stopped early
        ((0.0, 5.0, 10.0), {}, False),  # no cadence recorded: coverage unprovable
    ],
)
def test_os_numbers_need_ticks_that_span_the_phase(ticks: tuple[float, ...], meta: dict, measured: bool) -> None:
    w = an.os_window([{"t": t, "procs": {"10": _proc(t / 10)}} for t in ticks], meta, 0.0, 10.0)
    assert ("roles" in w) == measured, w


def _latency(tmp: Path, rows: list[dict]) -> dict[str, Any]:
    (tmp / "latency.json").write_text(json.dumps({"rows": rows}))
    return dict(an.latency_attribution(None, tmp)["summary"])


def test_latency_percentiles_need_every_rep_and_enough_for_a_p99(tmp_path: Path) -> None:
    rows = [{"action": "play", "reflected_ms": 10.0 + i, "presented_ms": 50.0 if i >= 98 else 10.0} for i in range(100)]
    play = _latency(tmp_path, rows)["play"]
    assert play["presented_ms_p50"] == 10.0 and play["presented_ms_p99"] == 50.0  # nearest rank 99 of 100
    rows[0]["presented_ms"] = None  # one probe timeout voids both, never a p50 over fewer reps
    play = _latency(tmp_path, rows)["play"]
    assert play["presented_ms_p50"] is None and play["presented_ms_p99"] is None
    assert play["reflected_ms_p50"] == 59.5  # control: a complete metric still reports
    assert _latency(tmp_path, rows[:8])["play"]["reflected_ms_p99"] is None  # 8 reps cannot give a p99


def _class_a_rows(idle: dict) -> list[dict]:
    rows = [
        {"action": a, "destination_audible_ms": 15.0, "presented_ms": 20.0, "reflected_ms": 5.0}
        for a in ("play", "fader_cut", "fader_open", "eq_low_kill")
        for _ in range(100)
    ]
    return rows + [
        {"action": "idle_control", "paired": a, "destination_audible_ms": None, "presented_ms": None, **idle}
        for a in ("fader_cut", "eq_low_kill")
        for _ in range(100)
    ]


@pytest.mark.parametrize(
    ("idle", "void"),
    [
        ({"detectors_usable": ["level_drop", "low_kill"], "detectors_fired": []}, set()),  # control
        ({"detectors_usable": ["level_drop", "low_kill"], "detectors_fired": ["low_kill"]}, {"eq_low_kill"}),
        ({"detectors_usable": ["level_drop", "low_kill"], "detectors_fired": ["level_drop"]}, {"fader_cut"}),
        ({"detectors_usable": ["level_drop"], "detectors_fired": []}, {"eq_low_kill"}),  # never armed
        (None, {"fader_cut", "eq_low_kill"}),  # no idle control at all
    ],
)
def test_audible_detectors_count_only_when_idle_control_stays_silent(
    tmp_path: Path, idle: dict | None, void: set[str]
) -> None:
    rows = _class_a_rows(idle or {})
    if idle is None:
        rows = [r for r in rows if r["action"] != "idle_control"]
    summ = _latency(tmp_path, rows)
    for action in ("fader_cut", "eq_low_kill"):
        assert (summ[action]["presented_ms_p99"] is None) == (action in void), (action, summ[action])
        assert summ[action]["reflected_ms_p50"] == 5.0  # state latency is not the detector's
    assert summ["play"]["presented_ms_p99"] == 20.0  # sound appearing cannot be faked by the music


def test_each_action_is_validated_only_by_the_controls_on_its_own_passage(tmp_path: Path) -> None:
    """The driver replays each fall-timed action's passage for its own
    idle_control (paired). A control that listened before another action, an
    unpaired one, or fewer controls than presses validate nothing; a fire on
    one action's passage voids only that action."""
    silent = {"detectors_usable": ["level_drop", "low_kill"], "detectors_fired": []}
    rows = _class_a_rows(silent)
    summ = _latency(tmp_path, rows)
    assert summ["fader_cut"]["presented_ms_p99"] == 20.0 and summ["eq_low_kill"]["presented_ms_p99"] == 20.0
    # the music under the EQ kill's passage trips the low-kill detector with nothing pressed
    fired = [{**r, "detectors_fired": ["low_kill"]} if r.get("paired") == "eq_low_kill" else r for r in rows]
    summ = _latency(tmp_path, fired)
    assert summ["eq_low_kill"]["audible_void"].startswith("low_kill detector fired with nothing pressed on 100")
    assert summ["fader_cut"]["presented_ms_p99"] == 20.0  # control: the cut's own passage stayed silent
    # controls that listened before the cut only: the EQ kill has no control of its own
    only_cut = [r for r in rows if r.get("paired") != "eq_low_kill"]
    summ = _latency(tmp_path, only_cut)
    assert (
        summ["eq_low_kill"]["audible_void"] == "no idle_control rows paired with eq_low_kill to validate the detector"
    )
    assert summ["fader_cut"]["presented_ms_p99"] == 20.0
    # an older capture's unpaired controls validate nothing
    unpaired = [{k: v for k, v in r.items() if k != "paired"} for r in rows]
    summ = _latency(tmp_path, unpaired)
    assert summ["fader_cut"]["presented_ms_p99"] is None and summ["eq_low_kill"]["presented_ms_p99"] is None
    # one press without its control
    short = [r for i, r in enumerate(rows) if not (r.get("paired") == "fader_cut" and i == len(rows) - 101)]
    summ = _latency(tmp_path, short)
    assert summ["fader_cut"]["audible_void"] == (
        "99 idle_control reps paired with fader_cut for 100 presses: not every press has its control"
    )


def test_the_driver_replays_each_actions_passage_for_its_control() -> None:
    src = (LAB / "driver.mjs").read_text(encoding="utf-8")
    steps = src[src.index("const LATENCY_STEPS = [") :]
    steps = steps[: steps.index("];")]
    order = [
        "{ action: 'idle_control', paired: 'fader_cut', seek: true }",
        "{ action: 'fader_cut', seek: true }",
        "{ action: 'idle_control', paired: 'eq_low_kill', seek: true }",
        "{ action: 'eq_low_kill', seek: true }",
    ]
    assert [steps.index(o) for o in order] == sorted(steps.index(o) for o in order)
    loop = src[src.index("for (const { action, paired = null, seek } of LATENCY_STEPS) {") :]
    assert loop.index("if (seek) {") < loop.index("type: 'seek', deck, position_ms: LATENCY_SEEK_MS")
    assert "row.paired = paired;" in loop


def _series(pre_db: float, post: list[float], n_pre: int = 25) -> list[list[float]]:
    """A low/mid ratio series 20 ms apart: n_pre samples before the press, then post."""
    pts = [[-20.0 * (n_pre - i), pre_db] for i in range(n_pre)] + [[20.0 * i, r] for i, r in enumerate(post)]
    return [[t, 10.0 + t / 1000, r] for t, r in pts]


def test_low_kill_onset_times_the_first_sample_of_a_sustained_fall() -> None:
    assert an.low_kill_onset(_series(20.0, [20.0, 20.0] + [-6.0] * 10)) == (40.0, 10.04)
    assert an.low_kill_onset(_series(20.0, [20.0, -6.0, -6.0, -6.0, 20.0] * 4)) is None  # short dips
    assert an.low_kill_onset(_series(20.0, [5.0] * 20)) is None  # under the threshold
    edge = [20.0] + [1.0] * an.LOW_KILL_RUN  # control: exactly one run fires
    assert an.low_kill_onset(_series(20.0, edge)) == (20.0, 10.02)
    with pytest.raises(ValueError, match="baseline"):
        an.low_kill_onset(_series(20.0, [-6.0] * 10, n_pre=5))


def test_eq_kill_presentation_comes_from_the_output_clock(tmp_path: Path) -> None:
    kill = _series(20.0, [20.0, 20.0] + [-6.0] * 70)
    stamp = {"contextTime": 11.0, "performanceTime": 1500.0}
    idle = [
        {
            "action": "idle_control",
            "paired": "eq_low_kill",
            "low_ratio_series": _series(20.0, [20.0] * 72),
            "detectors_usable": ["level_drop"],
        }
    ]
    rows = [{"action": "eq_low_kill", "low_ratio_series": kill, "output_stamp": stamp}] * 100 + idle * 100
    eq = _latency(tmp_path, rows)["eq_low_kill"]
    assert eq["destination_audible_ms_p50"] == 40.0
    assert eq["presented_ms_p99"] == 540.0  # 10.04 s is 960 ms of context before the stamp's 11.0 s at 1500 ms
    stalled = {"contextTime": 10.0, "performanceTime": 1000.0}  # the output never reached the kill
    rows = [{"action": "eq_low_kill", "low_ratio_series": kill, "output_stamp": stalled}] * 100 + idle * 100
    assert _latency(tmp_path, rows)["eq_low_kill"]["presented_ms_p99"] is None


def test_latency_coverage_needs_presented_times_and_names_unexercised_paths() -> None:
    full = {a: {"presented_ms_p50": 20.0} for a in ("play", "fader_cut", "fader_open", "eq_low_kill")}
    notes: list[str] = []
    cells = an._latency_cells({"latency": {"summary": full}}, notes)
    assert cells["L1"] == 1.0 and cells["L2"] == round(3 / 9, 3)
    assert notes == ["latency L2: Class A paths not exercised: pause, cue, hot_cue, filter, stem_mute, crossfader"]
    cells = an._latency_cells({"latency": {"summary": {**full, "play": {"presented_ms_p50": None}}}})
    assert cells["L1"] == an.UNKNOWN and cells["L2"] == an.UNKNOWN


def test_a_capture_with_no_phases_still_renders_its_report() -> None:
    cov = an.coverage({"phases": {}, "latency": {}, "heap": {}}, {})
    assert cov["status"] == an.UNKNOWN and cov["cells"] == {}
    report = an.render_report({"run": "r", "coverage": cov, "phases": {}})
    assert "no phases" in report
