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

from .probe_app_signals import browser_perf_ring, engine_metrics
from .probe_log_store import append_bounded_jsonl, summarize_logs
from .probe_native_metrics import (
    DarwinProcessMetrics,
    machine_metrics,
    process_metric_record,
    vmmap_summary,
)
from .probe_process_family import (
    WEBKIT_AND_SHELL_ROLES,
    associate_process_family,
    bundle_build_identity,
    find_shell,
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
    "append_bounded_jsonl",
    "browser_perf_ring",
    "main",
    "summarize_logs",
    "suspected_orphans",
]


class OpenDJProbe:
    def __init__(self, shell_pid: int | None, bundle_ids: tuple[str, ...]) -> None:
        self.requested_shell_pid = shell_pid
        self.bundle_ids = bundle_ids
        self.native = DarwinProcessMetrics()
        self.prior_cpu: dict[int, tuple[int, float]] = {}
        self.prior_io: dict[int, tuple[int, int]] = {}
        self.known_descendants: dict[int, tuple[str, str]] = {}
        self.build_by_shell_pid: dict[int, dict[str, Any]] = {}

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
            processes.append(
                process_metric_record(
                    row, role, usage, monotonic_now, self.prior_cpu, self.prior_io
                )
            )
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
        self.prior_cpu = {
            pid: value for pid, value in self.prior_cpu.items() if pid in live_pids
        }
        self.prior_io = {
            pid: value for pid, value in self.prior_io.items() if pid in live_pids
        }

    def _deep_vmmap(self, processes: list[dict[str, Any]]) -> dict[str, Any]:
        webcontent = next(
            (process for process in processes if process["role"] == "webkit-webcontent"),
            None,
        )
        if webcontent is None:
            return {"available": False, "reason": "WebContent not found"}
        return vmmap_summary(int(webcontent["pid"]))

    def sample(self, *, deep: bool) -> dict[str, Any]:
        monotonic_now = time.monotonic()
        rows = process_table()
        shell = find_shell(rows, self.requested_shell_pid)
        if shell is None:
            return self._app_not_running_record(rows)

        family, association = associate_process_family(rows, shell, self.native)
        build = self.build_by_shell_pid.setdefault(shell.pid, bundle_build_identity(shell))
        processes = self._family_metrics(family, monotonic_now)
        live_pids = {process["pid"] for process in processes}
        orphans = suspected_orphans(rows, live_pids, self.known_descendants)
        self._forget_dead_pids(rows, family, live_pids)

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
            "machine": machine_metrics(),
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
        "cpu_percent": round(
            sum(float(process["cpu_percent"] or 0) for process in processes), 2
        ),
        "process_count": len(processes),
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shell-pid",
        type=int,
        default=None,
        help="pin association to this OpenDJ shell PID",
    )
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
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 130
    finally:
        lock_handle.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    _reject_impossible_args(args)
    if args.summary:
        summary = summarize_logs(args.output_dir, args.summary_hours)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    try:
        probe = OpenDJProbe(args.shell_pid, tuple(args.bundle_ids or DEFAULT_BUNDLE_IDS))
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
