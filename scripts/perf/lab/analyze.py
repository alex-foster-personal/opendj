# /// script
# requires-python = ">=3.11"
# dependencies = ["perfetto>=0.11", "psutil>=5.9"]
# ///
"""perf-lab analyzer: attribution, controls and the Profiling Coverage Index.

Reads one perf-lab run directory (driver.mjs + sampler.py + heapdiff.py output)
and answers, per phase:

  CPU      who burned it: process (OS) > thread (OS) > subsystem (trace slice
           self time: script, style, layout, paint, layerize/commit, raster,
           composite/draw, GC, audio render, IPC) > code (V8 samples in the
           trace).
  Memory   who holds it: process PSS (OS) > allocator (Chromium memory-infra
           effective_size: malloc, partition_alloc partitions, v8, blink_gc,
           gpu, cc, skia ...) > sub-pool > JS constructor (heap snapshot).
  Latency  where the press-to-effect time went: app model stages, the
           independent destination probe, and the main-thread work inside each
           press window.
  Leaks    MemLab three-snapshot set: constructors allocated between A and B
           that survive to C, with sizes; allocator slopes across cycles.

The Profiling Coverage Index (PCI) says how much of what the OS measured the
harness could explain, at each depth, and whether it found what was planted:

  coverage[resource][depth] = attributed amount / OS-measured amount
  depth weights L1 0.1, L2 0.2, L3 0.3, L4 0.4 (deeper attribution is worth more)
  PCI = mean over resources of sum(w * coverage) x control recovery factor

An unmeasured cell is UNKNOWN, scores 0 and is listed - never a silent pass
((private path)). There is no industry standard for this
number; it follows the closest practice (V8 profiles' (program)/(idle) split,
"symbolized on-CPU samples / all on-CPU samples") and extends it per depth.

Usage:
  uv run --locked --script scripts/perf/lab/analyze.py RUN_DIR
Writes RUN_DIR/analysis.json and RUN_DIR/report.md.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

LAB_DIR = Path(__file__).resolve().parent
# the libraries the analyzer records (manifest.measurement_deps)
INSTRUMENT_DEPS = ("perfetto", "psutil")

DEPTH_WEIGHTS = {"L1": 0.1, "L2": 0.2, "L3": 0.3, "L4": 0.4}
UNKNOWN = "UNKNOWN"

# Slice name -> subsystem. Order matters: first match wins. Generic scheduler
# containers are deliberately NOT here: their self time is "unattributed".
SUBSYSTEM_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "gc",
        re.compile(r"(^V8\.GC|GC$|^MajorGC|^MinorGC|^BlinkGC|Scavenge|MarkCompact|^CppGC|Oilpan|ThreadState::)", re.I),
    ),
    (
        "script",
        re.compile(
            r"(FunctionCall|v8\.callFunction|v8\.run|V8\.Execute|EvaluateScript|v8\.compile|V8\.Compile|RunMicrotasks|TimerFire|FireAnimationFrame|EventDispatch|v8\.produceCache|V8\.ScriptCompiler|HandlePostMessage|v8\.callModuleMethod)",
            re.I,
        ),
    ),
    (
        "style",
        re.compile(
            r"(UpdateLayoutTree|RecalcStyle|Document::updateStyle|StyleEngine|ScheduleStyleRecalculation|ParseAuthorStyleSheet)",
            re.I,
        ),
    ),
    (
        "layout",
        re.compile(r"^(Layout|LocalFrameView::layout|UpdateLayout|LayoutNG|IntersectionObserver|ResizeObserver)", re.I),
    ),
    ("prepaint", re.compile(r"(PrePaint|LocalFrameView::RunPrePaintLifecyclePhase)", re.I)),
    (
        "paint",
        re.compile(r"(^Paint$|PaintImage|LocalFrameView::RunPaintLifecyclePhase|Blink\.Paint|PaintController)", re.I),
    ),
    (
        "layerize-commit",
        re.compile(
            r"(Layerize|PaintArtifactCompositor|pushPaintArtifactToCompositor|CompositingCommit|^Commit$|LayerTreeHost::DoUpdateLayers|ProxyMain::BeginMainFrame::commit|UpdateLayers)",
            re.I,
        ),
    ),
    ("canvas", re.compile(r"(Canvas|BaseRenderingContext2D|OffscreenCanvas|DrawImage)", re.I)),
    (
        "raster",
        re.compile(
            r"(Raster|TileManager|ImageDecode|GpuImageDecodeCache|SoftwareImageDecodeCache|PaintOpBuffer)", re.I
        ),
    ),
    (
        "composite-draw",
        re.compile(
            r"(DrawAndSwap|DirectRenderer|SoftwareRenderer|SkiaRenderer|SkiaOutputSurface|Display::|DisplayScheduler|SurfaceAggregator|Graphics\.Pipeline|ProxyImpl::ScheduledAction|DrawFrame|SwapBuffers|CompositorFrame)",
            re.I,
        ),
    ),
    ("audio-render", re.compile(r"(Audio|WebAudio|Worklet)", re.I)),
    ("input", re.compile(r"(Input|EventLatency|HandleInputEvent)", re.I)),
    ("ipc-mojo", re.compile(r"(Mojo|mojom|ChannelMojo|IPC|Receive mojo message|ScopedSetIpcHash)", re.I)),
    ("animation", re.compile(r"(Animation|Animate|ServiceScriptedAnimations|PageAnimator)", re.I)),
    ("loading", re.compile(r"(ResourceLoad|ParseHTML|ResourceFetcher|Navigation)", re.I)),
]
GENERIC_CONTAINERS = re.compile(
    r"^(RunTask|ThreadControllerImpl::RunTask|ThreadPool_RunTask|SequenceManager|TaskQueueImpl|ThreadController|"
    r"ProxyMain::BeginMainFrame|WebFrameWidgetImpl::|BeginMainFrame|LayerTreeHost::|ProxyImpl::|"
    r"Scheduler::|SingleThreadProxy::|WidgetBase::|Receive mojo message|toplevel)",
    re.I,
)


def subsystem_of(name: str) -> str | None:
    for subsystem, pattern in SUBSYSTEM_RULES:
        if pattern.search(name):
            return subsystem
    return None


# --------------------------------------------------------------- OS (sampler)
def load_samples(run: Path) -> tuple[list[dict], dict]:
    path = run / "samples.jsonl"
    if not path.exists():
        return [], {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    meta = json.loads((run / "sampler.json").read_text(encoding="utf-8")) if (run / "sampler.json").exists() else {}
    return rows, meta


def _reaped_unseen(first: dict[str, dict], last: dict[str, dict], at_start: dict) -> int:
    """Processes whose reaped-children CPU grew inside the window (or could not
    be read). A descendant born AND exited between two ticks is in no tick at
    all; its CPU shows up only there. The window covers [first tick, last
    tick] inside the phase for numerator and denominator alike, so churn
    outside that span does not bias the rate; churn inside it that no tick saw
    voids every role's CPU, since which role it belonged to is unknown."""
    n = 0
    for pid, pl in last.items():
        c1 = pl.get("child_cpu_s")
        c0 = first[pid].get("child_cpu_s") if pid in at_start else 0.0
        if c1 is None or c0 is None or c1 > c0:
            n += 1
    return n


def _thread_cpu(inside: list[dict], last: dict[str, dict], names: dict) -> tuple[dict[str, float], int]:
    """CPU-seconds per role::thread name across the window's ticks, and how
    many threads exited while their process lived on. Each thread counts from
    its baseline to its last sighting, so one that exits mid-phase keeps the
    CPU every tick saw; only its tail after the last tick is lost, which
    undercounts named-thread CPU and so can only lower CPU L2 coverage, never
    inflate it. A thread (or process) born in the window started from zero."""
    seen: dict[str, dict[str, list[float]]] = {}
    for tick in inside:
        for pid, pr in tick["procs"].items():
            for tid, cpu in pr["threads"].items():
                span = seen.setdefault(pid, {}).setdefault(tid, [cpu if tick is inside[0] else 0.0, cpu])
                span[1] = cpu
    threads: dict[str, float] = defaultdict(float)
    exited = 0
    for pid, spans in seen.items():
        pl = last[pid]
        for tid, (cpu0, cpu1) in spans.items():
            raw = names.get(pid, {}).get(tid)
            tname = "(unnamed)" if raw is None else re.sub(r"\d+$", "#", raw)
            threads[f"{pl['role']}::{tname}"] += cpu1 - cpu0
            if pid in inside[-1]["procs"] and tid not in pl["threads"]:
                exited += 1
    return threads, exited


def _coverage_gap(first: float, last: float, t0: float, t1: float, meta: dict) -> str | None:
    """Why the ticks do not span the phase [t0, t1], or None. A sampler that
    started late or stopped early would otherwise publish a fragment's rate
    and end memory as the whole phase's; each end may miss by two intervals."""
    interval = meta.get("interval_s")
    if not isinstance(interval, (int, float)) or interval <= 0:
        return "sampler.json records no interval_s, so phase coverage is unprovable"
    slack = 2 * interval
    if first - t0 > slack or t1 - last > slack:
        return (
            f"sampler ticks cover {first - t0:.1f}s..{last - t0:.1f}s of a {t1 - t0:.1f}s phase "
            f"(allowed gap {slack:.1f}s at each end)"
        )
    return None


def os_window(samples: list[dict], meta: dict, t0: float, t1: float) -> dict[str, Any]:
    """CPU-seconds and memory per role/thread between two wall-clock instants."""
    inside = [s for s in samples if t0 <= s["t"] <= t1]
    if len(inside) < 2:
        return {"status": UNKNOWN, "reason": f"{len(inside)} sampler ticks inside window"}
    a, b = inside[0], inside[-1]
    gap = _coverage_gap(a["t"], b["t"], t0, t1, meta)
    if gap:
        return {"status": UNKNOWN, "reason": gap}
    secs = b["t"] - a["t"]
    roles: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cpu_s": 0.0, "pss_mb": 0.0, "uss_mb": 0.0, "rss_mb": 0.0, "n": 0}
    )
    # Each pid is counted from its own first to its own last observation in the
    # window, so a helper that starts or exits mid-phase still adds its CPU. A
    # pid absent from the first tick was born in the window (the sampler
    # rediscovers every tick), so its whole CPU counts. CPU a process burned
    # after its last tick before exiting is lost, so a role with an exit inside
    # the window has no measurable CPU: cpu_pct is None (UNKNOWN), never an
    # undercount that reads as a saving.
    first: dict[str, dict] = {}
    last: dict[str, dict] = {}
    for tick in inside:
        for pid, pr in tick["procs"].items():
            first.setdefault(pid, pr)
            last[pid] = pr
    born = [pid for pid in last if pid not in a["procs"]]
    exited = [pid for pid in last if pid not in b["procs"]]
    exited_roles = {last[pid]["role"] for pid in exited}
    reaped = _reaped_unseen(first, last, a["procs"])
    # memory is summed over a role's processes alive at the end; one whose read
    # failed makes that role's total UNKNOWN (None), never a partial sum
    unread: set[tuple[str, str]] = set()
    threads, threads_exited = _thread_cpu(inside, last, meta.get("thread_names", {}))
    for pid, pl in last.items():
        r = roles[pl["role"]]
        if pid in b["procs"]:
            r["n"] += 1
            for k_src, k_dst in (("pss", "pss_mb"), ("uss", "uss_mb"), ("rss", "rss_mb")):
                if pl.get(k_src) is not None:
                    r[k_dst] += pl[k_src] / 1048576
                else:
                    unread.add((pl["role"], k_dst))
        pf = first[pid] if pid in a["procs"] else None
        r["cpu_s"] += pl["cpu_s"] - (pf["cpu_s"] if pf else 0.0)
    for role, rv in roles.items():
        for k_dst in ("pss_mb", "uss_mb", "rss_mb"):
            if rv["n"] == 0 or (role, k_dst) in unread:
                rv[k_dst] = None
    return {
        "secs": secs,
        "lifecycle": {
            "born_in_window": len(born),
            "exited_in_window": len(exited),
            "reaped_unseen": reaped,
            "threads_exited_in_window": threads_exited,
        },
        "roles": {
            k: {
                **v,
                "cpu_pct": None if reaped or k in exited_roles else 100 * v["cpu_s"] / secs,
                "exited_in_window": sum(1 for pid in exited if last[pid]["role"] == k),
            }
            for k, v in roles.items()
        },
        "threads_cpu_pct": {
            k: 100 * v / secs for k, v in sorted(threads.items(), key=lambda kv: -kv[1]) if v / secs >= 0.002
        },
    }


# ------------------------------------------------------------ trace processor
def trace_processor_shell(run: Path) -> dict[str, str] | str:
    """The trace_processor_shell binary that would parse this run's trace, by
    path and the sha256 of its bytes. perfetto's package pins a sha256 and
    verifies it only when it downloads the binary; the cached copy it runs
    afterwards is never re-checked, so the analyzer hashes the bytes itself and
    open_trace runs exactly that path. "not used" without a trace, "absent"
    without perfetto. Resolving it may download the pinned binary, as the
    first open_trace would."""
    if not (run / "trace.pftrace").exists():
        return "not used"
    try:
        from perfetto.trace_processor.platform import (  # type: ignore[import-not-found,unused-ignore]
            PlatformDelegate,
        )
    except ImportError:
        return "absent"
    shell = Path(PlatformDelegate().get_shell_path(bin_path=None)).resolve()
    from scripts.perf.lab.manifest import sha256_file

    return {"path": str(shell), "sha256": sha256_file(shell)}


def analyzer_stamp(run: Path) -> tuple[str, dict[str, Any]]:
    """manifest.harness_stamp plus the trace processor binary (trace_processor_shell)."""
    from scripts.perf.lab.manifest import harness_stamp

    digest, deps = harness_stamp(INSTRUMENT_DEPS, LAB_DIR)
    return digest, {**deps, "trace_processor_shell": trace_processor_shell(run)}


def open_trace(run: Path, shell: dict[str, str] | str | None = None):
    """The trace processor on this run's trace. analyze's own run passes the
    shell binary its stamp hashed (trace_processor_shell), so it runs that
    exact file; a caller outside a stamped analysis (None) lets perfetto
    resolve its own binary, as it always did."""
    trace = run / "trace.pftrace"
    if not trace.exists():
        return None
    from perfetto.trace_processor import (  # type: ignore[import-not-found,unused-ignore]
        TraceProcessor,
        TraceProcessorConfig,
    )

    bin_path = shell["path"] if isinstance(shell, dict) else None
    return TraceProcessor(trace=str(trace), config=TraceProcessorConfig(bin_path=bin_path, load_timeout=600))


def q(tp, sql: str) -> list[dict]:
    return [dict(r.__dict__) for r in tp.query(sql)]


def has_table(tp, name: str) -> bool:
    try:
        q(tp, f"select * from {name} limit 1")
    except Exception:
        return False
    return True


def phase_windows(tp) -> dict[str, tuple[int, int]]:
    """perflab:<phase> user-timing measures -> (ts, ts_end) in trace ns."""
    rows = q(tp, "select name, ts, dur from slice where name like 'perflab:%' and dur > 0")
    out: dict[str, tuple[int, int]] = {}
    for r in rows:
        name = r["name"].removeprefix("perflab:")
        # press windows and the harness's own probe ticks are not phases
        if name.startswith("latency:") or name == "probe":
            continue
        out[name] = (r["ts"], r["ts"] + r["dur"])
    return out


def thread_roles(tp) -> list[dict]:
    return q(
        tp,
        """
        select t.utid, t.tid, t.name as tname, p.pid, p.name as pname
        from thread t left join process p using(upid)
        """,
    )


def self_cpu_ms(self_ns: int, dur: int, tdur: int | None) -> tuple[float, str]:
    """A slice's clipped wall self time, scaled to thread CPU by thread_dur/dur
    when the slice has thread_dur (descheduled time is not busy time)."""
    ms = max(0, self_ns) / 1e6
    if tdur is not None and dur:
        return ms * min(1.0, max(0, tdur) / dur), "thread_cpu"
    return ms, "wall"


# The harness's own work inside a measured window (driver.mjs, press.mjs and
# audio-probe.mjs measure it as `perflab:probe`): its thread self time is
# moved out of the subsystem it would otherwise land in and reported here.
HARNESS_PROBE = "harness-probe"


def _clipped_self_rows(tp, intervals_sql: str, tdur: str, lookback_ns: int | None = None) -> list[dict]:
    """Thread slices' self time clipped to each interval of intervals_sql
    (rows of `a`, `b` in trace ns, not overlapping one another): a slice's
    clipped duration minus its children's clipped durations, per interval.
    lookback_ns, when given, bounds how long before an interval a slice may
    start, so many short intervals stay one indexed range each."""
    since = "" if lookback_ns is None else f" and s.ts >= w.a - {int(lookback_ns)}"
    return q(
        tp,
        f"""
        with w as ({intervals_sql}),
        s2 as (
          select s.id, s.name, s.track_id, s.parent_id, w.a,
                 max(s.ts, w.a) as cts, min(s.ts + s.dur, w.b) as cte,
                 s.dur, {tdur} as tdur
          from slice s join thread_track tt on s.track_id = tt.id
          join w on s.ts < w.b and s.ts + s.dur > w.a{since}
          where s.dur > 0 and tt.name is null
        ),
        kids as (
          select parent_id, a, sum(cte - cts) as child
          from s2 where parent_id is not null group by parent_id, a
        )
        select s2.name, tt.utid, (s2.cte - s2.cts) - coalesce(kids.child, 0) as self_ns,
               s2.dur, s2.tdur
        from s2 join thread_track tt on s2.track_id = tt.id
        left join kids on kids.parent_id = s2.id and kids.a = s2.a
        """,
    )


def slice_self_by_subsystem(tp, ts0: int, ts1: int) -> dict[str, Any]:
    """Self time of every thread slice in the window, folded to subsystems.
    Clipped to the window. Basis is thread CPU where the slice carries
    thread_dur (its clipped wall self time scaled by thread_dur/dur, so time a
    thread spent descheduled inside a slice is not counted as busy) and wall
    otherwise; the result names which rows used which basis. Self time inside
    a `perflab:probe` measure is the harness's, not the app's: it is moved to
    HARNESS_PROBE, so no subsystem of the app is charged with it."""
    cols = {r["name"] for r in q(tp, "select name from pragma_table_info('slice')")}
    has_tdur = "thread_dur" in cols
    tdur = "s.thread_dur" if has_tdur else "null"
    rows = _clipped_self_rows(tp, f"select {ts0} as a, {ts1} as b", tdur)
    probes = _clipped_self_rows(
        tp,
        f"select max(ts, {ts0}) as a, min(ts + dur, {ts1}) as b from slice "
        f"where name = 'perflab:probe' and dur > 0 and ts < {ts1} and ts + dur > {ts0}",
        tdur,
        # a probe tick is one short synchronous call: what encloses it began
        # within its own task, far less than a second before it
        lookback_ns=1_000_000_000,
    )
    threads = {r["utid"]: r for r in thread_roles(tp)}
    per_thread: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unattributed_names: dict[str, float] = defaultdict(float)
    basis = {"thread_cpu": 0, "wall": 0}
    for sign, batch in ((1, rows), (-1, probes)):
        for r in batch:
            th = threads.get(r["utid"], {})
            tkey = f"{th.get('pname') or '?'}::{re.sub(r'[0-9]+$', '#', th.get('tname') or '?')}"
            self_ms, kind = self_cpu_ms(r["self_ns"], r["dur"], r["tdur"])
            if sign > 0:
                basis[kind] += 1
            sub = None if GENERIC_CONTAINERS.search(r["name"] or "") else subsystem_of(r["name"] or "")
            if sub is None:
                per_thread[tkey]["(unattributed)"] += sign * self_ms
                unattributed_names[r["name"]] += sign * self_ms
            else:
                per_thread[tkey][sub] += sign * self_ms
            if sign < 0:
                per_thread[tkey][HARNESS_PROBE] += self_ms
    secs = (ts1 - ts0) / 1e9
    table = {
        t: {k: round(100 * v / 1000 / secs, 2) for k, v in sorted(d.items(), key=lambda kv: -kv[1])}
        for t, d in per_thread.items()
    }
    return {
        # True only when every slice was measured in thread CPU, so a wall-time
        # row can never pass for CPU in the coverage note.
        "has_thread_cpu_time": has_tdur and basis["wall"] == 0,
        "slice_basis_counts": basis,
        "busy_pct_by_thread_subsystem": table,
        "top_unattributed_slices_ms": dict(sorted(unattributed_names.items(), key=lambda kv: -kv[1])[:15]),
    }


def js_samples(tp, ts0: int, ts1: int) -> dict[str, Any]:
    """V8 CPU samples recorded in the trace -> self samples by function/url."""
    if not has_table(tp, "cpu_profile_stack_sample"):
        return {"status": UNKNOWN, "reason": "no cpu_profile_stack_sample table"}
    rows = q(
        tp,
        f"""
        select f.name as fname, coalesce(m.name, '') as url, t.name as tname, count(*) as n
        from cpu_profile_stack_sample s
        join stack_profile_callsite c on s.callsite_id = c.id
        join stack_profile_frame f on c.frame_id = f.id
        left join stack_profile_mapping m on f.mapping = m.id
        left join thread t on s.utid = t.utid
        where s.ts >= {ts0} and s.ts < {ts1}
        group by 1, 2, 3 order by n desc
        """,
    )
    total = sum(r["n"] for r in rows)
    if total == 0:
        return {"status": UNKNOWN, "reason": "no V8 samples in window"}
    buckets = {"(idle)": 0, "(program)": 0, "(garbage collector)": 0, "(root)": 0}
    named: dict[str, int] = defaultdict(int)
    files: dict[str, int] = defaultdict(int)
    for r in rows:
        fname = r["fname"] or "(anonymous)"
        if fname in buckets:
            buckets[fname] += r["n"]
            continue
        url = re.sub(r"^.*?/(src|node_modules)/", r"\1/", r["url"]).split("?")[0]
        named[f"{fname} {url}"] += r["n"]
        files[url or "(native/builtin)"] += r["n"]
    non_idle = total - buckets["(idle)"]
    main_thread = sum(r["n"] for r in rows if r["tname"] == "CrRendererMain" and r["fname"] != "(idle)")
    main_thread_all = sum(r["n"] for r in rows if r["tname"] == "CrRendererMain")
    return {
        "samples": total,
        "non_idle_samples": non_idle,
        "main_thread_samples": main_thread,
        "main_thread_all_samples": main_thread_all,
        "buckets_pct_of_non_idle": {
            k: round(100 * v / max(1, non_idle), 1) for k, v in buckets.items() if k != "(idle)"
        },
        "symbolized_pct_of_non_idle": round(100 * sum(named.values()) / max(1, non_idle), 1),
        "top_functions_pct": [
            (k, round(100 * v / max(1, non_idle), 2)) for k, v in sorted(named.items(), key=lambda kv: -kv[1])[:25]
        ],
        "top_files_pct": [
            (k, round(100 * v / max(1, non_idle), 2)) for k, v in sorted(files.items(), key=lambda kv: -kv[1])[:15]
        ],
        "_named": named,
    }


def memory_dumps(tp) -> dict[str, Any]:
    """memory-infra dumps -> per process, per allocator effective_size (MB)."""
    if not has_table(tp, "memory_snapshot_node"):
        return {"status": UNKNOWN, "reason": "no memory_snapshot_node table (memory-infra off?)"}
    rows = q(
        tp,
        """
        select ms.timestamp as ts, p.pid, p.name as pname, n.path, n.size, n.effective_size,
               n.parent_node_id is null as is_root
        from memory_snapshot_node n
        join process_memory_snapshot pms on n.process_snapshot_id = pms.id
        join memory_snapshot ms on pms.snapshot_id = ms.id
        left join process p on pms.upid = p.upid
        """,
    )
    if not rows:
        return {"status": UNKNOWN, "reason": "memory_snapshot_node empty"}
    by_dump: dict[int, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        key = f"{r['pname'] or '?'}:{r['pid']}"
        # Root allocators are summed by effective_size (ownership edges remove
        # double counting across allocators). Inside an allocator, partitions
        # and buckets are read by size: effective_size moves bytes nobody claims
        # ownership of (ArrayBuffer backing stores, i.e. decoded PCM) to
        # allocated_objects/<unspecified>, which hides exactly the pool that
        # matters. The planted-buffer control showed it: 64.0 MB by size under
        # partitions/array_buffer, 2.2 MB by effective_size.
        val = r["effective_size"] if "/" not in r["path"] else r["size"]
        by_dump[r["ts"]][key][r["path"]] = (val or 0) / 1048576
    return {"dumps": {str(ts): {p: dict(v) for p, v in d.items()} for ts, d in sorted(by_dump.items())}}


def allocator_view(paths: dict[str, float]) -> dict[str, Any]:
    """Root allocators and the next level down, from one process dump."""
    roots = {p: v for p, v in paths.items() if "/" not in p}
    second = {p: v for p, v in paths.items() if 1 <= p.count("/") <= 3 and v >= 1 and "allocated_objects" not in p}
    return {
        "roots_mb": dict(sorted(((k, round(v, 1)) for k, v in roots.items()), key=lambda kv: -kv[1])),
        "sub_mb": dict(sorted(((k, round(v, 1)) for k, v in second.items()), key=lambda kv: -kv[1])[:40]),
        "roots_total_mb": round(sum(roots.values()), 1),
    }


P99_MIN_REPS = 100

# Actions whose audible effect is a detector that music alone could trip, and
# that detector. The driver's idle_control rows arm both with no command sent.
_CONTROLLED_DETECTORS = {"fader_cut": "level_drop", "eq_low_kill": "low_kill"}
_AUDIBLE_KEYS = ("destination_audible_ms", "presented_ms")


# The low-EQ kill detector, run here on the series the driver recorded rather
# than on line, so it can be tuned against idle_control and tested. A kill is the
# first run of LOW_KILL_RUN samples (polled every ~20 ms under load, each a
# 2048-sample window) whose low/mid ratio sits LOW_KILL_DROP_DB below the median
# of the half second before the press. The low shelf takes ~26 dB off below
# 150 Hz at a kill. Tuned Wed 14 Oct 2026 on 100 kill and 100 idle_control reps
# of one fixture passage: 18 dB x 6 was the shortest setting with no idle fire
# and no missed kill (15 dB x 6 fired once idle; 18 dB x 5 once). Because it
# waits for the whole run, a kill can be heard LATE but never early, so the EQ
# p99 is an upper bound; idle_control still voids it on any other music.
LOW_KILL_DROP_DB = 18.0
LOW_KILL_RUN = 6
_MIN_PRE_SAMPLES = 10


def low_kill_onset(
    series: list[list[Any]], drop_db: float = LOW_KILL_DROP_DB, run: int = LOW_KILL_RUN
) -> tuple[float, float] | None:
    """(ms from press, context time) where the kill is first heard, or None.
    Samples are [ms from press, context time, ratio dB, presented context time?,
    that stamp's correlated ms from press?].
    Raises ValueError when the series cannot say: too few pre-press samples or
    a sample the analyser could not read."""
    pre = [s[2] for s in series if s[0] < 0]
    if len(pre) < _MIN_PRE_SAMPLES or any(r is None for r in pre):
        raise ValueError(f"no usable baseline ({len(pre)} pre-press samples)")
    ref = sorted(pre)[len(pre) // 2]
    start: tuple[float, float] | None = None
    n = 0
    for t, c, r, *_presented in series:
        if t < 0:
            continue
        if r is None:
            raise ValueError("an unreadable sample after the press")
        if r < ref - drop_db:
            if n == 0:
                start = (t, c)
            n += 1
            if n >= run:
                return start
        else:
            n = 0
    return None


def _apply_low_kill(rows: list[dict]) -> None:
    """Fill the EQ kill's audible times, and idle_control's low_kill verdict,
    from the recorded series, in the same fields the on-line detectors use."""
    for r in rows:
        series = r.get("low_ratio_series")
        if r["action"] not in ("eq_low_kill", "idle_control") or series is None:
            continue
        try:
            hit = low_kill_onset(series)
        except ValueError:
            continue  # low_kill stays out of detectors_usable: unmeasured, not silent
        r.setdefault("detectors_usable", []).append("low_kill")
        if hit is None:
            continue
        r.setdefault("detectors_fired", []).append("low_kill")
        if r["action"] == "eq_low_kill":
            t, c = hit
            r["destination_audible_ms"] = t
            r["presented_ms"] = _presented_from_series(series, r.get("output_stamp"), c)


def _presented_from_series(series: list, stamp: dict | None, ctx: float) -> float | None:
    """When the context time ctx reached the listener, accepted only once the
    output clock is seen to have presented it: the first sample whose presented
    context time (4th element) is at or past ctx, or else the late stamp's own
    contextTime. A stall after the analyser saw the effect leaves neither, so a
    correlation alone would extrapolate a time for audio nobody heard. The time
    comes from that crossing observation only: its own correlation (a sample's
    5th element, or the stamp's performanceTime) maps ctx exactly, and without
    one the crossing sample bounds it, late by up to one poll and never early.
    A correlation from another moment is never used: the clock may have been
    re-anchored between them. None when nothing shows it presented."""
    for s in series:
        if len(s) > 3 and s[3] is not None and s[3] >= ctx:
            own = s[4] if len(s) > 4 else None
            return own + (ctx - s[3]) * 1000 if own is not None else s[0]
    if stamp is not None and stamp["contextTime"] >= ctx and stamp.get("performanceTime") is not None:
        return stamp["performanceTime"] + (ctx - stamp["contextTime"]) * 1000
    return None


def _void_unvalidated_detectors(summary: dict[str, Any], by_action: dict[str, list[dict]]) -> None:
    """An audible time from a detector is a measurement only when the same
    detector, armed on the same music with nothing pressed, stayed silent on
    every idle_control rep paired with that action (the driver replays the
    action's passage for its own control, so a later natural drop cannot pass
    for the effect). Otherwise (it fired, could not arm, there was no paired
    control, or fewer controls than presses) the action's audible stats are
    UNKNOWN, with the reason."""
    for action, det in _CONTROLLED_DETECTORS.items():
        if action not in summary:
            continue
        idle = [r for r in by_action.get("idle_control", []) if r.get("paired") == action]
        presses = len(by_action.get(action, []))
        why: str | None
        if not idle:
            why = f"no idle_control rows paired with {action} to validate the detector"
        elif len(idle) < presses:
            why = f"{len(idle)} idle_control reps paired with {action} for {presses} presses: not every press has its control"
        elif any(det not in r.get("detectors_usable", []) for r in idle):
            why = f"{det} detector could not arm on {sum(det not in r.get('detectors_usable', []) for r in idle)} idle_control reps"
        else:
            fired = sum(det in r.get("detectors_fired", []) for r in idle)
            why = (
                f"{det} detector fired with nothing pressed on {fired} of {len(idle)} idle_control reps"
                if fired
                else None
            )
        if why is None:
            continue
        s = summary[action]
        s["audible_void"] = why
        for k in _AUDIBLE_KEYS:
            for q in ("p50", "p95", "p99"):
                if f"{k}_{q}" in s:
                    s[f"{k}_{q}"] = None


def _press_window_attribution(tp) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Main-thread ms by subsystem inside each perflab:latency:<action> measure
    (press.mjs: from the input event to the painted frame showing the effect),
    per action, and apart from it the harness's own probe ms in those windows."""
    attributed: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    harness: dict[str, float] = defaultdict(float)
    if tp is None:
        return attributed, harness
    for w in q(tp, "select name, ts, dur from slice where name like 'perflab:latency:%' and dur > 0"):
        action = w["name"].split(":")[-1]
        sub = slice_self_by_subsystem(tp, w["ts"], w["ts"] + w["dur"])
        main = {k: v for k, v in sub["busy_pct_by_thread_subsystem"].items() if k.endswith("CrRendererMain")}
        for cats in main.values():
            for cat, p in cats.items():
                ms = p * w["dur"] / 1e9 * 10  # pct * secs * 1000 / 100 = ms
                # the harness's own checks are not the press's work
                if cat == HARNESS_PROBE:
                    harness[action] += ms
                else:
                    attributed[action][cat] += ms
    return attributed, harness


def latency_attribution(tp, run: Path) -> dict[str, Any]:
    lat_path = run / "latency.json"
    if not lat_path.exists():
        return {"status": UNKNOWN, "reason": "no latency scenario"}
    data = json.loads(lat_path.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    _apply_low_kill(rows)
    by_action: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_action[r["action"]].append(r)

    def pct(xs: list[float | None], p: float) -> float | None:
        # Every attempted rep must have produced a value: dropping the misses
        # would report a p50 over fewer reps than n claims. A metric that does
        # not apply to an action (audible for pause) is None on every rep.
        if not xs or any(x is None for x in xs):
            return None
        vals = sorted(x for x in xs if x is not None)
        k = (len(vals) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 2)

    def p99(xs: list[float | None]) -> float | None:
        # The Class A budget is a p99 (AGENTS.md: <=30 ms p99 audible), and a
        # tail can regress while the median holds. Nearest-rank, like
        # scripts/perf/capture_s2_agg.py, and only from P99_MIN_REPS complete
        # reps: below 100 samples the "p99" is just the maximum.
        if len(xs) < P99_MIN_REPS or any(x is None for x in xs):
            return None
        vals = sorted(x for x in xs if x is not None)
        return round(vals[math.ceil(0.99 * len(vals)) - 1], 2)

    summary = {}
    for action, rs in by_action.items():
        summary[action] = {
            "n": len(rs),
            # a press that never reached its control (press.mjs) measured
            # nothing of this action's path: UNKNOWN, never a number
            "status": UNKNOWN if any(r.get("press_error") for r in rs) else "OK",
            "n_valid": {
                k: sum(1 for r in rs if r.get(k) is not None)
                for k in ("reflected_ms", "next_frame_ms", "destination_audible_ms", "presented_ms")
            },
            **{
                f"{k}_p50": pct([r.get(k) for r in rs], 0.5)
                for k in ("reflected_ms", "next_frame_ms", "destination_audible_ms", "presented_ms")
            },
            **{
                f"{k}_p95": pct([r.get(k) for r in rs], 0.95)
                for k in ("reflected_ms", "next_frame_ms", "destination_audible_ms", "presented_ms")
            },
            **{f"{k}_p99": p99([r.get(k) for r in rs]) for k in ("reflected_ms", "next_frame_ms", "presented_ms")},
            "errors": [r["press_error"] for r in rs if r.get("press_error")],
        }
    _void_unvalidated_detectors(summary, by_action)
    attributed, harness = _press_window_attribution(tp)
    app_rows = data.get("app_model_rows", [])
    model = [r["stages"].get("input_to_output_ms") or r["stages"].get("input_to_audible_ms") for r in app_rows]
    return {
        "summary": summary,
        "main_thread_ms_in_press_windows": {
            a: {k: round(v, 1) for k, v in sorted(d.items(), key=lambda kv: -kv[1])} for a, d in attributed.items()
        },
        "harness_probe_ms_in_press_windows": {a: round(v, 1) for a, v in harness.items()},
        "app_model_input_to_audible_p50_ms": pct(model, 0.5),
        "app_model_rows": len(app_rows),
        "app_model_rows_valid": sum(1 for m in model if m is not None),
        "app_model_by_action": _app_model_by_action(app_rows, model, data.get("app_model_expected_by_action")),
    }


def _app_model_by_action(app_rows: list[dict], model: list[Any], expected: dict | None) -> dict[str, dict[str, int]]:
    """Model rows, usable rows and owed rows per press action. A row the driver
    could not tie to a press counts under "(untagged)", which owes nothing."""
    out: dict[str, dict[str, int]] = {}
    for r, m in zip(app_rows, model, strict=True):
        a = out.setdefault(str(r.get("perflab_action") or "(untagged)"), {"rows": 0, "valid": 0, "expected": 0})
        a["rows"] += 1
        a["valid"] += m is not None
    for action, n in (expected or {}).items():
        out.setdefault(action, {"rows": 0, "valid": 0, "expected": 0})["expected"] = int(n)
    return out


# ----------------------------------------------------------------- heap diffs
def heap_diff(run: Path, harness: str) -> dict[str, Any]:
    """heapdiff.json, when heapdiff.py stamped the harness this analyzer runs
    (the lab code that diffed the snapshots is the code that scores them);
    else UNKNOWN naming why."""
    out = run / "heapdiff.json"
    if not out.exists():
        return {"status": UNKNOWN, "reason": "heapdiff.json missing (run heapdiff.py)"}
    heap = json.loads(out.read_text(encoding="utf-8"))
    if heap.get("harness") != harness or not isinstance(heap.get("measurement_deps"), dict):
        why = (
            f"heapdiff.json was made by other harness code ({str(heap.get('harness'))[:12]} vs {harness[:12]})"
            if heap.get("harness")
            else "heapdiff.json records no harness digest or measurement_deps: the code that diffed it is unknown"
        )
        return {"status": UNKNOWN, "reason": why}
    return heap


# ---------------------------------------------------------------- coverage
def _cpu_cells(phase: dict, roles: dict, chrome_cpu: float | None, notes: list[str]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    os_ = phase.get("os", {})
    # a role with cpu_pct None (exited in the window) or no Chrome role at all
    # leaves the CPU picture partial, so coverage is UNKNOWN, never full
    full = bool(roles) and all(v.get("cpu_pct") is not None for v in roles.values())
    cells["L1"] = 1.0 if full and any(k.startswith("chrome") for k in roles) else UNKNOWN
    # CPU L2: named threads / process CPU.
    th = os_.get("threads_cpu_pct", {})
    th_named = sum(v for k, v in th.items() if not k.endswith("::(unnamed)") and k.startswith("chrome"))
    cells["L2"] = round(min(1.0, th_named / chrome_cpu), 3) if chrome_cpu else UNKNOWN
    # CPU L3: trace-attributed subsystem time on chrome threads / OS chrome CPU.
    sub = phase.get("subsystems", {}).get("busy_pct_by_thread_subsystem", {})
    named_sub = sum(p for t, cats in sub.items() for c, p in cats.items() if c != "(unattributed)")
    if chrome_cpu and sub:
        cells["L3"] = round(min(1.0, named_sub / chrome_cpu), 3)
        if not phase.get("subsystems", {}).get("has_thread_cpu_time"):
            notes.append("CPU L3 uses slice WALL self time (no thread_dur in trace); can overstate on-CPU time.")
    else:
        cells["L3"] = UNKNOWN
    # CPU L4: share of renderer-main script time with a symbolized JS frame, scaled
    # by script's share of the chrome CPU; native subsystems stop at L3 (no
    # Chromium symbols), so L4 is honest about that.
    js = phase.get("js", {})
    if isinstance(js, dict) and "symbolized_pct_of_non_idle" in js and chrome_cpu:
        script_pct = sum(cats.get("script", 0) for t, cats in sub.items())
        sym = js["symbolized_pct_of_non_idle"] / 100
        cells["L4"] = round(min(1.0, (script_pct * sym) / chrome_cpu), 3)
    else:
        cells["L4"] = UNKNOWN
    return cells


def _memory_cells(phase_name: str, phase: dict, roles: dict, analysis: dict, notes: list[str]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    # Memory L1: PSS per process from the OS, full only when every role has a
    # measured PSS (os_window sets None when any of its reads was lost)
    measured = [v.get("pss_mb") is not None for v in roles.values()]
    cells["L1"] = round(sum(measured) / len(measured), 3) if any(measured) else UNKNOWN
    # Memory L2: memory-infra root allocators / renderer PSS.
    mem = phase.get("memory", {})
    renderer_pss = roles.get("chrome-renderer", {}).get("pss_mb")
    if mem.get("renderer") and renderer_pss:
        cells["L2"] = round(min(1.0, mem["renderer"]["roots_total_mb"] / renderer_pss), 3)
        cells["L2_raw_ratio"] = round(mem["renderer"]["roots_total_mb"] / renderer_pss, 3)
    else:
        cells["L2"] = UNKNOWN
    # Memory L3: sub-pools with a name below the root, / renderer PSS.
    if mem.get("renderer") and renderer_pss:
        subs = mem["renderer"]["sub_mb"]
        # Named pools one level under each root (partition_alloc one level
        # deeper, since its children are just "partitions"/"allocated_objects").
        named = {
            k: v
            for k, v in subs.items()
            if (k.startswith("partition_alloc/partitions/") and k.count("/") == 2)
            or (not k.startswith("partition_alloc") and k.count("/") == 1)
        }
        cells["L3"] = round(min(1.0, sum(named.values()) / renderer_pss), 3)
        cells["L3_raw_ratio"] = round(sum(named.values()) / renderer_pss, 3)
    else:
        cells["L3"] = UNKNOWN
    # Memory L4: JS heap by constructor (heap snapshot) covers only the V8 heap.
    # The ratio needs the snapshot and the PSS from one state: the leak
    # scenario's snapshots come after every reload cycle (and, with controls,
    # the planted leak), so dividing them by this phase's PSS mixes two states
    hd = analysis.get("heap", {})
    if isinstance(hd, dict) and hd.get("snapshot_total_mb") and renderer_pss and hd.get("phase") == phase_name:
        cells["L4"] = round(min(1.0, hd["snapshot_total_mb"] / renderer_pss), 3)
    else:
        cells["L4"] = UNKNOWN
        if isinstance(hd, dict) and hd.get("snapshot_total_mb"):
            notes.append(
                f"memory L4 UNKNOWN: the heap snapshot was taken in {hd.get('phase') or 'the leak scenario'}, "
                f"not in {phase_name}, so it cannot be divided by {phase_name}'s renderer PSS"
            )
    return cells


# The Class A control paths of the latency contract (AGENTS.md, LATENCY-01),
# each with the scenario actions whose output-presented time covers it. A path
# with no actions is not exercised yet, so latency L2 cannot reach 1.0.
_CLASS_A_PATHS: dict[str, tuple[str, ...]] = {
    "play": ("play",),
    "pause": (),
    "cue": (),
    "hot_cue": (),
    "eq": ("eq_low_kill",),
    "filter": (),
    "stem_mute": (),
    "fader": ("fader_cut", "fader_open"),
    "crossfader": (),
}
_CLASS_A_AUDIBLE = tuple(a for acts in _CLASS_A_PATHS.values() for a in acts)


def class_a_scope() -> dict[str, list[str]]:
    """Which Class A paths this harness times (and ledger.py holds to the
    budgets) and which it does not exercise yet, by name, so a comparison can
    say what it did not check instead of passing it."""
    return {
        "budgeted": [p for p, acts in _CLASS_A_PATHS.items() if acts],
        "unbudgeted": [p for p, acts in _CLASS_A_PATHS.items() if not acts],
    }


def _latency_cells(analysis: dict, notes: list[str] | None = None) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    # Latency: L1 end-to-end measured; L2 split into resolve/reflect/frame/audible;
    # L3 main-thread work in windows attributed to subsystems; L4 app model stages.
    lat = analysis.get("latency", {})
    if lat.get("summary"):
        summ = lat["summary"]
        # end to end means presented at the output: a press timed only to its
        # state reflection or the graph render earns no latency credit
        play = summ.get("play", {})
        cells["L1"] = 1.0 if play.get("presented_ms_p50") is not None else UNKNOWN
        # L2: the share of Class A paths with an output-presented time for
        # every action covering it. Any measured action without one leaves the
        # cell UNKNOWN; a path with no action counts as unmeasured, not as passed.
        if any(summ.get(a, {}).get("presented_ms_p50") is None for a in _CLASS_A_AUDIBLE):
            cells["L2"] = UNKNOWN
        else:
            covered = [path for path, acts in _CLASS_A_PATHS.items() if acts]
            cells["L2"] = round(len(covered) / len(_CLASS_A_PATHS), 3)
            missing = [path for path in _CLASS_A_PATHS if path not in covered]
            if missing and notes is not None:
                notes.append(f"latency L2: Class A paths not exercised: {', '.join(missing)}")
        main = lat.get("main_thread_ms_in_press_windows", {})
        tot = sum(sum(d.values()) for d in main.values())
        named = sum(v for d in main.values() for k, v in d.items() if k != "(unattributed)")
        cells["L3"] = round(named / tot, 3) if tot else UNKNOWN
        # L4: the share of app-model rows with a usable stage timing, over every
        # row the presses owed, per action: a press that emitted none counts as
        # unmeasured, and one action's surplus never covers another's gap
        by_action = lat.get("app_model_by_action") or {}
        owed = sum(max(a["rows"], a["expected"]) for a in by_action.values())
        valid = sum(a["valid"] for a in by_action.values())
        cells["L4"] = round(valid / owed, 3) if owed else 0.0
    else:
        for d in DEPTH_WEIGHTS:
            cells[d] = UNKNOWN
    return cells


def coverage(analysis: dict, controls: dict) -> dict[str, Any]:
    """Coverage per resource and depth, plus control recovery -> PCI."""
    notes: list[str] = []
    phases = analysis["phases"]
    phase_name = "four-playing" if phases.get("four-playing") else next(iter(phases), "")
    phase = phases.get(phase_name)
    if not phase:
        return {
            "status": UNKNOWN,
            "reason": "no phases",
            "cells": {},
            "notes": ["no phases: the capture ended before any phase completed"],
        }
    roles = phase.get("os", {}).get("roles", {})
    chrome = [v["cpu_pct"] for k, v in roles.items() if k.startswith("chrome")]
    # a chrome role with an exit inside the window has no measurable CPU (os_window)
    chrome_cpu = None if any(c is None for c in chrome) else sum(chrome)
    cells: dict[str, dict[str, Any]] = {
        "cpu": _cpu_cells(phase, roles, chrome_cpu, notes),
        "memory": _memory_cells(phase_name, phase, roles, analysis, notes),
        "latency": _latency_cells(analysis, notes),
    }
    # Controls: recovery of what was planted.
    recov = []
    for c in controls.get("planted", []):
        r = analysis.get("controls", {}).get(c["kind"])
        recov.append((c["kind"], r))
    factor = 1.0
    for kind, r in recov:
        if not isinstance(r, dict) or r.get("score") is None:
            # a planted control that was not recovered proves nothing: no credit
            why = r.get("reason", "not scored") if isinstance(r, dict) else "not scored"
            notes.append(f"Control {kind} not recovered ({why}): control factor 0.")
            factor = 0.0
        else:
            factor *= max(0.0, min(1.0, r["score"]))
    resource_scores = {}
    for res, ds in cells.items():
        s = 0.0
        for d, w in DEPTH_WEIGHTS.items():
            v = ds.get(d)
            s += w * (v if isinstance(v, (int, float)) else 0.0)
        resource_scores[res] = round(s, 3)
    pci = round(sum(resource_scores.values()) / len(resource_scores) * (factor if recov else 0.8), 3)
    if not recov:
        notes.append("No controls planted: PCI capped at 0.8x (run with --controls).")
    return {
        "cells": cells,
        "resource_scores": resource_scores,
        "control_factor": round(factor, 3),
        "controls": dict(recov),
        "PCI": pci,
        "notes": notes,
    }


def score_retained_arraybuffer(delta_mb: dict[str, float], planted_mb: float) -> dict[str, Any]:
    """Score the planted Float32Array against the allocator it lives in. The
    largest growth anywhere could be unrelated, so only array_buffer paths count."""
    ab = {p: v for p, v in delta_mb.items() if "array_buffer" in p}
    top = sorted(delta_mb.items(), key=lambda kv: -kv[1])[:5]
    if not ab:
        return {
            "planted_mb": planted_mb,
            "score": None,
            "reason": "no array_buffer allocator in dumps",
            "top_growth": top,
        }
    path, grew = max(ab.items(), key=lambda kv: kv[1])
    return {
        "planted_mb": planted_mb,
        "array_buffer_growth": (path, grew),
        "top_growth": top,
        "score": round(max(0.0, 1 - abs(grew - planted_mb) / planted_mb), 3),
    }


def score_controls(analysis: dict, controls: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in controls.get("planted", []):
        if c["kind"] == "cpu_burner":
            ph = analysis["phases"].get("control-burner", {})
            js = ph.get("js", {})
            named = js.get("_named", {}) if isinstance(js, dict) else {}
            hit = sum(v for k, v in named.items() if k.startswith("perflabPlantedBurner"))
            # expected = burned_ms / window_ms is a share of wall time, and the
            # sampler ticks at a fixed rate busy or idle, so the matching
            # denominator is every renderer-main sample, idle included (not other
            # threads, which would dilute it, nor busy samples only, which would
            # inflate it whenever the main thread does other work).
            main_samples = js.get("main_thread_all_samples") if isinstance(js, dict) else None
            if not main_samples or not ph.get("secs"):
                out["cpu_burner"] = {"score": None, "reason": "no renderer-main JS samples in control phase"}
                continue
            expected = c["burned_ms"] / c["window_ms"]
            measured = hit / max(1, main_samples)
            out["cpu_burner"] = {
                "expected_main_thread_share": round(expected, 3),
                "measured_share": round(measured, 3),
                "found_by_name": hit > 0,
                "score": round(max(0.0, 1 - abs(measured - expected) / expected), 3) if hit else 0.0,
            }
        elif c["kind"] == "retained_arraybuffer":
            d = analysis.get("plant_delta_mb", {})
            if not d:
                out["retained_arraybuffer"] = {"score": None, "reason": "no before/after-plant dumps"}
                continue
            out["retained_arraybuffer"] = score_retained_arraybuffer(d, c["bytes"] / 1048576)
        elif c["kind"] == "js_leak":
            hd = analysis.get("heap", {})
            leak = (hd.get("leak_candidates") or []) if isinstance(hd, dict) else []
            hit = next((x for x in leak if x["name"] == "PerflabPlantedLeak"), None)
            expected = c["count_per_cycle"] * c["cycles_between_A_and_C"] // 2  # allocated between A and B
            if hit is None:
                out["js_leak"] = {"score": 0.0 if leak else None, "found_by_name": False, "expected_count": expected}
            else:
                rank = leak.index(hit) + 1
                out["js_leak"] = {
                    "found_by_name": True,
                    "rank": rank,
                    "count": hit["count"],
                    "expected_count": expected,
                    "score": round(max(0.0, 1 - abs(hit["count"] - expected) / expected), 3),
                }
    return out


# ------------------------------------------------------------------- report
def fmt_table(rows: list[list[Any]], header: list[str]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def render_report(a: dict) -> str:
    lines = [f"# perf-lab run {a['run']}", ""]
    cov = a["coverage"]
    lines += [
        f"**Profiling Coverage Index: {cov.get('PCI')}** "
        f"(0-1; per-resource {cov.get('resource_scores')}, control factor {cov.get('control_factor')})",
        "",
    ]
    if cov.get("notes"):
        lines += [f"- {n}" for n in cov["notes"]] + [""]
    lines += [
        "## Coverage cells",
        fmt_table(
            [[r, *[cov["cells"][r].get(d, UNKNOWN) for d in DEPTH_WEIGHTS]] for r in cov["cells"]],
            ["resource", *DEPTH_WEIGHTS],
        ),
        "",
    ]
    lines += ["## Controls", "```json", json.dumps(cov.get("controls"), indent=1, default=str), "```", ""]
    for name, ph in a["phases"].items():
        lines += [f"## Phase {name} ({ph.get('secs', 0):.1f} s)"]
        os_ = ph.get("os", {})
        if os_.get("roles"):
            lines += [
                fmt_table(
                    [
                        [
                            k,
                            "UNKNOWN (exit in window)" if v["cpu_pct"] is None else f"{v['cpu_pct']:.1f}",
                            "UNKNOWN" if v["pss_mb"] is None else f"{v['pss_mb']:.0f}",
                            "UNKNOWN" if v["uss_mb"] is None else f"{v['uss_mb']:.0f}",
                        ]
                        for k, v in sorted(os_["roles"].items(), key=lambda kv: -(kv[1]["cpu_pct"] or -1.0))
                    ],
                    ["role", "CPU %", "PSS MB", "USS MB"],
                ),
                "",
            ]
            lines += [
                "Top threads (OS CPU %): "
                + ", ".join(f"{k} {v:.1f}" for k, v in list(os_.get("threads_cpu_pct", {}).items())[:10]),
                "",
            ]
        sub = ph.get("subsystems", {}).get("busy_pct_by_thread_subsystem", {})
        for t, cats in sorted(sub.items(), key=lambda kv: -sum(kv[1].values()))[:6]:
            lines.append(f"- `{t}`: " + ", ".join(f"{c} {p}%" for c, p in list(cats.items())[:8]))
        js = ph.get("js", {})
        if isinstance(js, dict) and js.get("top_functions_pct"):
            lines += [
                "",
                f"JS samples: {js['samples']} (non-idle {js['non_idle_samples']}), "
                f"symbolized {js['symbolized_pct_of_non_idle']}%, buckets {js['buckets_pct_of_non_idle']}",
            ]
            lines += [f"  - {f} {p}%" for f, p in js["top_functions_pct"][:12]]
        mem = ph.get("memory", {})
        if mem.get("renderer"):
            lines += [
                "",
                "Renderer allocators (memory-infra effective MB): "
                + ", ".join(f"{k} {v}" for k, v in list(mem["renderer"]["roots_mb"].items())[:10]),
            ]
            lines += ["Sub-pools: " + ", ".join(f"{k} {v}" for k, v in list(mem["renderer"]["sub_mb"].items())[:14])]
        lines.append("")
    if a.get("latency", {}).get("summary"):
        lines += ["## Latency", "```json", json.dumps(a["latency"], indent=1, default=str), "```", ""]
    if isinstance(a.get("heap"), dict) and a["heap"].get("leak_candidates"):
        lines += [
            "## Leak candidates (allocated A..B, alive at C)",
            fmt_table(
                [
                    [x["name"], x["count"], x["self_mb"], x.get("retainer", "")]
                    for x in a["heap"]["leak_candidates"][:15]
                ],
                ["constructor", "count", "self MB", "sample retainer path"],
            ),
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path)
    args = ap.parse_args()
    run: Path = args.run
    sys.path.insert(0, str(LAB_DIR.parents[2]))
    from scripts.perf.lab.manifest import harness_moved

    # The identity of the code and libraries that analyze, taken before the
    # trace processor loads (perfetto is imported and its trace parsed, for up
    # to load_timeout, inside open_trace) and verified again before anything is
    # published, as sampler.py does: an analyzer or perfetto replaced while it
    # ran has no one identity, so the analysis carries none and is invalid.
    started_as = analyzer_stamp(run)
    phases_meta = json.loads((run / "phases.json").read_text(encoding="utf-8"))
    controls = (
        json.loads((run / "controls.json").read_text(encoding="utf-8")) if (run / "controls.json").exists() else {}
    )
    samples, smeta = load_samples(run)
    tp = open_trace(run, started_as[1]["trace_processor_shell"])
    windows = phase_windows(tp) if tp else {}
    from datetime import datetime

    from scripts.perf.lab.ledger import capture_invalid

    # the measurement code that analyzed this run; the ledger refuses a run whose
    # driver, sampler and analyzer stamps differ
    analysis: dict[str, Any] = {
        "run": run.name,
        "harness": started_as[0],
        "measurement_deps": dict(started_as[1]),
        "phases": {},
    }
    dumps: dict[str, Any] = memory_dumps(tp) if tp else {"status": UNKNOWN}
    dump_ts = sorted(int(k) for k in dumps.get("dumps", {}))

    def renderer_view_at(ts_lo: int, ts_hi: int) -> dict | None:
        cands = [t for t in dump_ts if ts_lo <= t <= ts_hi]
        if not cands:
            return None
        d = dumps["dumps"][str(cands[-1])]
        rend = [v for k, v in d.items() if k.lower().startswith("renderer")]
        return allocator_view(max(rend, key=lambda v: sum(x for p, x in v.items() if "/" not in p))) if rend else None

    # An unscoped sampler counted every matching process on the machine: its
    # totals would publish as this run's, so every OS cell is UNKNOWN instead.
    drv = json.loads((run / "driver.json").read_text(encoding="utf-8")) if (run / "driver.json").exists() else {}
    scope = smeta.get("scope")
    off_scope = (
        f"the sampler ran with scope {scope!r}, not process-family: its CPU and memory include other processes"
        if samples and scope != "process-family"
        else None
    )
    # a dropout, page exception or failed dump voids the capture: the analysis
    # is still written for diagnosis, but marked and refused, never [OK]
    invalid = capture_invalid(drv)
    for ph in phases_meta:
        name = ph["name"]
        wall0 = datetime.fromisoformat(ph["wall_start"].replace("Z", "+00:00")).timestamp()
        entry: dict[str, Any] = {"secs": ph["wall_s"]}
        entry["os"] = (
            {"status": UNKNOWN, "reason": off_scope or "no sampler ticks"}
            if off_scope or not samples
            else os_window(samples, smeta, wall0, wall0 + ph["wall_s"])
        )
        if tp and name in windows:
            ts0, ts1 = windows[name]
            entry["subsystems"] = slice_self_by_subsystem(tp, ts0, ts1)
            entry["js"] = js_samples(tp, ts0, ts1)
            rv = renderer_view_at(ts0, max(ts1, ts0 + int(6e9)))
            entry["memory"] = {"renderer": rv} if rv else {"status": UNKNOWN}
        analysis["phases"][name] = entry

    # planted-buffer delta between the explicit before/after dumps
    if tp:
        marks = {r["name"]: r["ts"] for r in q(tp, "select name, ts from slice where name like 'perflab:dump:%'")}
        b, a_ = marks.get("perflab:dump:before-plant"), marks.get("perflab:dump:after-plant")
        if b and a_:
            before = [t for t in dump_ts if t >= b and t < a_]
            after = [t for t in dump_ts if t >= a_]
            if before and after:

                def rend(ts: int) -> dict[str, float]:
                    d = dumps["dumps"][str(ts)]
                    v = [x for k, x in d.items() if k.lower().startswith("renderer")]
                    return max(v, key=lambda m: sum(x for p, x in m.items() if "/" not in p)) if v else {}

                rb, ra = rend(before[0]), rend(after[0])
                analysis["plant_delta_mb"] = {
                    p: round(ra.get(p, 0) - rb.get(p, 0), 1) for p in set(ra) | set(rb) if p.count("/") <= 4
                }
    analysis["latency"] = latency_attribution(tp, run)
    analysis["heap"] = heap_diff(run, started_as[0])
    analysis["controls"] = score_controls(analysis, controls)
    analysis["coverage"] = coverage(analysis, controls)
    analysis["class_a"] = class_a_scope()
    for ph in analysis["phases"].values():
        if isinstance(ph.get("js"), dict):
            ph["js"].pop("_named", None)
    moved = harness_moved(started_as, analyzer_stamp(run))
    if moved:
        analysis.update(harness=None, measurement_deps=None, harness_fault=moved)
        invalid = invalid or moved
    else:
        # heapdiff.py is an instrument too: its interpreter joins the identity
        heap_deps = analysis["heap"].get("measurement_deps")
        analysis["measurement_deps"]["heapdiff"] = heap_deps if isinstance(heap_deps, dict) else "absent"
    return _publish(run, analysis, invalid)


def _publish(run: Path, analysis: dict[str, Any], invalid: str | None) -> int:
    """Write analysis.json and report.md; an invalid capture is written for
    diagnosis but marked so in both, and exits 1, never [OK]."""
    report = render_report(analysis)
    if invalid:
        analysis["invalid"] = invalid
        report = (
            f"# INVALID CAPTURE: {invalid}\n\nNothing below is evidence; it is kept for diagnosis only.\n\n{report}"
        )
    # explicit UTF-8: a non-ASCII frame or path must not depend on the OS code page
    (run / "analysis.json").write_text(json.dumps(analysis, indent=1, default=str), encoding="utf-8", newline="\n")
    (run / "report.md").write_text(report, encoding="utf-8", newline="\n")
    if invalid:
        print(
            f"[FAIL] capture is invalid: {invalid}; analysis kept for diagnosis -> {run / 'report.md'}", file=sys.stderr
        )
        return 1
    print(f"[OK] PCI {analysis['coverage'].get('PCI')} -> {run / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
