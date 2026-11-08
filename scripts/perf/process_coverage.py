"""Compare the in-app process monitor's RSS coverage against an independent ps family.

Why this exists: the performance KPI epic needs `monitor_coverage_pct`,
`idle_family_rss_mb`, and `idle_family_cpu_pct` without guessing. The monitor
sums live `members[].rss_mb` from `/api/v1/performance/telemetry/processes`; this
script builds the family independently from `ps` via the diagnostics probe and
reports coverage, per-role RSS, and mean family CPU over sampled intervals.

FILE REQUIREMENTS (mini-PRD)

* R1 coverage = monitor's summed live `members[].rss_mb` / ps family RSS, as a
  percent, 1 dp, never clamped at 100; exit 1 below `--min-coverage`, 0 at or
  above. Members carry NO pid; probe_log rows and totals are not counted; the
  monitor fetch is paired with the FIRST ps sample only.
  Status: planned.
* R2 nothing measured -> exit 2 and no percentage on stdout: connection refused
  -> "Open DJ is not running"; HTTP errors, unavailable body, ambiguous shells,
  non-macOS -> exit 2 with their own messages.
  Status: planned.
* R3 family CPU percent = mean over N-1 intervals of per-pid user+system deltas
  (not children_*, not ps %cpu); None with one sample.
  Status: planned.
* R4 `--json` emits exactly `coverage_pct`, `family_rss_mb`, `monitor_rss_mb`,
  `family_cpu_pct`, `by_role_mb`, `samples`. `by_role_mb` is the PS family breakdown.
  Status: planned.

Runs macOS-only on the ps side (DarwinProcessMetrics). Needs psutil: launch with
`uv run --no-sync python -m scripts.perf.process_coverage`, never `--no-project`.
WebKit helpers have ppid 1; only the probe's start-time association reaches them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise

import psutil

from scripts.diagnostics.probe_native_metrics import DarwinProcessMetrics
from scripts.diagnostics.probe_process_family import (
    ProcessRow,
    associate_process_family,
    find_shell,
    process_table,
    shell_candidates,
)
from scripts.diagnostics.probe_types import ProbeUnavailable, round_mb

EXIT_OK = 0
EXIT_BELOW = 1
EXIT_UNMEASURABLE = 2
DEFAULT_PORT = 7700
DEFAULT_MIN_COVERAGE = 95.0
DEFAULT_SAMPLES = 6
DEFAULT_INTERVAL_S = 5.0
TELEMETRY_PATH = "/api/v1/performance/telemetry/processes"
FETCH_TIMEOUT_S = 5.0
JSON_KEYS = (
    "coverage_pct",
    "family_rss_mb",
    "monitor_rss_mb",
    "family_cpu_pct",
    "by_role_mb",
    "samples",
)


class AppNotRunning(RuntimeError):
    """Nothing answered on the telemetry port."""


class MeasurementError(RuntimeError):
    """Something answered, but no coverage can be measured."""


@dataclass(frozen=True)
class FamilySample:
    rss_mb_by_pid: dict[int, float]
    cpu_seconds_by_pid: dict[int, float]
    taken_monotonic: float


def telemetry_url(port: int) -> str:
    return f"http://127.0.0.1:{port}{TELEMETRY_PATH}"


def fetch_telemetry_body(port: int, timeout_s: float = FETCH_TIMEOUT_S) -> dict[str, object]:
    url = telemetry_url(port)
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise MeasurementError(
            f"port {port} answered HTTP {exc.code} for {TELEMETRY_PATH}; "
            "not an Open DJ backend with process telemetry"
        ) from exc
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AppNotRunning(f"nothing answered on {url} ({reason})") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MeasurementError(f"port {port} returned non-JSON body for {TELEMETRY_PATH}") from exc
    if not isinstance(parsed, dict):
        raise MeasurementError(f"port {port} returned a non-object JSON body for {TELEMETRY_PATH}")
    return parsed


def monitor_rss_mb(body: dict[str, object]) -> float:
    if body.get("available") is not True:
        reason = body.get("reason", "no reason given")
        raise MeasurementError(f"the monitor reports no process telemetry: {reason}")
    members = body.get("members")
    if not isinstance(members, list):
        raise MeasurementError("the monitor body has no members list")
    total = 0.0
    for row in members:
        if not isinstance(row, dict):
            continue
        rss = row.get("rss_mb")
        if isinstance(rss, (int, float)) and not isinstance(rss, bool):
            total += float(rss)
    return round(total, 1)


def _reject_ambiguous_shell(rows: list[ProcessRow], shell_pid: int | None) -> None:
    if shell_pid is not None:
        return
    candidates = shell_candidates(rows)
    if len(candidates) > 1:
        pids = ", ".join(str(row.pid) for row in sorted(candidates, key=lambda row: row.pid))
        raise MeasurementError(
            f"{len(candidates)} Open DJ desktop shells are running (pids {pids}). "
            "Pass --shell-pid to pick one; measuring without a pin would "
            "silently compare whichever shell happens to have the lowest pid."
        )


def discover_family(shell_pid: int | None) -> tuple[int, list[tuple[int, str]]]:
    try:
        native = DarwinProcessMetrics()
    except ProbeUnavailable as exc:
        raise MeasurementError(
            f"the ps-side family association is macOS-only ({exc})"
        ) from exc
    rows = process_table()
    _reject_ambiguous_shell(rows, shell_pid)
    shell = find_shell(rows, shell_pid)
    if shell is None:
        suffix = "" if shell_pid is None else f" with pid {shell_pid}"
        raise MeasurementError(
            f"no Open DJ desktop shell is running{suffix} "
            "(looked for .../MacOS/opendj-desktop in ps)"
        )
    family, _assoc = associate_process_family(rows, shell, native)
    return shell.pid, [(row.pid, role) for row, role in family]


def sample_family(family: list[tuple[int, str]]) -> FamilySample:
    rss_mb_by_pid: dict[int, float] = {}
    cpu_seconds_by_pid: dict[int, float] = {}
    for pid, _role in family:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue
        with proc.oneshot():
            rss_mb_by_pid[pid] = round_mb(proc.memory_info().rss)
            times = proc.cpu_times()
            cpu_seconds_by_pid[pid] = float(times.user) + float(times.system)
    return FamilySample(
        rss_mb_by_pid=rss_mb_by_pid,
        cpu_seconds_by_pid=cpu_seconds_by_pid,
        taken_monotonic=time.monotonic(),
    )


def collect_samples(
    family: list[tuple[int, str]],
    samples: int,
    interval_s: float,
    sleep: Callable[[float], None] = time.sleep,
) -> list[FamilySample]:
    collected: list[FamilySample] = []
    for index in range(samples):
        if index > 0:
            sleep(interval_s)
        collected.append(sample_family(family))
    return collected


def family_cpu_pct(samples: list[FamilySample]) -> float | None:
    """Mean CPU use as percent of one core over sampled intervals (like top)."""

    if len(samples) < 2:
        return None
    interval_percents: list[float] = []
    for before, after in pairwise(samples):
        wall = after.taken_monotonic - before.taken_monotonic
        if wall <= 0:
            continue
        delta_cpu = 0.0
        for pid, before_cpu in before.cpu_seconds_by_pid.items():
            after_cpu = after.cpu_seconds_by_pid.get(pid)
            if after_cpu is None:
                continue
            delta_cpu += after_cpu - before_cpu
        interval_percents.append(delta_cpu / wall * 100.0)
    if not interval_percents:
        return None
    return round(sum(interval_percents) / len(interval_percents), 1)


def coverage_pct(monitor_mb: float, family_mb: float) -> float:
    if family_mb <= 0:
        raise MeasurementError("ps family RSS is zero; nothing to compare against")
    return round(monitor_mb / family_mb * 100.0, 1)


def by_role_mb(
    family: list[tuple[int, str]],
    sample: FamilySample,
) -> dict[str, float]:
    totals: dict[str, float] = {}
    for pid, role in family:
        rss = sample.rss_mb_by_pid.get(pid)
        if rss is None:
            continue
        totals[role] = round(totals.get(role, 0.0) + rss, 1)
    return dict(sorted(totals.items(), key=lambda item: (-item[1], item[0])))


def build_report(
    monitor_mb: float,
    family: list[tuple[int, str]],
    samples: list[FamilySample],
) -> dict[str, object]:
    if not samples:
        raise MeasurementError("no ps samples were collected")
    family_mb = round(sum(samples[0].rss_mb_by_pid.values()), 1)
    coverage = coverage_pct(monitor_mb, family_mb)
    return {
        "coverage_pct": coverage,
        "family_rss_mb": family_mb,
        "monitor_rss_mb": monitor_mb,
        "family_cpu_pct": family_cpu_pct(samples),
        "by_role_mb": by_role_mb(family, samples[0]),
        "samples": len(samples),
    }


def _render_text(
    report: dict[str, object],
    *,
    port: int,
    shell_pid: int,
    min_coverage: float,
    interval_s: float,
) -> str:
    lines = [f"Open DJ process coverage (port {port}, shell pid {shell_pid})"]
    by_role = report.get("by_role_mb")
    if isinstance(by_role, dict):
        for role, mb in by_role.items():
            lines.append(f"  {role:<22} {mb:.1f} MB")
    monitor_mb = report["monitor_rss_mb"]
    family_mb = report["family_rss_mb"]
    cov = report["coverage_pct"]
    lines.append(f"monitor covered: {monitor_mb:.1f} MB")
    lines.append(f"ps family:       {family_mb:.1f} MB")
    lines.append(f"coverage: {cov:.1f} percent (minimum {min_coverage:.1f})")
    cpu = report.get("family_cpu_pct")
    sample_count = report.get("samples")
    if cpu is None:
        lines.append("family CPU: n/a (one sample; pass --samples 2 or more)")
    elif isinstance(cpu, (int, float)) and isinstance(sample_count, int) and sample_count > 1:
        intervals = sample_count - 1
        lines.append(
            f"family CPU: {cpu:.1f} percent of one core, "
            f"mean of {intervals} intervals of {interval_s:.1f} s"
        )
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=(
            "backend port for telemetry (default 7700 = desktop PREFERRED_ENGINE_PORT; "
            "pass a worktree backend port when measuring dev)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=DEFAULT_MIN_COVERAGE,
        help="exit 1 when coverage percent is below this threshold (default 95)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLES,
        help="number of ps family samples for RSS and CPU (default 6)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help="seconds between ps samples (default 5)",
    )
    parser.add_argument(
        "--shell-pid",
        type=int,
        default=None,
        help="pin family discovery to this Open DJ shell pid when two builds run",
    )
    args = parser.parse_args(argv)
    if args.samples < 1:
        parser.error("--samples must be at least 1")
    if args.interval < 0:
        parser.error("--interval must be non-negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        body = fetch_telemetry_body(args.port)
        monitor = monitor_rss_mb(body)
        shell_pid, family = discover_family(args.shell_pid)
        samples = collect_samples(family, args.samples, args.interval)
        report = build_report(monitor, family, samples)
    except AppNotRunning as exc:
        print(
            f"[ERROR] Open DJ is not running: {exc}. "
            "Start the app, or pass --port for a worktree backend.",
            file=sys.stderr,
        )
        return EXIT_UNMEASURABLE
    except MeasurementError as exc:
        print(f"[ERROR] cannot measure coverage: {exc}", file=sys.stderr)
        return EXIT_UNMEASURABLE

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            _render_text(
                report,
                port=args.port,
                shell_pid=shell_pid,
                min_coverage=args.min_coverage,
                interval_s=args.interval,
            )
        )

    coverage = float(report["coverage_pct"])
    if coverage < args.min_coverage:
        print(
            f"[FAIL] coverage {coverage:.1f} percent is below the {args.min_coverage:.1f} percent minimum",
            file=sys.stderr,
        )
        return EXIT_BELOW
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
