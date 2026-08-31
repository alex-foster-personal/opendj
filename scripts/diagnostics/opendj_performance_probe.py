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

INTERPRETER FLOOR: this file runs under ``/usr/bin/python3``, which is 3.9 on
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
import ctypes
import fcntl
import json
import os
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIB = 1024 * 1024
DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_DEEP_INTERVAL_SECONDS = 300.0
DEFAULT_LOG_CAP_BYTES = 20 * MIB
DEFAULT_OUTPUT_DIR = Path.home() / ".local/share/music-dj-tools/performance"
# macOS derives the WebKit storage path from the bundle identifier, so the
# perf ring moves when the identifier does. The bake-off ended Sat 29 Aug 2026
# (OPS-08): the plain product is now the default and the lane-b build is what
# is still installed on machines that have not taken a new dmg. Both are tried,
# newest-write-wins, the same candidate-list shape as virgin_boot.sh.
DEFAULT_BUNDLE_IDS = ("com.opendj.desktop", "com.opendj.desktop.lane-b")
WEBKIT_ASSOCIATION_WINDOW_SECONDS = 45.0
WEBKIT_CLUSTER_WINDOW_SECONDS = 3.0

SHELL_MARKERS = (
    "/Applications/Open DJ",
    "/MacOS/opendj-desktop",
)
WEBKIT_MARKER = "/WebKit.framework/"
WEBKIT_ROLES = {
    "com.apple.WebKit.WebContent": "webkit-webcontent",
    "com.apple.WebKit.GPU": "webkit-gpu",
    "com.apple.WebKit.Networking": "webkit-networking",
}
# Roles that are re-derived from scratch every sample. Only the roles OUTSIDE
# this set are remembered across samples, because those are the ones that can
# outlive their shell and turn into the orphans the probe is looking for.
WEBKIT_AND_SHELL_ROLES = frozenset(
    {"desktop-shell", "webkit-other", *WEBKIT_ROLES.values()}
)


class ProbeUnavailable(RuntimeError):
    """The requested native metric is unavailable on this platform."""


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    pgid: int
    command: str


class RUsageInfoV4(ctypes.Structure):
    """Darwin ``struct rusage_info_v4`` from ``sys/resource.h``."""

    _fields_ = [("uuid", ctypes.c_ubyte * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time",
            "system_time",
            "pkg_idle_wkups",
            "interrupt_wkups",
            "pageins",
            "wired_size",
            "resident_size",
            "phys_footprint",
            "proc_start_abstime",
            "proc_exit_abstime",
            "child_user_time",
            "child_system_time",
            "child_pkg_idle_wkups",
            "child_interrupt_wkups",
            "child_pageins",
            "child_elapsed_abstime",
            "diskio_bytesread",
            "diskio_byteswritten",
            "cpu_time_qos_default",
            "cpu_time_qos_maintenance",
            "cpu_time_qos_background",
            "cpu_time_qos_utility",
            "cpu_time_qos_legacy",
            "cpu_time_qos_user_initiated",
            "cpu_time_qos_user_interactive",
            "billed_system_time",
            "serviced_system_time",
            "logical_writes",
            "lifetime_max_phys_footprint",
            "instructions",
            "cycles",
            "billed_energy",
            "serviced_energy",
            "interval_max_phys_footprint",
            "runnable_time",
        )
    ]


class MachTimebaseInfo(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class DarwinProcessMetrics:
    """Read cheap per-process counters without spawning profiler processes."""

    RUSAGE_INFO_V4 = 4

    def __init__(self) -> None:
        if platform.system() != "Darwin":
            raise ProbeUnavailable("proc_pid_rusage is only available on macOS")
        self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._rusage = self._libproc.proc_pid_rusage
        self._rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self._rusage.restype = ctypes.c_int

        self._libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self._mach_timebase = self._libsystem.mach_timebase_info
        self._mach_timebase.argtypes = [ctypes.POINTER(MachTimebaseInfo)]
        self._mach_timebase.restype = ctypes.c_int
        timebase = MachTimebaseInfo()
        if self._mach_timebase(ctypes.byref(timebase)) != 0 or timebase.denom == 0:
            raise ProbeUnavailable("mach_timebase_info failed")
        self._nanoseconds_per_tick = timebase.numer / timebase.denom

    def read(self, pid: int) -> RUsageInfoV4:
        info = RUsageInfoV4()
        ctypes.set_errno(0)
        if self._rusage(pid, self.RUSAGE_INFO_V4, ctypes.byref(info)) != 0:
            errno = ctypes.get_errno()
            raise ProcessLookupError(errno, os.strerror(errno), pid)
        return info

    def ticks_to_seconds(self, ticks: int) -> float:
        return ticks * self._nanoseconds_per_tick / 1_000_000_000


def _run_text(command: list[str], timeout: float = 4.0) -> str:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout


def process_table() -> list[ProcessRow]:
    output = _run_text(["ps", "-axo", "pid=,ppid=,pgid=,command="], timeout=5.0)
    rows: list[ProcessRow] = []
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)$", line)
        if not match:
            continue
        rows.append(
            ProcessRow(
                pid=int(match.group(1)),
                ppid=int(match.group(2)),
                pgid=int(match.group(3)),
                command=match.group(4),
            )
        )
    return rows


def _is_shell(row: ProcessRow) -> bool:
    return any(marker in row.command for marker in SHELL_MARKERS)


def find_shell(rows: Iterable[ProcessRow], requested_pid: int | None) -> ProcessRow | None:
    candidates = [row for row in rows if _is_shell(row)]
    if requested_pid is not None:
        return next((row for row in candidates if row.pid == requested_pid), None)
    return min(candidates, key=lambda row: row.pid) if candidates else None


def descendants(rows: Iterable[ProcessRow], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    found: set[int] = set()
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(children.get(pid, []))
    return found


def webkit_role(command: str) -> str | None:
    if WEBKIT_MARKER not in command:
        return None
    for marker, role in WEBKIT_ROLES.items():
        if marker in command:
            return role
    return "webkit-other"


def process_role(row: ProcessRow, shell_pid: int, descendant_pids: set[int]) -> str:
    if row.pid == shell_pid:
        return "desktop-shell"
    role = webkit_role(row.command)
    if role is not None:
        return role
    if row.pid in descendant_pids:
        if "apps.engine_core serve" in row.command:
            return "python-engine"
        if "worker" in row.command or "agent" in row.command:
            return "engine-worker"
        return "shell-descendant"
    return "other"


def _open_dj_bundle_root(command: str) -> str | None:
    start = command.find("/Applications/Open DJ")
    if start < 0:
        return None
    end = command.find(".app/", start)
    if end < 0:
        return None
    return command[start : end + len(".app/")]


def bundle_build_identity(shell: ProcessRow) -> dict[str, Any]:
    root = _open_dj_bundle_root(shell.command)
    if root is None:
        return {"available": False, "reason": "app bundle root not found"}
    manifest = Path(root) / "Contents/Resources/payload/manifest.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"available": False, "reason": "payload manifest unavailable"}
    identity = value.get("identity") if isinstance(value, dict) else None
    if not isinstance(identity, dict):
        return {"available": False, "reason": "payload manifest has no identity"}
    allowed = {
        "app_version",
        "built_at_utc",
        "bundle_identifier",
        "engine_version",
        "git_branch",
        "git_dirty",
        "git_sha",
        "git_sha_full",
        "lane_label",
        "product_name",
    }
    return {
        "available": True,
        **{key: identity[key] for key in sorted(allowed) if key in identity},
    }


def suspected_orphans(
    rows: list[ProcessRow],
    selected_pids: set[int],
    known_descendants: dict[int, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Find same-bundle or formerly-associated children outside the family."""

    by_pid = {row.pid: row for row in rows}
    live_shells = [row for row in rows if _is_shell(row)]
    live_roots = {
        root: descendants(rows, shell.pid)
        for shell in live_shells
        if (root := _open_dj_bundle_root(shell.command)) is not None
    }
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row.pid in selected_pids:
            continue
        root = _open_dj_bundle_root(row.command)
        if root is None or "/Contents/Resources/payload/" not in row.command:
            continue
        if row.pid not in live_roots.get(root, set()):
            found[row.pid] = {
                "pid": row.pid,
                "ppid": row.ppid,
                "reason": "same OpenDJ app payload is not a descendant of its live shell",
                "command": row.command,
            }
    for pid, (command, role) in known_descendants.items():
        row = by_pid.get(pid)
        if row is None or pid in selected_pids or row.command != command:
            continue
        found[pid] = {
            "pid": pid,
            "ppid": row.ppid,
            "reason": f"previously associated {role} is still alive outside the current family",
            "command": row.command,
        }
    return [found[pid] for pid in sorted(found)]


def associate_process_family(
    rows: list[ProcessRow],
    shell: ProcessRow,
    native: DarwinProcessMetrics,
) -> tuple[list[tuple[ProcessRow, str]], dict[str, Any]]:
    descendant_pids = descendants(rows, shell.pid)
    selected: dict[int, tuple[ProcessRow, str]] = {
        shell.pid: (shell, "desktop-shell")
    }
    for row in rows:
        if row.pid in descendant_pids:
            selected[row.pid] = (
                row,
                process_role(row, shell.pid, descendant_pids),
            )

    shell_usage = native.read(shell.pid)
    shell_start = native.ticks_to_seconds(shell_usage.proc_start_abstime)
    webkit_candidates: list[tuple[ProcessRow, str, float]] = []
    for row in rows:
        role = webkit_role(row.command)
        if role is None:
            continue
        try:
            usage = native.read(row.pid)
        except ProcessLookupError:
            continue
        start = native.ticks_to_seconds(usage.proc_start_abstime)
        delta = start - shell_start
        if 0 <= delta <= WEBKIT_ASSOCIATION_WINDOW_SECONDS:
            webkit_candidates.append((row, role, start))

    webcontent = [item for item in webkit_candidates if item[1] == "webkit-webcontent"]
    cluster_start: float | None = None
    if webcontent:
        chosen_content = min(webcontent, key=lambda item: item[2])
        cluster_start = chosen_content[2]
        for row, role, start in webkit_candidates:
            if abs(start - cluster_start) <= WEBKIT_CLUSTER_WINDOW_SECONDS:
                selected[row.pid] = (row, role)

    association = {
        "shell_rule": "command contains an Open DJ app-bundle marker",
        "descendant_rule": "recursive PPID ancestry from desktop shell",
        "webkit_rule": (
            "first WebContent launched 0..45s after shell; include WebKit helpers "
            "within 3s of that WebContent"
        ),
        "webkit_cluster_found": cluster_start is not None,
        "included_pids": sorted(selected),
    }
    return [selected[pid] for pid in sorted(selected)], association


def _round_mb(value: int) -> float:
    return round(value / MIB, 1)


def _cpu_percent(
    pid: int,
    cpu_ns: int,
    monotonic_now: float,
    prior: dict[int, tuple[int, float]],
) -> float | None:
    previous = prior.get(pid)
    prior[pid] = (cpu_ns, monotonic_now)
    if previous is None:
        return None
    delta_seconds = monotonic_now - previous[1]
    if delta_seconds <= 0:
        return None
    return round(max(0, cpu_ns - previous[0]) / 1_000_000_000 / delta_seconds * 100, 2)


def process_metric_record(
    row: ProcessRow,
    role: str,
    usage: RUsageInfoV4,
    monotonic_now: float,
    prior_cpu: dict[int, tuple[int, float]],
    prior_io: dict[int, tuple[int, int]],
) -> dict[str, Any]:
    cpu_ns = usage.user_time + usage.system_time
    previous_io = prior_io.get(row.pid)
    prior_io[row.pid] = (usage.diskio_bytesread, usage.diskio_byteswritten)
    read_delta = None if previous_io is None else max(0, usage.diskio_bytesread - previous_io[0])
    write_delta = (
        None
        if previous_io is None
        else max(0, usage.diskio_byteswritten - previous_io[1])
    )
    return {
        "pid": row.pid,
        "ppid": row.ppid,
        "pgid": row.pgid,
        "role": role,
        "command": row.command,
        "physical_footprint_mb": _round_mb(usage.phys_footprint),
        "peak_physical_footprint_mb": _round_mb(usage.lifetime_max_phys_footprint),
        "resident_mb": _round_mb(usage.resident_size),
        "wired_mb": _round_mb(usage.wired_size),
        "cpu_percent": _cpu_percent(row.pid, cpu_ns, monotonic_now, prior_cpu),
        "cpu_seconds_total": round(cpu_ns / 1_000_000_000, 3),
        "disk_read_mb_total": _round_mb(usage.diskio_bytesread),
        "disk_write_mb_total": _round_mb(usage.diskio_byteswritten),
        "disk_read_mb_delta": None if read_delta is None else _round_mb(read_delta),
        "disk_write_mb_delta": None if write_delta is None else _round_mb(write_delta),
        "pageins": usage.pageins,
    }


def _parse_byte_count(raw: str) -> int | None:
    match = re.fullmatch(r"([0-9.]+)([KMGTP])?", raw.strip(), re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").upper()
    scale = {"": 1, "K": 1024, "M": MIB, "G": 1024 * MIB, "T": 1024**4, "P": 1024**5}
    return int(value * scale[unit])


def vmmap_summary(pid: int) -> dict[str, Any]:
    try:
        output = _run_text(["vmmap", "-summary", str(pid)], timeout=30.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}

    result: dict[str, Any] = {"available": True, "pid": pid}
    for label, key in (
        ("Physical footprint:", "physical_footprint_mb"),
        ("Physical footprint (peak):", "peak_physical_footprint_mb"),
    ):
        match = re.search(rf"^{re.escape(label)}\s+([0-9.]+[KMGTP]?)", output, re.MULTILINE)
        if match:
            value = _parse_byte_count(match.group(1))
            if value is not None:
                result[key] = _round_mb(value)

    region_prefixes = {
        "JS JIT generated code": "js_jit",
        "JS VM Gigacage": "js_vm_gigacage",
        "WebKit Malloc": "webkit_malloc",
        "owned unmapped (graphics)": "owned_unmapped_graphics",
        "VM_ALLOCATE (graphics)": "vm_allocate_graphics",
        "IOAccelerator (graphics)": "ioaccelerator_graphics",
        "IOSurface": "iosurface",
    }
    regions: dict[str, dict[str, float]] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if "(reserved)" in stripped:
            continue
        for prefix, key in region_prefixes.items():
            if not stripped.startswith(prefix):
                continue
            rest = stripped[len(prefix) :].strip().split()
            if len(rest) < 4:
                continue
            values = [_parse_byte_count(item) for item in rest[:4]]
            if all(value is not None for value in values):
                regions[key] = {
                    "virtual_mb": _round_mb(values[0] or 0),
                    "resident_mb": _round_mb(values[1] or 0),
                    "dirty_mb": _round_mb(values[2] or 0),
                    "swapped_mb": _round_mb(values[3] or 0),
                }
            break
    result["regions"] = regions
    return result


def _sysctl(name: str) -> str | None:
    try:
        return _run_text(["sysctl", "-n", name], timeout=2.0).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def machine_metrics() -> dict[str, Any]:
    result: dict[str, Any] = {}
    memsize = _sysctl("hw.memsize")
    pressure = _sysctl("kern.memorystatus_vm_pressure_level")
    swap = _sysctl("vm.swapusage")
    if memsize and memsize.isdigit():
        result["physical_memory_mb"] = _round_mb(int(memsize))
    if pressure and pressure.isdigit():
        result["kernel_memory_pressure_level"] = int(pressure)
    if swap:
        result["swap_raw"] = swap
        for source, key in (
            ("total", "swap_total_mb"),
            ("used", "swap_used_mb"),
            ("free", "swap_free_mb"),
        ):
            match = re.search(rf"{source}\s*=\s*([0-9.]+)([KMGTP])", swap, re.IGNORECASE)
            if match:
                value = _parse_byte_count(match.group(1) + match.group(2))
                if value is not None:
                    result[key] = _round_mb(value)
    return result


def _fetch_json(url: str, timeout: float = 1.5) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def engine_metrics(family: list[tuple[ProcessRow, str]]) -> dict[str, Any]:
    engine = next((row for row, role in family if role == "python-engine"), None)
    if engine is None:
        return {"available": False, "reason": "python engine not found"}
    port_match = re.search(r"(?:^|\s)--port\s+(\d+)(?:\s|$)", engine.command)
    if port_match is None:
        return {"available": False, "reason": "engine port not found", "pid": engine.pid}
    port = int(port_match.group(1))
    base = f"http://127.0.0.1:{port}/api/v1"
    result: dict[str, Any] = {"available": True, "pid": engine.pid, "port": port}
    for name, path in (("health", "/health"), ("jobs", "/jobs"), ("clients", "/telemetry/clients")):
        try:
            value = _fetch_json(base + path)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            result[name] = {"error": type(exc).__name__}
            continue
        if name == "jobs" and isinstance(value, list):
            statuses = Counter(
                str(item.get("status", "unknown"))
                for item in value
                if isinstance(item, dict)
            )
            result[name] = {
                "count": len(value),
                "by_status": dict(statuses),
                "active": [
                    {
                        "id": item.get("id"),
                        "kind": item.get("kind"),
                        "status": item.get("status"),
                        "worker_pid": item.get("worker_pid"),
                    }
                    for item in value
                    if isinstance(item, dict)
                    and item.get("status")
                    not in {"succeeded", "failed", "cancelled"}
                ],
            }
        else:
            result[name] = value
    return result


def _decode_local_storage_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return str(value)
    if len(value) >= 2 and value[1] == 0:
        return value.decode("utf-16-le")
    return value.decode("utf-8")


def _local_storage_candidates(bundle_ids: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    for bundle_id in bundle_ids:
        root = Path.home() / "Library/WebKit" / bundle_id / "WebsiteData/Default"
        try:
            found.extend(root.glob("*/*/LocalStorage/localstorage.sqlite3"))
        except OSError:
            continue
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def browser_perf_ring(bundle_ids: Iterable[str]) -> dict[str, Any]:
    candidates = _local_storage_candidates(bundle_ids)
    best: tuple[str, Path, list[Any]] | None = None
    for path in candidates:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25)
            try:
                row = connection.execute(
                    "SELECT value FROM ItemTable WHERE key = ?",
                    ("mdt.perfEventLog",),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                continue
            events = json.loads(_decode_local_storage_value(row[0]))
            if not isinstance(events, list) or not events:
                continue
            last = events[-1]
            last_timestamp = str(last.get("t", "")) if isinstance(last, dict) else ""
            if best is None or last_timestamp > best[0]:
                best = (last_timestamp, path, events)
        except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError):
            continue
    if best is None:
        return {"available": False, "reason": "perf ring not found"}
    _, path, events = best
    last = events[-1] if isinstance(events[-1], dict) else {}
    loads = [
        event
        for event in events
        if isinstance(event, dict)
        and str(event.get("kind", "")).startswith("deck-load")
    ]
    return {
        "available": True,
        "storage_path": str(path),
        "event_count": len(events),
        "first_timestamp": events[0].get("t") if isinstance(events[0], dict) else None,
        "last_timestamp": last.get("t"),
        "last_kind": last.get("kind"),
        "deck_load_count_in_ring": len(loads),
        "last_deck_load": loads[-1] if loads else None,
    }


def utc_now() -> str:
    # datetime.UTC is 3.11; this file runs on /usr/bin/python3 (3.9).
    now = datetime.now(timezone.utc)  # noqa: UP017
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_bounded_jsonl(
    output_dir: Path, record: dict[str, Any], cap_bytes: int
) -> tuple[Path, bool]:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Deliberately LOCAL time: the file name is the operator's calendar day,
    # so a log is found where a human looks for it. Every record inside
    # carries a UTC timestamp, which is what any analysis reads.
    stamp = datetime.now().strftime("%Y-%m-%d")  # noqa: DTZ005
    path = output_dir / f"opendj-performance-{stamp}.jsonl"
    payload = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        current_size = 0
    if current_size + len(payload) > cap_bytes:
        return path, False
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return path, True


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def _series_summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "first": round(values[0], 2),
        "last": round(values[-1], 2),
        "delta": round(values[-1] - values[0], 2),
        "min": round(min(values), 2),
        "median": round(statistics.median(values), 2),
        "p95": _percentile(values, 0.95) or 0.0,
        "max": round(max(values), 2),
    }


def _load_timed_records(
    output_dir: Path, cutoff: float
) -> tuple[list[tuple[datetime, dict[str, Any]]], int]:
    """Read every in-window record, counting the lines that would not parse."""

    rows: list[tuple[datetime, dict[str, Any]]] = []
    malformed_lines = 0
    for path in sorted(output_dir.glob("opendj-performance-*.jsonl")):
        try:
            handle = path.open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (UnicodeError, ValueError):
                    malformed_lines += 1
                    continue
                if not isinstance(record, dict):
                    continue
                timestamp = _record_timestamp(record)
                if timestamp is None or timestamp.timestamp() < cutoff:
                    continue
                rows.append((timestamp, record))
    rows.sort(key=lambda item: item[0])
    return rows, malformed_lines


@dataclass
class _SampleAggregate:
    """Everything the trend summary accumulates over the sample window."""

    footprints: list[float]
    cpu_values: list[float]
    elapsed_seconds: list[float]
    role_values: dict[str, list[float]]
    process_families: set[tuple[tuple[str, int], ...]]
    max_active_jobs: int
    max_suspected_orphans: int
    deep_samples: int
    latest_deep: dict[str, Any] | None
    builds: dict[str, dict[str, Any]]


def _role_footprints(record: dict[str, Any]) -> tuple[list[tuple[str, int]], dict[str, float]]:
    """Split one sample's process list into its family shape and per-role MB."""

    family: list[tuple[str, int]] = []
    per_role: dict[str, float] = {}
    processes = record.get("processes")
    if not isinstance(processes, list):
        return family, per_role
    for process in processes:
        if not isinstance(process, dict):
            continue
        role = process.get("role")
        if not isinstance(role, str):
            continue
        pid = process.get("pid")
        if isinstance(pid, int):
            family.append((role, pid))
        value = process.get("physical_footprint_mb")
        if isinstance(value, (int, float)):
            per_role[role] = per_role.get(role, 0.0) + float(value)
    return family, per_role


def _active_engine_job_count(record: dict[str, Any]) -> int:
    engine = record.get("engine")
    jobs = engine.get("jobs") if isinstance(engine, dict) else None
    active = jobs.get("active") if isinstance(jobs, dict) else None
    return len(active) if isinstance(active, list) else 0


def _aggregate_samples(
    samples: list[tuple[datetime, dict[str, Any]]],
) -> _SampleAggregate:
    aggregate = _SampleAggregate(
        footprints=[],
        cpu_values=[],
        elapsed_seconds=[],
        role_values={},
        process_families=set(),
        max_active_jobs=0,
        max_suspected_orphans=0,
        deep_samples=0,
        latest_deep=None,
        builds={},
    )
    first_time = samples[0][0]
    for timestamp, record in samples:
        totals = record.get("totals")
        if not isinstance(totals, dict):
            continue
        footprint = totals.get("physical_footprint_mb")
        if isinstance(footprint, (int, float)):
            aggregate.footprints.append(float(footprint))
            aggregate.elapsed_seconds.append((timestamp - first_time).total_seconds())
        cpu = totals.get("cpu_percent")
        if isinstance(cpu, (int, float)):
            aggregate.cpu_values.append(float(cpu))
        family, per_role = _role_footprints(record)
        aggregate.process_families.add(tuple(sorted(family)))
        for role, value in per_role.items():
            aggregate.role_values.setdefault(role, []).append(value)
        aggregate.max_active_jobs = max(
            aggregate.max_active_jobs, _active_engine_job_count(record)
        )
        orphan_count = record.get("suspected_orphan_count")
        if isinstance(orphan_count, int):
            aggregate.max_suspected_orphans = max(
                aggregate.max_suspected_orphans, orphan_count
            )
        deep = record.get("deep_vmmap")
        if isinstance(deep, dict):
            aggregate.deep_samples += 1
            aggregate.latest_deep = deep
        build = record.get("build")
        if isinstance(build, dict) and build.get("available") is True:
            key = str(build.get("git_sha_full") or build.get("built_at_utc") or build)
            aggregate.builds[key] = build
    return aggregate


def _linear_slope_mb_per_hour(
    elapsed_seconds: list[float], footprints: list[float]
) -> float | None:
    """Least-squares growth rate: the leak signal the point-in-time MB cannot show."""

    if len(footprints) < 2 or len(elapsed_seconds) != len(footprints):
        return None
    x_mean = statistics.mean(elapsed_seconds)
    y_mean = statistics.mean(footprints)
    denominator = sum((value - x_mean) ** 2 for value in elapsed_seconds)
    if denominator <= 0:
        return None
    # zip(strict=) is 3.10+; this file runs on /usr/bin/python3. The length
    # equality is asserted by the guard above instead.
    numerator = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(elapsed_seconds, footprints)  # noqa: B905
    )
    return round(numerator / denominator * 3600, 2)


def _swap_delta(samples: list[tuple[datetime, dict[str, Any]]]) -> dict[str, float | None]:
    """Machine-wide swap, so app growth is never confused with host saturation."""

    def _used(record: dict[str, Any]) -> float | None:
        machine = record.get("machine")
        value = machine.get("swap_used_mb") if isinstance(machine, dict) else None
        return float(value) if isinstance(value, (int, float)) else None

    first = _used(samples[0][1])
    last = _used(samples[-1][1])
    return {
        "first": first,
        "last": last,
        "delta": None if first is None or last is None else round(last - first, 2),
    }


def summarize_logs(output_dir: Path, hours: float) -> dict[str, Any]:
    """Summarize bounded samples into trend evidence without exposing commands."""

    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600  # noqa: UP017
    rows, malformed_lines = _load_timed_records(output_dir, cutoff)
    samples = [
        (timestamp, record) for timestamp, record in rows if record.get("kind") == "sample"
    ]
    if not samples:
        return {
            "schema_version": 1,
            "available": False,
            "hours_requested": hours,
            "reason": "no process samples in requested window",
            "malformed_lines": malformed_lines,
        }

    aggregate = _aggregate_samples(samples)
    cpu_values = aggregate.cpu_values
    return {
        "schema_version": 1,
        "available": True,
        "hours_requested": hours,
        "window": {
            "first": samples[0][0].isoformat().replace("+00:00", "Z"),
            "last": samples[-1][0].isoformat().replace("+00:00", "Z"),
            "duration_minutes": round(
                (samples[-1][0] - samples[0][0]).total_seconds() / 60, 2
            ),
            "sample_count": len(samples),
            "non_sample_record_count": len(rows) - len(samples),
            "malformed_lines": malformed_lines,
        },
        "physical_footprint_mb": _series_summary(aggregate.footprints),
        "linear_slope_mb_per_hour": _linear_slope_mb_per_hour(
            aggregate.elapsed_seconds, aggregate.footprints
        ),
        "cpu_percent": {
            "mean": round(statistics.mean(cpu_values), 2) if cpu_values else None,
            "p95": _percentile(cpu_values, 0.95),
            "max": round(max(cpu_values), 2) if cpu_values else None,
        },
        "by_role_mb": {
            role: _series_summary(values)
            for role, values in sorted(aggregate.role_values.items())
        },
        "unique_process_families": len(aggregate.process_families),
        "max_active_engine_jobs": aggregate.max_active_jobs,
        "max_suspected_orphans": aggregate.max_suspected_orphans,
        "swap_used_mb": _swap_delta(samples),
        "deep_vmmap_sample_count": aggregate.deep_samples,
        "latest_deep_vmmap": aggregate.latest_deep,
        "builds": list(aggregate.builds.values()),
    }


class OpenDJProbe:
    def __init__(self, shell_pid: int | None, bundle_ids: tuple[str, ...]) -> None:
        self.requested_shell_pid = shell_pid
        self.bundle_ids = bundle_ids
        self.native = DarwinProcessMetrics()
        self.prior_cpu: dict[int, tuple[int, float]] = {}
        self.prior_io: dict[int, tuple[int, int]] = {}
        self.known_descendants: dict[int, tuple[str, str]] = {}
        self.build_by_shell_pid: dict[int, dict[str, Any]] = {}

    def sample(self, *, deep: bool) -> dict[str, Any]:
        monotonic_now = time.monotonic()
        rows = process_table()
        shell = find_shell(rows, self.requested_shell_pid)
        if shell is None:
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
        family, association = associate_process_family(rows, shell, self.native)
        build = self.build_by_shell_pid.setdefault(shell.pid, bundle_build_identity(shell))
        processes: list[dict[str, Any]] = []
        for row, role in family:
            try:
                usage = self.native.read(row.pid)
            except ProcessLookupError:
                continue
            processes.append(
                process_metric_record(
                    row,
                    role,
                    usage,
                    monotonic_now,
                    self.prior_cpu,
                    self.prior_io,
                )
            )
        live_pids = {process["pid"] for process in processes}
        orphans = suspected_orphans(rows, live_pids, self.known_descendants)
        self.known_descendants.update(
            {
                row.pid: (row.command, role)
                for row, role in family
                if role not in WEBKIT_AND_SHELL_ROLES
            }
        )
        self.known_descendants = {
            pid: value
            for pid, value in self.known_descendants.items()
            if pid in {row.pid for row in rows}
        }
        self.prior_cpu = {pid: value for pid, value in self.prior_cpu.items() if pid in live_pids}
        self.prior_io = {pid: value for pid, value in self.prior_io.items() if pid in live_pids}

        total_footprint = sum(float(process["physical_footprint_mb"]) for process in processes)
        total_peak = sum(float(process["peak_physical_footprint_mb"]) for process in processes)
        total_cpu = sum(float(process["cpu_percent"] or 0) for process in processes)
        record: dict[str, Any] = {
            "schema_version": 1,
            "kind": "sample",
            "timestamp": utc_now(),
            "monotonic_seconds": round(monotonic_now, 3),
            "shell_pid": shell.pid,
            "association": association,
            "build": build,
            "totals": {
                "physical_footprint_mb": round(total_footprint, 1),
                "summed_lifetime_peak_mb": round(total_peak, 1),
                "cpu_percent": round(total_cpu, 2),
                "process_count": len(processes),
            },
            "processes": processes,
            "machine": machine_metrics(),
            "engine": engine_metrics(family),
            "browser_perf_ring": browser_perf_ring(self.bundle_ids),
            "suspected_orphan_count": len(orphans),
            "suspected_orphans": orphans,
        }
        if deep:
            webcontent = next(
                (process for process in processes if process["role"] == "webkit-webcontent"),
                None,
            )
            record["deep_vmmap"] = (
                vmmap_summary(int(webcontent["pid"]))
                if webcontent is not None
                else {"available": False, "reason": "WebContent not found"}
            )
        return record


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
