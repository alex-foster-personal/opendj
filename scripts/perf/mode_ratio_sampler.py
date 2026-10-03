"""Process-tree footprint and CPU sampling for PERFMODE-15 mode-ratio capture.

Extracted from capture_mode_ratios.py so the capture orchestrator stays under
the Python file-size ratchet; behavior unchanged.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import psutil

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics

_PS_BIN = "/bin/ps"


@dataclass(frozen=True)
class _PsRow:
    pid: int
    ppid: int
    rss_kb: int
    cpu_percent: float


def _parse_ps_table(output: str) -> list[_PsRow]:
    """Parse `ps -Ao pid=,ppid=,rss=,%cpu=`; a malformed line raises, never drops."""
    rows: list[_PsRow] = []
    for line in output.split("\n"):
        text = line.strip()
        if not text:
            continue
        fields = text.split()
        if len(fields) != 4:
            raise RuntimeError(f"ps returned an unparseable row: {text!r}")
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
            rss_kb = int(fields[2])
            cpu_percent = float(fields[3])
        except ValueError as exc:
            raise RuntimeError(f"ps returned an unparseable row: {text!r}") from exc
        rows.append(_PsRow(pid, ppid, rss_kb, cpu_percent))
    if not rows:
        raise RuntimeError("ps returned no rows")
    return rows


def _read_ps_by_pid() -> dict[int, _PsRow]:
    """One `%cpu` table from `/bin/ps`, keyed by pid (PERFMODE-14 `readPsTable`)."""
    try:
        completed = subprocess.run(
            [_PS_BIN, "-Ao", "pid=,ppid=,rss=,%cpu="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"failed to run {_PS_BIN} for %cpu sampling: {exc}") from exc
    return {row.pid: row for row in _parse_ps_table(completed.stdout)}


class _ProcessTreeSampler:
    """Samples footprint and CPU of a live process and its descendants.

    `mode_ratio_browser.mjs` drives Gig and Trackify in a Playwright-launched
    Chromium against the dev frontend -- NOT the packaged desktop app. The
    engine's `/api/v1/performance/telemetry/processes` endpoint reports the
    packaged app's own process family (desktop-shell / python-engine /
    webkit-webcontent) read from a native diagnostics log, which has nothing
    to do with this Chromium instance: sampling it would compute a
    footprint/CPU ratio over processes whose cost barely moves between
    modes, then publish that as "Trackify savings vs Gig" (claude-review
    finding on PR #3676). This sampler instead walks the actual OS process
    tree rooted at the browser subprocess's PID (Chromium's main process
    plus every renderer/GPU helper it spawns), which is where the
    AudioContexts, decoded PCM and waveform work this KPI is about actually
    live.

    CPU is the sum of each family member's `%cpu` from `/bin/ps -Ao
    pid=,ppid=,rss=,%cpu=`, the same instantaneous column PERFMODE-14's
    `sampleProcessFamilyFootprint` reads via `readPsTable()` (not psutil's
    delta `cpu_percent`, which would disagree with scorer=perfmode14 rows).

    Footprint is read via `DarwinProcessMetrics.read(pid).phys_footprint`,
    the same `proc_pid_rusage` counter Activity Monitor shows and the
    packaged app's own diagnostics probe uses -- NOT `psutil`'s
    `memory_info().rss`. Summing RSS across a multi-process Chromium tree
    double-counts pages the processes share (GPU shared memory, sandboxed
    IPC buffers, mapped V8 snapshot data), so a KPI card claiming
    "physical_footprint_mb" while actually summing RSS could pass or fail
    the PERFMODE-15 threshold on an artifact of that overcounting rather
    than a real mode difference (Codex review, PR #3676).

    Engine family (round 8, PERFMODE-15 spec): given `engine_root_pid`, the
    sample also counts the engine and every descendant of it (stem workers),
    by ancestry like PERFMODE-14's `descendantFamilyPids`, and reports the
    browser and engine halves separately. The root is pinned: psutil's
    `is_running()` compares create time, so an engine that exits or whose pid
    is reused mid-capture raises instead of sampling a different process. The
    leak capture passes no engine root (its KPI is browser-family only, per
    ADR-NEW-trackify-leak-kpi-quiescent-baselines).
    """

    def __init__(
        self,
        root_pid: int,
        *,
        engine_root_pid: int | None = None,
        native: DarwinProcessMetrics | None = None,
    ) -> None:
        self._root_pid = root_pid
        self._tracked: dict[int, psutil.Process] = {}
        self._engine_tracked: dict[int, psutil.Process] = {}
        self._engine_root = _pinned_engine_root(engine_root_pid)
        # Lazy real construction (ctypes, Darwin-only) so tests can inject a
        # duck-typed fake and stay runnable off-macOS, matching this repo's
        # existing DarwinProcessMetrics test convention (test_probe_process_
        # family.py's _FakeNative).
        self._native = native if native is not None else DarwinProcessMetrics()

    def _live_tree(self) -> list[psutil.Process]:
        try:
            root = psutil.Process(self._root_pid)
        except psutil.NoSuchProcess as exc:
            raise RuntimeError(
                f"mode_ratio_browser process {self._root_pid} is not running"
            ) from exc
        return _track(self._tracked, [root, *root.children(recursive=True)])

    def _engine_family(self) -> list[psutil.Process]:
        if self._engine_root is None:
            return []
        if not self._engine_root.is_running():
            raise RuntimeError(
                f"engine root pid {self._engine_root.pid} exited or was reused mid-capture"
            )
        family = [self._engine_root, *self._engine_root.children(recursive=True)]
        return _track(self._engine_tracked, family)

    def _read_footprint_mb(self, proc: psutil.Process) -> float:
        return self._native.read(proc.pid).phys_footprint / (1024 * 1024)

    def _cpu_percent(self, proc: psutil.Process, by_pid: dict[int, _PsRow]) -> float:
        row = by_pid.get(proc.pid)
        if row is not None:
            return row.cpu_percent
        if proc.is_running():
            raise RuntimeError(f"pid {proc.pid} exited mid-sample")
        raise psutil.NoSuchProcess(proc.pid)

    def _sum_live(
        self, procs: list[psutil.Process], by_pid: dict[int, _PsRow]
    ) -> tuple[float, float, int]:
        """Footprint and CPU of every process in `procs`; only one that has exited is skipped.

        A live process that cannot be read raises (Sol P1/BLOCKING, PR #4888):
        `DarwinProcessMetrics.read` raises ProcessLookupError for every failure,
        EPERM included, so a failed read counts as an exit only once
        `is_running()` confirms the process is gone. Anything else would drop
        a live member's cost from a row that still claims its whole family.
        """
        footprint_mb = cpu_percent = 0.0
        live = 0
        for proc in procs:
            try:
                proc_footprint = self._read_footprint_mb(proc)
                proc_cpu = self._cpu_percent(proc, by_pid)
            except (psutil.NoSuchProcess, ProcessLookupError) as exc:
                if proc.is_running():
                    raise RuntimeError(
                        f"pid {proc.pid} is still running but its footprint/CPU could not be read: {exc}"
                    ) from exc
                continue
            footprint_mb += proc_footprint
            cpu_percent += proc_cpu
            live += 1
        return footprint_mb, cpu_percent, live

    def sample(self) -> dict[str, float]:
        by_pid = _read_ps_by_pid()
        # `root` is the Node `mode_ratio_browser.mjs` launcher that SPAWNS
        # Chromium via Playwright, not a member of the Chromium family. Its
        # own fixed footprint and CPU would dilute both savings ratios with a
        # cost that barely moves between modes (Sol review, PR #3676) -- it is
        # walked for tree discovery (`_live_tree`) but excluded here.
        browser = [proc for proc in self._live_tree() if proc.pid != self._root_pid]
        engine = self._engine_family()
        overlap = {proc.pid for proc in browser} & {proc.pid for proc in engine}
        if overlap:
            raise RuntimeError(f"pids counted in both the browser and engine family: {sorted(overlap)}")
        browser_footprint, browser_cpu, live = self._sum_live(browser, by_pid)
        if live == 0:
            raise RuntimeError(
                f"mode_ratio_browser process tree rooted at {self._root_pid} "
                "has no live Chromium descendant to sample (only the launcher itself is running)"
            )
        engine_footprint = engine_cpu = 0.0
        if engine:
            # The root must read: a family whose root is unreadable is unmeasured, not small.
            engine_footprint = self._read_footprint_mb(engine[0])
            engine_cpu = self._cpu_percent(engine[0], by_pid)
            rest_footprint, rest_cpu, _ = self._sum_live(engine[1:], by_pid)
            engine_footprint += rest_footprint
            engine_cpu += rest_cpu
        return {
            "physical_footprint_mb": browser_footprint + engine_footprint,
            "cpu_percent": browser_cpu + engine_cpu,
            "browser_footprint_mb": browser_footprint,
            "browser_cpu_percent": browser_cpu,
            "engine_footprint_mb": engine_footprint,
            "engine_cpu_percent": engine_cpu,
            "engine_pid_count": float(len(engine)),
        }


def _pinned_engine_root(pid: int | None) -> psutil.Process | None:
    if pid is None:
        return None
    try:
        return psutil.Process(pid)
    except psutil.NoSuchProcess as exc:
        raise RuntimeError(f"engine root pid {pid} is not running") from exc


def _track(cache: dict[int, psutil.Process], tree: list[psutil.Process]) -> list[psutil.Process]:
    """Keep one psutil.Process per live pid for stable tree walks across samples."""
    seen_pids = {proc.pid for proc in tree}
    for pid in list(cache):
        if pid not in seen_pids:
            del cache[pid]
    for proc in tree:
        if proc.pid not in cache:
            cache[proc.pid] = proc
    return [cache[proc.pid] for proc in tree]
