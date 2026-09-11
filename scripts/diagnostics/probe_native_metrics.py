"""Darwin per-process and machine counters, read without a profiler process.

``proc_pid_rusage`` is the same physical-footprint counter Activity Monitor
shows, and it is cheap enough to sample every 15 seconds. ``vmmap`` is not,
which is why the deep categorization lives behind its own interval.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
from typing import Any

from .probe_types import (
    ProbeUnavailable,
    ProcessRow,
    parse_byte_count,
    round_mb,
    run_text,
)


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
        "physical_footprint_mb": round_mb(usage.phys_footprint),
        "peak_physical_footprint_mb": round_mb(usage.lifetime_max_phys_footprint),
        "resident_mb": round_mb(usage.resident_size),
        "wired_mb": round_mb(usage.wired_size),
        "cpu_percent": _cpu_percent(row.pid, cpu_ns, monotonic_now, prior_cpu),
        "cpu_seconds_total": round(cpu_ns / 1_000_000_000, 3),
        "disk_read_mb_total": round_mb(usage.diskio_bytesread),
        "disk_write_mb_total": round_mb(usage.diskio_byteswritten),
        "disk_read_mb_delta": None if read_delta is None else round_mb(read_delta),
        "disk_write_mb_delta": None if write_delta is None else round_mb(write_delta),
        "pageins": usage.pageins,
    }


VMMAP_REGION_PREFIXES = {
    "JS JIT generated code": "js_jit",
    "JS VM Gigacage": "js_vm_gigacage",
    "WebKit Malloc": "webkit_malloc",
    "owned unmapped (graphics)": "owned_unmapped_graphics",
    "VM_ALLOCATE (graphics)": "vm_allocate_graphics",
    "IOAccelerator (graphics)": "ioaccelerator_graphics",
    "IOSurface": "iosurface",
}


def _vmmap_footprints(output: str) -> dict[str, float]:
    """The two headline footprint lines, in MB, when vmmap printed them."""

    found: dict[str, float] = {}
    for label, key in (
        ("Physical footprint:", "physical_footprint_mb"),
        ("Physical footprint (peak):", "peak_physical_footprint_mb"),
    ):
        match = re.search(rf"^{re.escape(label)}\s+([0-9.]+[KMGTP]?)", output, re.MULTILINE)
        if match is None:
            continue
        value = parse_byte_count(match.group(1))
        if value is not None:
            found[key] = round_mb(value)
    return found


def _vmmap_region_row(stripped: str, prefix: str) -> dict[str, float] | None:
    """The virtual/resident/dirty/swapped quad for one named region, or None."""

    rest = stripped[len(prefix) :].strip().split()
    if len(rest) < 4:
        return None
    values = [parse_byte_count(item) for item in rest[:4]]
    if not all(value is not None for value in values):
        return None
    return {
        "virtual_mb": round_mb(values[0] or 0),
        "resident_mb": round_mb(values[1] or 0),
        "dirty_mb": round_mb(values[2] or 0),
        "swapped_mb": round_mb(values[3] or 0),
    }


def _vmmap_regions(output: str) -> dict[str, dict[str, float]]:
    regions: dict[str, dict[str, float]] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if "(reserved)" in stripped:
            continue
        for prefix, key in VMMAP_REGION_PREFIXES.items():
            if not stripped.startswith(prefix):
                continue
            row = _vmmap_region_row(stripped, prefix)
            if row is not None:
                regions[key] = row
            break
    return regions


def vmmap_summary(pid: int) -> dict[str, Any]:
    try:
        output = run_text(["vmmap", "-summary", str(pid)], timeout=30.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    result: dict[str, Any] = {"available": True, "pid": pid}
    result.update(_vmmap_footprints(output))
    result["regions"] = _vmmap_regions(output)
    return result


def _sysctl(name: str) -> str | None:
    try:
        return run_text(["sysctl", "-n", name], timeout=2.0).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _swap_fields(swap: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for source, key in (
        ("total", "swap_total_mb"),
        ("used", "swap_used_mb"),
        ("free", "swap_free_mb"),
    ):
        match = re.search(rf"{source}\s*=\s*([0-9.]+)([KMGTP])", swap, re.IGNORECASE)
        if match is None:
            continue
        value = parse_byte_count(match.group(1) + match.group(2))
        if value is not None:
            found[key] = round_mb(value)
    return found


def _load_average() -> dict[str, float]:
    """The three load averages, from the kernel, with no subprocess at all.

    Load average is the single condition the performance register blames most
    often for a number it cannot trust: the same waveform decode measured
    0.73 s and 7.53 s in one evening with the load average moving between 39
    and 141. It belongs in every machine sample, and ``os.getloadavg`` is a
    plain syscall, so there is no cost argument against taking it.
    """

    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        return {}
    return {
        "load_average_1m": round(one, 2),
        "load_average_5m": round(five, 2),
        "load_average_15m": round(fifteen, 2),
    }


def _vm_stat_page_count(output: str, label: str) -> int | None:
    match = re.search(rf"^{label}:\s+(\d+)\.", output, re.MULTILINE)
    if match is None:
        return None
    return int(match.group(1))


def vm_stat_churn_snapshot() -> dict[str, int] | None:
    """Swap/compressor counters from ``vm_stat``, or None when unreadable."""

    try:
        output = run_text(["vm_stat"], timeout=2.0)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    header = re.search(r"page size of (\d+) bytes", output)
    if header is None:
        return None
    page_size = int(header.group(1))
    swapins = _vm_stat_page_count(output, "Swapins")
    swapouts = _vm_stat_page_count(output, "Swapouts")
    decompressions = _vm_stat_page_count(output, "Decompressions")
    compressor_pages = _vm_stat_page_count(output, "Pages stored in compressor")
    if swapins is None or swapouts is None or decompressions is None:
        return None
    snapshot: dict[str, int] = {
        "swapins": swapins,
        "swapouts": swapouts,
        "decompressions": decompressions,
        "page_size": page_size,
    }
    if compressor_pages is not None:
        snapshot["compressor_pages"] = compressor_pages
    return snapshot


def churn_metrics_from_snapshots(
    prior: dict[str, int] | None,
    current: dict[str, int],
    elapsed_s: float,
    *,
    swap_weight: int = 10,
) -> dict[str, float]:
    """Delta churn between two ``vm_stat_churn_snapshot`` readings."""

    if prior is None or elapsed_s <= 0:
        return {}
    swap_delta = (current["swapins"] - prior["swapins"]) + (
        current["swapouts"] - prior["swapouts"]
    )
    decomp_delta = current["decompressions"] - prior["decompressions"]
    swap_rate = swap_delta / elapsed_s
    decomp_rate = decomp_delta / elapsed_s
    result: dict[str, float] = {
        "swap_rate": round(swap_rate, 3),
        "decomp_rate": round(decomp_rate, 3),
        "churn_score": round(swap_rate * swap_weight + decomp_rate, 3),
    }
    prior_pages = prior.get("compressor_pages")
    current_pages = current.get("compressor_pages")
    page_size = current.get("page_size")
    if (
        isinstance(prior_pages, int)
        and isinstance(current_pages, int)
        and isinstance(page_size, int)
    ):
        result["compressed_mb"] = round_mb(current_pages * page_size)
    return result


def _vm_stat_free_mb() -> dict[str, float]:
    """Free physical memory, from ``vm_stat``'s page counters.

    FREE, not available: only ``Pages free`` is counted, deliberately. The
    wider "free + inactive + speculative + purgeable" figure that
    ``scripts/mem_gate.py`` computes answers a different question (how much
    the kernel could reclaim under pressure) and is far larger, so mixing the
    two under one name would make two samples incomparable. The register's own
    "roughly 71 MB free RAM" reading is this narrow one.

    An unparseable output yields NO key rather than a zero. A machine sample
    that reports 0 MB free would be indistinguishable from a machine about to
    die, which is exactly the reading this must never fabricate.
    """

    try:
        output = run_text(["vm_stat"], timeout=2.0)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    header = re.search(r"page size of (\d+) bytes", output)
    free = re.search(r"^Pages free:\s+(\d+)", output, re.MULTILINE)
    if header is None or free is None:
        return {}
    return {"free_memory_mb": round_mb(int(free.group(1)) * int(header.group(1)))}


def machine_metrics() -> dict[str, Any]:
    result: dict[str, Any] = {}
    memsize = _sysctl("hw.memsize")
    pressure = _sysctl("kern.memorystatus_vm_pressure_level")
    swap = _sysctl("vm.swapusage")
    if memsize and memsize.isdigit():
        result["physical_memory_mb"] = round_mb(int(memsize))
    if pressure and pressure.isdigit():
        result["kernel_memory_pressure_level"] = int(pressure)
    if swap:
        result["swap_raw"] = swap
        result.update(_swap_fields(swap))
    result.update(_load_average())
    result.update(_vm_stat_free_mb())
    return result
