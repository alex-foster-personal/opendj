#!/usr/bin/env python3
"""Low-overhead, bounded OpenDJ process-family telemetry for macOS.

The browser cannot see the Rust shell, Python engine, or WebKit helper
footprints.  This probe reads Darwin's ``proc_pid_rusage`` counters directly,
which are the same physical-footprint counters shown by Activity Monitor.
It is useful both as a one-shot diagnostic and as a 15-second JSONL sampler.

The process-family association is explicit and inspectable:

* the Python engine and workers are descendants of the OpenDJ shell;
* WebKit helpers are the first helper cluster launched within 45 seconds of
  that shell, with a three-second cluster window;
* every sample records the association rule and each included command.

Deep ``vmmap`` categorization is deliberately infrequent because it is much
more expensive than ``proc_pid_rusage``.  Logs rotate daily, are owner-only,
and stop appending at the configured cap instead of growing without bound.

This module is the sampler and CLI; the collectors it composes live beside it:

* ``probe_types`` -- shared constants, ``ProcessRow``, byte/time helpers;
* ``probe_native_metrics`` -- ``proc_pid_rusage``, ``vmmap``, ``sysctl``;
* ``probe_process_family`` -- which PIDs belong to this app, and by what rule;
* ``probe_app_signals`` -- engine HTTP and the WebKit perf ring;
* ``probe_log_store`` -- bounded JSONL append and the trend summary.

RUN IT AS A MODULE: the pieces import each other relatively, so the entry point
is ``python3 -m <package>.opendj_performance_probe`` with the package's parent
directory on ``sys.path``. ``just probe-once`` and the launchd job both do that;
running this file by path will not resolve the sibling modules.

INTERPRETER FLOOR: this package runs under ``/usr/bin/python3``, which is 3.9 on
current macOS, and it is deliberately stdlib-only. That is what lets the launchd
job keep sampling on a machine with no repo checkout, no venv, and no network.
So: no 3.10+/3.11+ runtime syntax, whatever ruff's ``target-version`` says.
``datetime.UTC`` (3.11) and ``zip(strict=)`` (3.10) are the two the linter keeps
proposing; both carry a ``noqa`` and a reason. Annotations are exempt, because
``from __future__ import annotations`` defers them.

Install and uninstall are documented in ``docs/perf/diagnostics-probe.md``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .probe_app_signals import (
    TrendReportError,
    browser_perf_ring,
    engine_metrics,
    report_red_trend,
)
from .probe_log_store import append_bounded_jsonl, summarize_logs, trend_logs
from .probe_native_metrics import (
    DarwinProcessMetrics,
    churn_metrics_from_snapshots,
    machine_metrics,
    process_metric_record,
    vm_stat_churn_snapshot,
    vmmap_summary,
)
from .probe_process_family import (
    WEBKIT_AND_SHELL_ROLES,
    associate_process_family,
    bundle_build_identity,
    find_shell,
    opendj_process_name,
    process_table,
    suspected_orphans,
)
from .probe_types import (
    DEFAULT_BUNDLE_IDS,
    DEFAULT_DEEP_INTERVAL_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_LOG_CAP_BYTES,
    DEFAULT_OUTPUT_DIR,
    MIB,
    ProbeUnavailable,
    ProcessRow,
    utc_now,
)

# Re-exported so ``from ...opendj_performance_probe import X`` stays the one
# import site for callers and tests, whichever collector module owns X.
__all__ = [
    "DEFAULT_BUNDLE_IDS",
    "OpenDJProbe",
    "ProbeUnavailable",
    "ProcessRow",
    "TrendReportError",
    "append_bounded_jsonl",
    "browser_perf_ring",
    "main",
    "report_red_trend",
    "summarize_logs",
    "suspected_orphans",
    "trend_logs",
]

# A RED verdict exits 1. This is the separate case where the verdict is RED AND
# the durable client-error record of it could not be written, so a caller can
# tell "the app regressed" from "the app regressed and nothing recorded it".
TREND_REPORT_FAILURE_EXIT = 3

KERNEL_ELEVATED_LEVEL = 2
PRESSURE_CHURN_EARLY_WARNING = 500
PRESSURE_SAMPLE_ELEVATED_MS = 5000


class OpenDJProbe:
    def __init__(
        self,
        shell_pid: int | None,
        bundle_ids: tuple[str, ...],
        pid: int | None = None,
        match: str | None = None,
    ) -> None:
        self.requested_shell_pid = shell_pid
        self.bundle_ids = bundle_ids
        self.pid = pid
        self.match = match
        self.native = DarwinProcessMetrics()
        self.prior_cpu: dict[int, tuple[int, float]] = {}
        self.prior_io: dict[int, tuple[int, int]] = {}
        self.known_descendants: dict[int, tuple[str, str]] = {}
        self.build_by_shell_pid: dict[int, dict[str, Any]] = {}
        self._prior_vm_stat: dict[str, int] | None = None
        self._prior_vm_stat_at: float | None = None

    def _app_not_running_record(self, rows: list[ProcessRow]) -> dict[str, Any]:
        orphans = suspected_orphans(rows, set(), self.known_descendants)
        return {
            "schema_version": 1,
            "kind": "app-not-running",
            "timestamp": utc_now(),
            "requested_shell_pid": self.requested_shell_pid,
            "machine": machine_metrics(),
            "suspected_orphan_count": len(orphans),
            "suspected_orphans": orphans,
        }

    def _family_metrics(
        self, family: list[tuple[ProcessRow, str]], monotonic_now: float
    ) -> list[dict[str, Any]]:
        """One metric record per still-live family member."""

        processes: list[dict[str, Any]] = []
        for row, role in family:
            try:
                usage = self.native.read(row.pid)
            except ProcessLookupError:
                continue
            record = process_metric_record(
                row, role, usage, monotonic_now, self.prior_cpu, self.prior_io
            )
            record["name"] = opendj_process_name(row.command)
            processes.append(record)
        return processes

    def _forget_dead_pids(
        self,
        rows: list[ProcessRow],
        family: list[tuple[ProcessRow, str]],
        live_pids: set[int],
    ) -> None:
        """Carry only what the next sample can still use, so state cannot grow."""

        self.known_descendants.update(
            {
                row.pid: (row.command, role)
                for row, role in family
                if role not in WEBKIT_AND_SHELL_ROLES
            }
        )
        alive = {row.pid for row in rows}
        self.known_descendants = {
            pid: value for pid, value in self.known_descendants.items() if pid in alive
        }
        self.prior_cpu = {pid: value for pid, value in self.prior_cpu.items() if pid in live_pids}
        self.prior_io = {pid: value for pid, value in self.prior_io.items() if pid in live_pids}

    def _deep_vmmap(self, processes: list[dict[str, Any]]) -> dict[str, Any]:
        webcontents = [process for process in processes if process["role"] == "webkit-webcontent"]
        if not webcontents:
            return {"available": False, "reason": "WebContent not found"}
        profiled_pid = int(webcontents[0]["pid"])
        result = vmmap_summary(profiled_pid)
        if len(webcontents) > 1:
            result["partial"] = True
            result["profiled_pid"] = profiled_pid
            result["unprofiled_webcontent_pids"] = [
                int(process["pid"]) for process in webcontents[1:]
            ]
        return result

    def sample(self, *, deep: bool) -> dict[str, Any]:
        monotonic_now = time.monotonic()
        rows = process_table()
        shell = find_shell(
            rows,
            self.pid if self.pid is not None else self.requested_shell_pid,
        )
        if self.pid is not None and shell is None:
            shell = next((row for row in rows if row.pid == self.pid), None)
        if self.match is not None:
            matches = [row for row in rows if self.match in row.command]
            if len(matches) != 1:
                candidate_pids = ", ".join(str(row.pid) for row in matches) or "none"
                raise ProbeUnavailable(
                    f"--match {self.match!r} matched {len(matches)} processes; "
                    f"candidate PIDs: {candidate_pids}; use --pid to select one"
                )
            shell = matches[0]
        if shell is None:
            return self._app_not_running_record(rows)

        if self.pid is None and self.match is None:
            family, association = associate_process_family(rows, shell, self.native)
        else:
            descendant_pids = _descendant_pids(rows, shell.pid)
            selected = [row for row in rows if row.pid == shell.pid or row.pid in descendant_pids]
            family = [
                (row, "selected-root" if row.pid == shell.pid else "selected-descendant")
                for row in selected
            ]
            association = {
                "selector": "pid" if self.pid is not None else "match",
                "selector_value": self.pid if self.pid is not None else self.match,
                "descendant_rule": "recursive PPID ancestry from selected root",
                "included_pids": [row.pid for row in selected],
            }
        build = self.build_by_shell_pid.setdefault(shell.pid, bundle_build_identity(shell))
        processes = self._family_metrics(family, monotonic_now)
        live_pids = {process["pid"] for process in processes}
        orphans = suspected_orphans(rows, live_pids, self.known_descendants)
        self._forget_dead_pids(rows, family, live_pids)

        machine = machine_metrics()
        vm_snapshot = vm_stat_churn_snapshot()
        if vm_snapshot is not None:
            elapsed = (
                0.0
                if self._prior_vm_stat_at is None
                else max(0.0, monotonic_now - self._prior_vm_stat_at)
            )
            machine.update(
                churn_metrics_from_snapshots(self._prior_vm_stat, vm_snapshot, elapsed)
            )
            self._prior_vm_stat = vm_snapshot
            self._prior_vm_stat_at = monotonic_now
        record: dict[str, Any] = {
            "schema_version": 1,
            "kind": "sample",
            "timestamp": utc_now(),
            "monotonic_seconds": round(monotonic_now, 3),
            "shell_pid": shell.pid,
            "association": association,
            "build": build,
            "totals": _sample_totals(processes),
            "processes": processes,
            "machine": machine,
            "engine": engine_metrics(family),
            "browser_perf_ring": browser_perf_ring(self.bundle_ids),
            "suspected_orphan_count": len(orphans),
            "suspected_orphans": orphans,
        }
        if deep:
            record["deep_vmmap"] = self._deep_vmmap(processes)
        return record


def _sample_totals(processes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "physical_footprint_mb": round(
            sum(float(process["physical_footprint_mb"]) for process in processes), 1
        ),
        "summed_lifetime_peak_mb": round(
            sum(float(process["peak_physical_footprint_mb"]) for process in processes), 1
        ),
        "cpu_percent": round(sum(float(process["cpu_percent"] or 0) for process in processes), 2),
        "process_count": len(processes),
    }


def _descendant_pids(rows: list[ProcessRow], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    found: set[int] = set()
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        if pid not in found:
            found.add(pid)
            pending.extend(children.get(pid, []))
    return found


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shell-pid",
        type=int,
        default=None,
        help="pin association to this OpenDJ shell PID",
    )
    parser.add_argument("--pid", type=int, default=None, help="sample this process and descendants")
    parser.add_argument(
        "--match",
        default=None,
        help="sample the first command containing this text and descendants",
    )
    parser.add_argument(
        "command", nargs="?", choices=("trend",), help="read a trend from existing JSONL"
    )
    parser.add_argument("--since", default=None, help="ISO 8601 trend start, required for trend")
    parser.add_argument(
        "--bundle-id",
        action="append",
        dest="bundle_ids",
        default=None,
        help=(
            "WebKit bundle id whose perf ring is read; repeatable. Unset tries "
            + ", ".join(DEFAULT_BUNDLE_IDS)
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="seconds between cheap samples",
    )
    parser.add_argument(
        "--deep-every",
        type=float,
        default=DEFAULT_DEEP_INTERVAL_SECONDS,
        help="seconds between vmmap samples; 0 disables",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-log-mb", type=float, default=DEFAULT_LOG_CAP_BYTES / MIB)
    parser.add_argument(
        "--max-samples",
        type=int,
        default=0,
        help="stop after N samples; 0 means continuous",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="print one deep sample and exit without writing JSONL",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print an aggregate trend summary and exit",
    )
    parser.add_argument(
        "--summary-hours",
        type=float,
        default=24.0,
        help="lookback window for --summary",
    )
    return parser.parse_args(argv)


def _lock_probe(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / "opendj-performance-probe.lock"
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"another probe owns {lock_path}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    os.fchmod(handle.fileno(), 0o600)
    return handle


def _reject_impossible_args(args: argparse.Namespace) -> None:
    """Refuse a nonsense configuration up front, never clamp it silently."""

    if args.interval < 1:
        raise SystemExit("--interval must be at least 1 second")
    if args.deep_every < 0:
        raise SystemExit("--deep-every cannot be negative")
    if args.max_log_mb <= 0:
        raise SystemExit("--max-log-mb must be positive")
    if args.summary_hours <= 0:
        raise SystemExit("--summary-hours must be positive")
    if args.pid is not None and args.pid <= 0:
        raise SystemExit("--pid must be positive")
    if args.command == "trend" and args.since is None:
        raise SystemExit("trend requires --since <ISO>")
    if args.pid is not None and args.match is not None:
        raise SystemExit("--pid and --match are mutually exclusive")
    mode_conflict = _trend_mode_conflict(args)
    if mode_conflict is not None:
        raise SystemExit(mode_conflict)


def _trend_mode_conflict(args: argparse.Namespace) -> str | None:
    """Name every argument this mode accepts but would not act on, or None.

    An accepted-and-ignored argument is the quiet kind of wrong: `trend
    --summary --since X` used to take the --summary branch and exit 0, so a RED
    verdict never ran, and a trend given `--pid` computed its verdict over
    every process family in the JSONL rather than the one named. Both looked
    like a working command.
    """

    if args.command != "trend":
        return None if args.since is None else "--since only applies to the trend command"
    if args.summary:
        return "trend and --summary are mutually exclusive"
    # These select WHICH process family gets SAMPLED. Trend mode reads process
    # rows already written to JSONL, so it cannot honor them after the fact.
    supplied = [
        flag
        for flag, value in (
            ("--pid", args.pid),
            ("--match", args.match),
            ("--shell-pid", args.shell_pid),
            ("--bundle-id", args.bundle_ids),
        )
        if value is not None
    ]
    if not supplied:
        return None
    return (
        f"{', '.join(supplied)} select a sampled process family and are not applied "
        "by trend, which reads process rows already written to JSONL"
    )


def _sample_once_or_report_error(probe: OpenDJProbe, deep: bool) -> dict[str, Any]:
    """One sample, or the real error as a record: the log must not go silent."""

    try:
        return probe.sample(deep=deep)
    except Exception as exc:  # a KeepAlive job logs the failure, it does not die
        return {
            "schema_version": 1,
            "kind": "probe-error",
            "timestamp": utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _machine_sample_elevated(machine: object) -> bool:
    if not isinstance(machine, dict):
        return False
    level = machine.get("kernel_memory_pressure_level")
    if isinstance(level, int) and level >= KERNEL_ELEVATED_LEVEL:
        return True
    churn = machine.get("churn_score")
    return isinstance(churn, (int, float)) and churn >= PRESSURE_CHURN_EARLY_WARNING


def _probe_sleep_seconds(args: argparse.Namespace, record: dict[str, Any], started: float) -> float:
    base = max(0.0, args.interval - (time.monotonic() - started))
    if not _machine_sample_elevated(record.get("machine")):
        return base
    return max(base, PRESSURE_SAMPLE_ELEVATED_MS / 1000.0)


def _print_loop_line(record: dict[str, Any], path: Path, stored: bool) -> None:
    totals = record.get("totals", {})
    print(
        json.dumps(
            {
                "timestamp": record.get("timestamp"),
                "kind": record.get("kind"),
                "physical_footprint_mb": totals.get("physical_footprint_mb"),
                "cpu_percent": totals.get("cpu_percent"),
                "stored": stored,
                "path": str(path),
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _run_sampling_loop(probe: OpenDJProbe, args: argparse.Namespace) -> int:
    lock_handle = _lock_probe(args.output_dir)
    sample_count = 0
    next_deep = time.monotonic()
    cap_bytes = int(args.max_log_mb * MIB)
    try:
        while True:
            started = time.monotonic()
            deep = args.deep_every > 0 and started >= next_deep
            record = _sample_once_or_report_error(probe, deep)
            if deep:
                next_deep = started + args.deep_every
            path, stored = append_bounded_jsonl(args.output_dir, record, cap_bytes)
            _print_loop_line(record, path, stored)
            sample_count += 1
            if args.max_samples > 0 and sample_count >= args.max_samples:
                return 0
            time.sleep(_probe_sleep_seconds(args, record, started))
    except KeyboardInterrupt:
        return 130
    finally:
        lock_handle.close()


def _set_probe_process_identity() -> None:
    """Best-effort opendj-* title when setproctitle is available on the host."""
    try:
        import setproctitle
    except ImportError:
        return
    setproctitle.setproctitle(
        "opendj-performance-probe --name opendj-performance-probe"
    )


def main(argv: list[str] | None = None) -> int:
    _set_probe_process_identity()
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    _reject_impossible_args(args)
    if args.summary:
        summary = summarize_logs(args.output_dir, args.summary_hours)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.command == "trend":
        trend = trend_logs(args.output_dir, args.since)
        engine_port = trend.get("engine_port")
        report_failed = False
        if trend.get("verdict") == "RED":
            # Every RED verdict must produce a durable client-error record or
            # say why it could not. A window whose samples never captured an
            # integer engine port has nowhere to post, and that is a report
            # failure with its own exit code, not a plain RED (Sol P1, #1322).
            if not isinstance(engine_port, int):
                trend["client_error_posted"] = {
                    "posted": False,
                    "error": (
                        "no sample in the window recorded an integer engine port; "
                        "the RED verdict could not be posted"
                    ),
                }
                report_failed = True
            else:
                try:
                    trend["client_error_posted"] = report_red_trend(engine_port, trend)
                except TrendReportError as exc:
                    # The verdict itself is still true and still printed. What
                    # failed is the durable RECORD of it, and that has its own
                    # exit code so a caller can tell "app regressed" from "app
                    # regressed and nothing wrote it down".
                    trend["client_error_posted"] = {"posted": False, "error": str(exc)}
                    report_failed = True
        print(json.dumps(trend, ensure_ascii=False, sort_keys=True, indent=2))
        if report_failed:
            return TREND_REPORT_FAILURE_EXIT
        return int(trend["exit_code"])
    try:
        probe = OpenDJProbe(
            args.shell_pid, tuple(args.bundle_ids or DEFAULT_BUNDLE_IDS), args.pid, args.match
        )
    except ProbeUnavailable as exc:
        print(json.dumps({"available": False, "reason": str(exc)}))
        return 2
    if args.once:
        sample = probe.sample(deep=True)
        print(json.dumps(sample, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    return _run_sampling_loop(probe, args)


if __name__ == "__main__":
    raise SystemExit(main())
