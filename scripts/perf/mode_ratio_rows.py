"""Ledger rows for the PERFMODE-15 Gig-vs-Trackify capture (capture_mode_ratios).

Extracted from `capture_mode_ratios.py` (600-line ratchet) when round 8 widened
the ratio rows to the PERFMODE-14 process family: every Chromium process plus
the engine and its descendants (stem workers). The ratio KPIs are computed over
that whole family; the browser-only ratios are kept in the note as a diagnostic
so rows stay comparable with the browser-family rows written before round 8.
"""

from __future__ import annotations

from typing import Any

from scripts.perf.capture_kpi_ledger import CaptureMeta, build_row
from scripts.perf.capture_library_mode import (
    PERFMODE14_SCORER,
    perfmode14_require_scored_sample_count,
)
from scripts.perf.trackify_leak_series import (
    ADR_REF,
    CHECKPOINT_INTERVAL_S,
    CHECKPOINT_SAMPLES,
    RAW_SLOPE_KPI,
    RETAINED_SLOPE_KPI,
    LeakSeries,
)

GIG_DECKS = 4

#: Rows whose ratio is over the PERFMODE-14 family carry this, and only these
#: rows can satisfy the PERFMODE-15 ratio clauses (the requirement reader in
#: tests/scripts/test_trackify_mode_requirement.py ignores ratio rows without it).
PROCESS_FAMILY = "chromium+engine"

RATIO_METHOD = (
    "process-tree physical-footprint/CPU sampling of the PERFMODE-14 process family "
    "(capture_library_mode.py's attribution): every Chromium process the Playwright "
    "launcher spawned, plus the engine serving the page and every descendant of it "
    "(stem workers), found by ancestry, never by name. The engine root is the pid the "
    "engine reports in /api/v1/build-info, verified local by PERFMODE-14's "
    "engineRootPids (loopback origin, owns the listening port, runs engine code) and "
    "pinned for the whole capture. Footprint is phys_footprint (proc_pid_rusage), CPU "
    "is psutil's per-process delta, median of samples at >=6 ticks per mode (PERFMODE-14 scorer)"
)
BROWSER_METHOD = (
    "process-tree physical-footprint/CPU sampling of the Playwright-launched Chromium running "
    "Trackify (PERFMODE-15) -- browser/renderer process family only, not the engine"
)
RETAINED_METHOD = (
    f"quiescent-checkpoint baselines ({ADR_REF}): every {CHECKPOINT_INTERVAL_S} s of played "
    "time Trackify autoplay is turned off, deck 1 unloaded, the main isolate garbage collected "
    "and a critical memory-pressure notification sent, then the median of "
    f"{CHECKPOINT_SAMPLES} process-tree phys_footprint samples of the Playwright-launched "
    "Chromium (browser/renderer family only) is the baseline; the KPI is the least-squares "
    "slope of the baselines against played time"
)

_ENGINE_FIELDS = ("engine_footprint_mb", "engine_cpu_percent", "engine_pid_count_max")


def validate_gig_stable_ids(ids: object) -> list[str]:
    """Exactly `GIG_DECKS` non-empty string ids, or a RuntimeError naming the defect."""
    if not isinstance(ids, list) or len(ids) != GIG_DECKS:
        raise RuntimeError(f"GIG_STABLE_IDS must hold exactly {GIG_DECKS} ids, got {ids!r}")
    if not all(isinstance(stable_id, str) and stable_id for stable_id in ids):
        raise RuntimeError(f"GIG_STABLE_IDS holds a non-string or empty id: {ids!r}")
    return ids


def _require_engine_family(mode: str, phase: dict[str, float]) -> None:
    """A phase without a sampled engine is a browser-only capture: refuse it as a ratio row."""
    missing = [field for field in _ENGINE_FIELDS if field not in phase]
    if missing:
        raise SystemExit(
            f"{mode} phase has no engine family ({', '.join(missing)} missing); refusing a "
            "browser-only capture as a PERFMODE-15 ratio row, which PERFMODE-14's family includes"
        )
    if phase["engine_footprint_mb"] <= 0 or phase["engine_pid_count_max"] < 1:
        raise SystemExit(
            f"{mode} phase engine family read {phase['engine_footprint_mb']} MB over "
            f"{phase['engine_pid_count_max']} pid(s); a zero engine is unmeasured, not cheap"
        )


def _savings(trackify: float, gig: float) -> float:
    return 1.0 - (trackify / gig)


def gig_baseline_rows(
    gig: dict[str, float],
    trackify: dict[str, float],
    gig_stable_ids: object,
    meta: CaptureMeta,
) -> list[dict[str, Any]]:
    """The footprint and CPU ratio rows for one Gig-then-Trackify capture.

    Refuses a capture that cannot name its four Gig decks, has a zero
    denominator, or did not sample the engine family in both phases: none is an
    auditable PERFMODE-15 measurement.
    """
    stable_ids = validate_gig_stable_ids(gig_stable_ids)
    for mode, phase in (("gig", gig), ("trackify", trackify)):
        _require_engine_family(mode, phase)
        try:
            perfmode14_require_scored_sample_count(
                int(phase["sample_count"]), mode=mode, field="footprint_samples_mb"
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if gig["footprint_mb"] <= 0 or gig["cpu_percent"] <= 0:
        raise SystemExit("gig baseline denominators missing or zero; refusing ratio write")
    if gig["browser_footprint_mb"] <= 0 or gig["browser_cpu_percent"] <= 0:
        raise SystemExit("gig browser-family denominators missing or zero; refusing ratio write")
    footprint_ratio = _savings(trackify["footprint_mb"], gig["footprint_mb"])
    cpu_ratio = _savings(trackify["cpu_percent"], gig["cpu_percent"])
    note = (
        f"PERFMODE-15 trackify ratios, process_family={PROCESS_FAMILY}; "
        f"gig_fp={gig['footprint_mb']:.2f}MB trackify_fp={trackify['footprint_mb']:.2f}MB "
        f"gig_cpu={gig['cpu_percent']:.2f}% trackify_cpu={trackify['cpu_percent']:.2f}% "
        f"engine: gig_fp={gig['engine_footprint_mb']:.2f}MB "
        f"trackify_fp={trackify['engine_footprint_mb']:.2f}MB "
        f"gig_cpu={gig['engine_cpu_percent']:.2f}% trackify_cpu={trackify['engine_cpu_percent']:.2f}% "
        f"gig_pids_max={int(gig['engine_pid_count_max'])} "
        f"trackify_pids_max={int(trackify['engine_pid_count_max'])}; "
        "diagnostic browser_only_footprint_ratio="
        f"{_savings(trackify['browser_footprint_mb'], gig['browser_footprint_mb']):.4f} "
        "browser_only_cpu_ratio="
        f"{_savings(trackify['browser_cpu_percent'], gig['browser_cpu_percent']):.4f}; "
        f"samples_gig={int(gig['sample_count'])} "
        f"samples_trackify={int(trackify['sample_count'])} "
        f"gig_stable_ids={stable_ids!r}"
    )
    rows = [
        build_row(
            kpi=kpi,
            value=round(value, 4),
            unit="ratio",
            method=RATIO_METHOD,
            meta=meta,
            note=note,
            measured=True,
        )
        for kpi, value in (
            ("trackify_mode_footprint_ratio", footprint_ratio),
            ("trackify_mode_cpu_ratio", cpu_ratio),
        )
    ]
    for row in rows:
        row["process_family"] = PROCESS_FAMILY
        row["scorer"] = PERFMODE14_SCORER
    return rows


def leak_rows(series: LeakSeries, meta: CaptureMeta) -> list[dict[str, Any]]:
    """The gating retained-slope row and the diagnostic raw-slope row from one run."""
    retained = series.retained_slope_mb_per_10min()
    raw = series.raw_slope_mb_per_10min()
    summary = f"{series.summary()} retained_slope={retained:.4f} raw_slope={raw:.4f}"
    return [
        build_row(
            kpi=RETAINED_SLOPE_KPI,
            value=round(retained, 4),
            unit="MB/10min",
            method=RETAINED_METHOD,
            meta=meta,
            note=f"PERFMODE-15 trackify 1h retained (quiescent-baseline) slope, {ADR_REF}; {summary}",
            measured=True,
        ),
        build_row(
            kpi=RAW_SLOPE_KPI,
            value=round(raw, 4),
            unit="MB/10min",
            method=BROWSER_METHOD,
            meta=meta,
            note=(
                "PERFMODE-15 trackify raw playing-footprint slope, diagnostic: includes the playing "
                f"track's decoded audio, does not gate ({ADR_REF}); checkpointed run, not comparable "
                f"with uncheckpointed raw rows; {summary}"
            ),
            measured=True,
        ),
    ]
