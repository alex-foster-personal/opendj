"""perf-lab ledger: record one profiling run's headline numbers, compare with the last.

Ongoing tracking is a procedure, not a remembered verdict: every full harness
run (driver.mjs + sampler.py + analyze.py) appends its headline KPIs to the
project's existing perf KPI ledger ((private path), through
scripts/perf/kpi_ledger_append.py), as `perflab_*` entries that share one
capture_id, and `compare` says what moved against the previous capture from
the SAME host class. Every entry carries the capture's revision, host,
Chromium version and fixture track ids in its note, and the stack manifest the
capture copied (manifest.py) as `manifest`. Two captures compare only when
their manifests agree on everything but the code under test (commit, dirty
diff, lockfiles; a lockfile change is reported as "deps differ") and they
loaded the same fixture tracks. A capture with no manifest is UNKNOWN. `add`
refuses a capture whose sampler did not span the driver's whole run
(sampler_span), so every ledgered capture was sampled from start to finish.

The CPU numbers come from a TRACED run (tracing roughly doubles idle renderer
CPU), so they are comparable only with other perf-lab captures, never with
untraced captures or abwin.mjs A/B windows.

Usage:
  python scripts/perf/lab/ledger.py add RUN_DIR [--host-class cloud-4vcpu]
  python scripts/perf/lab/ledger.py compare [--host-class ...] [--tolerance 0.15]

Exit codes for compare: 0 nothing moved beyond tolerance, 1 a metric regressed
or an audible or visual p99 is over the Class A budget (BUDGETS) whatever it moved from,
3 UNKNOWN (fewer than two comparable rows, a capture has no stack manifest, the
two manifests differ outside the code under test, the two use different fixture tracks,
a metric is missing in either row, or a Class A path has no budget check and
--class-a-scope budgeted was not passed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# this file runs as a script: the harness root first, so its own scripts package wins
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.perf.lab import manifest as stack_manifest

REPO = Path(__file__).resolve().parents[3]
LEDGER = REPO / "(private path)"
PREFIX = "perflab_"
UNITS = {"pci": "index", "heap_C_mb": "MB", "class_a_paths_unbudgeted": "count"}

# (metric, direction): +1 means bigger is better (coverage), -1 means smaller is better.
HEADLINE: list[tuple[str, int]] = [
    ("pci", +1),
    ("renderer_cpu_pct.idle-empty", -1),
    ("renderer_cpu_pct.loaded-stopped", -1),
    ("renderer_cpu_pct.four-playing", -1),
    ("engine_cpu_pct.idle-empty", -1),
    ("renderer_pss_mb.loaded-stopped", -1),
    ("latency_play_audible_p50_ms", -1),
    # the Class A contract is a p99 (<=30 ms audible), and a tail can regress
    # under an unchanged median
    ("latency_play_audible_p99_ms", -1),
    # the other Class A controls, timed against playing audio
    ("latency_fader_cut_audible_p99_ms", -1),
    ("latency_fader_open_audible_p99_ms", -1),
    ("latency_eq_low_kill_audible_p99_ms", -1),
    ("latency_fader_reflected_p50_ms", -1),
    # the visual half of the Class A contract (<=16 ms p99): the first frame
    # after the state reflected the press, per exercised control
    ("latency_play_visual_p99_ms", -1),
    ("latency_fader_cut_visual_p99_ms", -1),
    ("latency_fader_open_visual_p99_ms", -1),
    ("latency_eq_low_kill_visual_p99_ms", -1),
    ("heap_C_mb", -1),
]

# Absolute ceilings (ms) from the Class A contract (AGENTS.md, LATENCY-01:
# <=30 ms p99 audible, <=16 ms p99 visual). A capture over one fails compare even when it moved
# less than the relative tolerance, or had no comparable previous value.
BUDGETS: dict[str, float] = {
    k: 30.0
    for k in (
        "latency_play_audible_p99_ms",
        "latency_fader_cut_audible_p99_ms",
        "latency_fader_open_audible_p99_ms",
        "latency_eq_low_kill_audible_p99_ms",
    )
}
# and <=16 ms p99 visual for the same controls
BUDGETS.update(
    {
        k: 16.0
        for k in (
            "latency_play_visual_p99_ms",
            "latency_fader_cut_visual_p99_ms",
            "latency_fader_open_visual_p99_ms",
            "latency_eq_low_kill_visual_p99_ms",
        )
    }
)


def _role(phase: dict, role: str, key: str) -> float | None:
    v = phase.get("os", {}).get("roles", {}).get(role, {}).get(key)
    return round(v, 2) if isinstance(v, (int, float)) else None


def _capture_revision(override: str | None, recorded: str | None) -> tuple[str | None, str | None]:
    """(revision, None) for the commit a capture ran, or (None, why not). The
    driver records the --commit it was given, so --commit here may only restate
    it (a prefix either way); it fills the gap only for a driver.json without
    that field, and must then still be a commit id."""
    hexid = re.compile(r"[0-9a-f]{7,40}")
    if override is not None and not hexid.fullmatch(override):
        return None, f"--commit {override!r} is not a commit id of at least 7 hex digits"
    if recorded:
        if override and not (recorded.startswith(override) or override.startswith(recorded)):
            return None, f"--commit {override} is not the revision the driver profiled ({recorded})"
        return recorded, None
    if override:
        return override, None
    return None, "no capture revision: driver.json has no opts.commit; pass --commit SHA"


def capture_invalid(drv: dict[str, Any]) -> str | None:
    """Why a capture may not be ledgered, or None. Any driver error voids it,
    not only a FATAL one: a page exception that stops a workload, or a memory
    dump that failed, leaves numbers that read as a saving. Console errors are
    the app's own logging, recorded but not a verdict on the capture. A
    driver.json that is missing, or has no finished_at (the driver's last
    write), means the driver never completed: its partial files are no capture."""
    if not drv.get("finished_at"):
        return "driver.json is missing or has no finished_at: the driver never completed this capture"
    if drv.get("invalid"):
        return str(drv["invalid"])
    errors = drv.get("errors", [])
    if errors:
        return f"driver recorded {len(errors)} error(s), first: {str(errors[0])[:200]}"
    return None


def _capture_digest(run: Path) -> str:
    """A stable identity for one capture: its analysis and driver record. The
    driver stamps its own start time, so two real captures never share it."""
    h = hashlib.sha256()
    for name in ("analysis.json", "driver.json"):
        f = run / name
        h.update(name.encode() + b"\0" + (f.read_bytes() if f.exists() else b"") + b"\0")
    return h.hexdigest()[:16]


def cmd_add(a: argparse.Namespace) -> int:
    run = Path(a.run_dir)
    an = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
    drv = json.loads((run / "driver.json").read_text(encoding="utf-8")) if (run / "driver.json").exists() else {}
    smp = json.loads((run / "sampler.json").read_text(encoding="utf-8")) if (run / "sampler.json").exists() else {}
    ph = an.get("phases", {})
    m: dict[str, float | None] = {"pci": an.get("coverage", {}).get("PCI")}
    for p in ("idle-empty", "loaded-stopped", "four-playing"):
        if p in ph:
            m[f"renderer_cpu_pct.{p}"] = _role(ph[p], "chrome-renderer", "cpu_pct")
            m[f"engine_cpu_pct.{p}"] = _role(ph[p], "engine-python", "cpu_pct")
            m[f"renderer_pss_mb.{p}"] = _role(ph[p], "chrome-renderer", "pss_mb")
    lat = an.get("latency", {}).get("summary", {})
    # presented at the output (getOutputTimestamp), not merely rendered by the graph
    m["latency_play_audible_p50_ms"] = lat.get("play", {}).get("presented_ms_p50")
    m["latency_play_audible_p99_ms"] = lat.get("play", {}).get("presented_ms_p99")
    m["latency_fader_cut_audible_p99_ms"] = lat.get("fader_cut", {}).get("presented_ms_p99")
    m["latency_fader_open_audible_p99_ms"] = lat.get("fader_open", {}).get("presented_ms_p99")
    m["latency_eq_low_kill_audible_p99_ms"] = lat.get("eq_low_kill", {}).get("presented_ms_p99")
    m["latency_fader_reflected_p50_ms"] = lat.get("fader_cut", {}).get("reflected_ms_p50")
    for action in ("play", "fader_cut", "fader_open", "eq_low_kill"):
        m[f"latency_{action}_visual_p99_ms"] = lat.get(action, {}).get("next_frame_ms_p99")
    m["heap_C_mb"] = an.get("heap", {}).get("snapshot_total_mb")
    unbudgeted = an.get("class_a", {}).get("unbudgeted")
    m["class_a_paths_unbudgeted"] = len(unbudgeted) if isinstance(unbudgeted, list) else None
    invalid = capture_invalid(drv)
    if invalid:
        print(f"[FAIL] capture is invalid: {invalid}", file=sys.stderr)
        return 2
    harness, why = capture_harness(drv, smp, an)
    if why:
        print(f"[FAIL] {why}", file=sys.stderr)
        return 2
    why = sampler_scope(drv, smp) or sampler_span(drv, smp)
    if why:
        print(f"[FAIL] {why}", file=sys.stderr)
        return 2
    digest = _capture_digest(run)
    ledger = Path(a.ledger)
    entries = json.loads(ledger.read_text(encoding="utf-8")).get("entries", []) if ledger.exists() else []
    # a second add of one run would become compare's newest pair and read as no movement
    dup = next((e for e in entries if f"capture_sha={digest};" in str(e.get("note", ""))), None)
    if dup:
        print(f"[FAIL] this capture is already in the ledger as {dup.get('capture_id')}", file=sys.stderr)
        return 2
    # The revision that produced the capture, as the driver recorded it; never
    # this checkout's HEAD, which may have moved or be a different worktree.
    sha, why = _capture_revision(a.commit, drv.get("opts", {}).get("commit"))
    if why:
        print(f"[FAIL] {why}", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    # the digest keeps two runs with one basename added in one second apart
    capture_id = f"perflab-{a.host_class}-{now.strftime('%Y%m%dT%H%M%S')}Z-{run.name}-{digest[:8]}"
    note = (
        f"host_class={a.host_class}; run={run.name}; capture_sha={digest}; "
        f"browser={drv.get('browser_version')}; fixture_ids={drv.get('opts', {}).get('ids')}; "
        f"controls_factor={an.get('coverage', {}).get('control_factor')}; "
        f"class_a_unbudgeted={','.join(unbudgeted) if isinstance(unbudgeted, list) else None}; traced run"
    )
    rows = []
    for key, value in m.items():
        unit = UNITS.get(key) or ("pct_of_core" if "cpu_pct" in key else "MB" if "_mb" in key else "ms")
        row: dict[str, Any] = {
            "date": now.date().isoformat(),
            "round": "perf-lab",
            "kpi": PREFIX + key.replace(".", "__").replace("-", "_"),
            "unit": unit,
            "machine": platform.node(),
            "sha": sha,
            "source": "scripts/perf/lab/ledger.py",
            "capture_id": capture_id,
            "note": note,
            # what the capture ran on, as the driver copied it from its stack:
            # the compared keys only, never the stack's local runtime paths
            "manifest": stack_manifest.comparable(drv.get("manifest")),
            "workload": workload(drv.get("opts"), smp),
            # the measurement code (lab scripts) that captured and analyzed it
            "harness": harness,
            # sha256 of the audio the stack served for each fixture id
            "media": drv.get("media"),
            # the GPU, driver and GL renderer the capture's browser painted with
            "graphics": drv.get("graphics"),
            # the output sink, sample rate and buffer latencies its audio played through
            "audio_output": drv.get("audio_output"),
        }
        if value is None:
            row.update(value=None, measured=False, status="error", note=note + "; not measured in this run")
        else:
            row["value"] = value
        rows.append(row)
    sys.path.insert(0, str(REPO))
    from scripts.perf.kpi_ledger_append import append_entries

    append_entries(ledger, rows)
    print(f"[OK] {len(rows)} perflab entries, capture {capture_id} -> {a.ledger}")
    return 0


def _over_budget(metrics: dict) -> bool:
    """Prints and reports every Class A p99 over its absolute ceiling."""
    over = False
    for key, ceiling in BUDGETS.items():
        c = metrics.get(key.replace("-", "_"))
        if c is not None and c > ceiling:
            print(f"OVER-BUDGET {key:34} {c} > {ceiling} ms (Class A p99 {'visual' if 'visual' in key else 'audible'})")
            over = True
    return over


# driver.mjs options that change the work a capture measures (--controls plants
# a CPU burner, a retained buffer and a leak; the rest change phases, reps,
# engine, rendering or viewport). Two captures compare only when all are equal.
DRIVER_WORKLOAD = (
    "scenario",
    "engine",
    "controls",
    "headless",
    "phaseS",
    "leakCycles",
    "latencyReps",
    "width",
    "height",
)
# the sampler's tick interval changes what it measures (sampling overhead, how
# short a spike it can see), so it is a capture setting like the driver's
WORKLOAD = (*DRIVER_WORKLOAD, "sampler_interval_s")


def workload(opts: Any, sampler: Any) -> dict[str, Any] | None:
    """The capture's workload settings from driver.json opts and sampler.json,
    or None when any is missing (an older or hand-made capture: not comparable)."""
    if not isinstance(opts, dict) or any(k not in opts for k in DRIVER_WORKLOAD):
        return None
    if not isinstance(sampler, dict) or sampler.get("interval_s") is None:
        return None
    return {**{k: opts[k] for k in DRIVER_WORKLOAD}, "sampler_interval_s": sampler["interval_s"]}


def sampler_scope(drv: dict, sampler: dict) -> str | None:
    """None when the sampler's roots are exactly the capture's own process
    families (the stack's engine and vite, and the driver, whose child is
    Chromium), else why not. A sampler missing a root, or with a foreign pid,
    still reports process-family, and its CPU totals would be incomplete or
    someone else's; the ledger keeps only the normalized scope, never pids."""
    if sampler.get("scope") != "process-family":
        return f"the sampler ran with scope {sampler.get('scope')!r}, not process-family"
    want = drv.get("roots")
    if not isinstance(want, dict) or any(not isinstance(want.get(k), int) for k in ("engine", "vite", "driver")):
        return "driver.json records no engine, vite and driver pids: the sampler's scope cannot be checked"
    roots = sampler.get("roots")
    if not isinstance(roots, list):
        return "sampler.json records no roots: its scope cannot be checked"
    named = {pid: role for role, pid in want.items()}
    missing = sorted(role for pid, role in named.items() if pid not in roots)
    foreign = sorted(pid for pid in roots if pid not in named)
    if missing or foreign:
        parts = ([f"missing {', '.join(missing)}"] if missing else []) + (
            [f"foreign pid(s) {foreign}"] if foreign else []
        )
        return f"the sampler's roots are not this capture's engine, vite and driver: {'; '.join(parts)}"
    return None


def _epoch(iso: Any) -> float | None:
    """A driver.json ISO stamp (Date.toISOString, UTC with a Z) as epoch
    seconds, or None. A stamp with no zone is refused rather than read as
    local time."""
    if not isinstance(iso, str):
        return None
    try:
        at = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return at.timestamp() if at.tzinfo is not None else None


def sampler_span(drv: dict, sampler: dict) -> str | None:
    """None when the sampler's ticks span the driver's whole capture, else why
    not. A sampler stopped early (a short --duration, a premature kill) or
    started late still reports totals for the phases it saw, while the later
    latency or leak work ran with or without concurrent process-tree sampling
    depending on the run; two such captures would compare. So the capture's
    temporal scope is checked here, from timestamps both writers record:
    driver.json started_at/finished_at (UTC ISO) and sampler.json
    first_tick_t/last_tick_t (epoch seconds), on one host's clock.

    Slack: at most one sampler tick (interval_s) at each end. Ticks are
    discrete, so the sampler cannot place its own coverage finer than one
    interval, and a sampler started first can still be asleep when the driver
    stamps started_at (before its browser launches), or take its last tick up
    to one interval before finished_at (stamped after the measured phases,
    while the inputs are re-hashed). A sampler that starts any time before the
    driver, or stops any time after it, covers it and passes."""
    names = {"started_at": drv.get("started_at"), "finished_at": drv.get("finished_at")}
    d0, d1 = _epoch(names["started_at"]), _epoch(names["finished_at"])
    bad = [k for k, v in (("started_at", d0), ("finished_at", d1)) if v is None]
    if bad:
        return f"UNKNOWN: driver.json has no UTC {' or '.join(bad)}: the sampler's span cannot be checked"
    stamps = {k: sampler.get(k) for k in ("first_tick_t", "last_tick_t", "interval_s")}
    absent = [k for k, v in stamps.items() if isinstance(v, bool) or not isinstance(v, (int, float))]
    if absent:
        return f"UNKNOWN: sampler.json records no {', '.join(absent)}: the sampler's span cannot be checked"
    host_d, host_s = drv.get("host"), sampler.get("host")
    if not isinstance(host_d, str) or not isinstance(host_s, str) or not host_d or not host_s:
        return "UNKNOWN: driver.json or sampler.json records no host: their clocks cannot be compared"
    if host_d.casefold() != host_s.casefold():
        return f"UNKNOWN: driver ran on {host_d} and sampler on {host_s}: their clocks cannot be compared"
    s0, s1, tick = float(stamps["first_tick_t"]), float(stamps["last_tick_t"]), float(stamps["interval_s"])
    if not (tick > 0 and s0 <= s1 and d0 <= d1):
        return f"UNKNOWN: inconsistent span (sampler {s0}..{s1} every {tick} s, driver {d0}..{d1})"
    late = s0 - d0
    if late > tick:
        return (
            f"the sampler's first tick came {late:.3f} s after the driver started ({names['started_at']}), "
            f"more than one {tick} s tick: the start of the capture was not sampled"
        )
    early = d1 - s1
    if early > tick:
        return (
            f"the sampler's last tick came {early:.3f} s before the driver finished ({names['finished_at']}), "
            f"more than one {tick} s tick: the sampler stopped before the capture ended"
        )
    return None


def capture_harness(drv: dict, sampler: dict, analysis: dict) -> tuple[str | None, str | None]:
    """(harness identity, None) when driver, sampler and analyzer all stamped
    one and the same measurement code (manifest.harness_digest) and each
    recorded the library versions it ran with (the driver's Playwright via
    harness.mjs jsMeasurementDeps, the others' via manifest.measurement_deps)
    and the behavior-changing environment it ran under (instrument_env, inside
    measurement_deps), else (None, why). The identity covers all of them, so a
    profiler upgrade under unchanged source, or a driver run under another
    NODE_OPTIONS, never compares."""
    stamps = {
        "driver.json": drv.get("harness"),
        "sampler.json": sampler.get("harness"),
        "analysis.json": analysis.get("harness"),
    }
    missing = [k for k, v in stamps.items() if not v]
    if missing:
        return None, f"{', '.join(missing)} records no harness digest: the measurement code is unknown"
    if len(set(stamps.values())) != 1:
        return None, "driver, sampler and analyzer ran different harness code: " + ", ".join(
            f"{k}={str(v)[:12]}" for k, v in stamps.items()
        )
    deps = {
        "driver": drv.get("measurement_deps"),
        "sampler": sampler.get("measurement_deps"),
        "analysis": analysis.get("measurement_deps"),
    }
    unknown = [f"{k}.json" for k, v in deps.items() if not isinstance(v, dict) or not v]
    if unknown:
        return None, f"{', '.join(unknown)} records no measurement_deps: the measuring libraries are unknown"
    # the behavior-changing environment each instrument ran under
    # (manifest.instrument_env, harness.mjs instrumentEnv): part of the identity
    no_env = [f"{k}.json" for k, v in deps.items() if not isinstance(v.get("instrument_env"), dict)]
    if no_env:
        return None, (
            f"{', '.join(no_env)} records no instrument_env: the environment the instrument ran under is unknown"
        )
    ident = json.dumps({"code": stamps["driver.json"], "deps": deps}, sort_keys=True)
    return hashlib.sha256(ident.encode()).hexdigest(), None


# A capture parameter each side must record and both must share, with what
# its absence means: (field, "records no ...", why a difference voids a delta).
_CAPTURE_RECORDS = (
    ("media", "input digests; the audio and analysis it measured are unknown", "comparable only on the same inputs"),
    ("harness", "harness digest; the code that measured it is unknown", "a delta could be the instrument, not the app"),
    ("workload", f"capture settings ({', '.join(WORKLOAD)})", "they change the measured workload"),
    (
        "graphics",
        "graphics identity (GPU, driver and GL renderer from driver.mjs)",
        "paint, raster and GPU memory cost differ by GPU and driver, not by the app",
    ),
    (
        "audio_output",
        "audio output identity (device, sink, sample rate, channels and buffer latencies from driver.mjs)",
        "presented-audio latency and audio CPU differ by output device and buffer, not by the app",
    ),
)


def _input_diff(prev: dict, cur: dict) -> list[str]:
    """Which fixture inputs differ: the asset paths (media.mjs) whose digests
    differ or that only one capture loaded."""
    out = []
    for k in {*prev, *cur}:
        p, c = prev.get(k), cur.get(k)
        if isinstance(p, dict) and isinstance(c, dict):
            out += [f"{k}.{a}" for a in {*p, *c} if p.get(a) != c.get(a)]
        elif p != c:
            out.append(k)
    return out


def _record_mismatch(field: str, prev: Any, cur: Any) -> str:
    if field == "media":
        changed = sorted(_input_diff(prev, cur))
        return f"fixture inputs differ ({', '.join(changed)})"
    if field == "harness":
        return f"captures were measured by different harness code or libraries ({prev[:12]} -> {cur[:12]})"
    if field == "graphics":
        changed = sorted(k for k in {*prev, *cur} if prev.get(k) != cur.get(k))
        return f"graphics stacks differ in {', '.join(changed)} ({prev.get('gl_renderer')} -> {cur.get('gl_renderer')})"
    if field == "audio_output":
        changed = sorted(k for k in {*prev, *cur} if prev.get(k) != cur.get(k))
        return f"audio outputs differ in {', '.join(changed)} ({', '.join(f'{k} {prev.get(k)} -> {cur.get(k)}' for k in changed)})"
    return f"capture settings differ in {', '.join(k for k in WORKLOAD if prev[k] != cur[k])}"


def _incomparable(prev: dict, cur: dict) -> str | None:
    """Why two captures' numbers cannot be compared, or None. The manifests
    must agree outside the code under test (manifest.verdict). The fixture
    tracks, the audio bytes they served, the measurement harness and the
    capture settings are capture parameters, not stack properties: captures
    that differ in any measure different work, so a delta says nothing."""
    why, deps = stack_manifest.verdict(prev.get("manifest"), cur.get("manifest"))
    if why:
        return why
    if deps:
        print(f"deps differ: {', '.join(deps)} (measured as part of the change)")
    if prev.get("fixture_ids") != cur.get("fixture_ids"):
        return (
            f"fixture tracks differ ({prev.get('fixture_ids')} -> {cur.get('fixture_ids')}); "
            "numbers are comparable only within one fixture set"
        )
    for field, absent, consequence in _CAPTURE_RECORDS:
        for side, c in (("first", prev), ("second", cur)):
            if not c.get(field):
                return f"{side} capture records no {absent}"
            # fail closed: an output whose concrete device could not be read is
            # no identity, so two unknown devices never compare as one output
            if field == "audio_output" and not c[field].get("device"):
                why = c[field].get("device_unknown") or "no device recorded"
                return f"{side} capture's audio output device is undeterminable ({why}); {consequence}"
        if prev[field] != cur[field]:
            return f"{_record_mismatch(field, prev[field], cur[field])}; {consequence}"
    return None


def cmd_compare(a: argparse.Namespace) -> int:
    ledger = Path(a.ledger)
    entries = json.loads(ledger.read_text(encoding="utf-8"))["entries"] if ledger.exists() else []
    caps: dict[str, dict] = {}
    for e in entries:
        if not str(e.get("kpi", "")).startswith(PREFIX) or f"host_class={a.host_class};" not in e.get("note", ""):
            continue
        c = caps.setdefault(
            e["capture_id"],
            {
                "run": e["capture_id"],
                "git_sha": e.get("sha", ""),
                "manifest": e.get("manifest"),
                "workload": e.get("workload"),
                "harness": e.get("harness"),
                "media": e.get("media"),
                "graphics": e.get("graphics"),
                "audio_output": e.get("audio_output"),
                "metrics": {},
                "fixture_ids": e["note"].split("fixture_ids=")[-1].split(";")[0],
                "class_a_unbudgeted": e["note"].split("class_a_unbudgeted=")[-1].split(";")[0]
                if "class_a_unbudgeted=" in e["note"]
                else None,
            },
        )
        c["metrics"][e["kpi"][len(PREFIX) :].replace("__", ".")] = e.get("value")
    rows = list(caps.values())
    # The absolute Class A ceilings need no history: the newest capture is
    # held to them first, so a first capture or one that cannot be compared
    # still fails on a budget breach instead of reading UNKNOWN.
    over = _over_budget(rows[-1]["metrics"]) if rows else False
    if len(rows) < 2:
        print(f"[UNKNOWN] {len(rows)} row(s) for host class {a.host_class}; need two to compare")
        return 1 if over else 3
    prev, cur = rows[-2], rows[-1]
    why = _incomparable(prev, cur)
    if why:
        print(f"[UNKNOWN] {why}")
        return 1 if over else 3
    worst = 1 if over else 0
    for key, direction in HEADLINE:
        k = key.replace("-", "_")
        p, c = prev["metrics"].get(k), cur["metrics"].get(k)
        if p is None or c is None:
            print(f"[UNKNOWN] {key}: prev {p} cur {c}")
            worst = max(worst, 3) if worst != 1 else 1
            continue
        if p:
            rel = (c - p) / abs(p)
            bad = rel * direction < -a.tolerance
            tag = "REGRESSED" if bad else ("improved" if rel * direction > a.tolerance else "ok")
            print(f"{tag:9} {key:34} {p} -> {c} ({rel:+.0%})")
        else:
            # No relative change exists from a zero baseline: any move counts
            # in its direction, so 0 -> positive on a smaller-is-better KPI regresses.
            bad = (c - p) * direction < 0
            tag = "REGRESSED" if bad else ("improved" if c != p else "ok")
            print(f"{tag:9} {key:34} {p} -> {c} (from zero)")
        if bad:
            worst = 1
    # A Class A path the capture never timed has no budget check at all, so a
    # clean compare would vouch for it: UNKNOWN unless the caller explicitly
    # scopes the verdict to the paths that were budgeted.
    unb = cur["metrics"].get("class_a_paths_unbudgeted")
    names = cur.get("class_a_unbudgeted")
    if unb is None or unb > 0:
        scope = f"{unb} Class A path(s) not budgeted ({names})" if unb else "Class A coverage not recorded"
        if a.class_a_scope == "budgeted" and unb is not None:
            print(f"[SCOPE] verdict covers budgeted paths only; {scope}")
        else:
            print(f"[UNKNOWN] {scope}; pass --class-a-scope budgeted to accept a partial verdict")
            worst = 1 if worst == 1 else 3
    print(f"compared {prev['run']} ({prev['git_sha'][:9]}) -> {cur['run']} ({cur['git_sha'][:9]})")
    return worst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add")
    ad.add_argument("run_dir")
    ad.add_argument("--commit", help="revision the capture ran (default: driver.json opts.commit)")
    co = sub.add_parser("compare")
    co.add_argument("--tolerance", type=float, default=0.15)
    co.add_argument(
        "--class-a-scope",
        choices=("all", "budgeted"),
        default="all",
        help="all (default): a Class A path with no budget check makes the verdict UNKNOWN; "
        "budgeted: judge only the budgeted paths and name the rest",
    )
    for p in (ad, co):
        p.add_argument("--ledger", default=str(LEDGER))
        p.add_argument("--host-class", default="cloud-4vcpu")
    a = ap.parse_args()
    return cmd_add(a) if a.cmd == "add" else cmd_compare(a)


if __name__ == "__main__":
    sys.exit(main())
