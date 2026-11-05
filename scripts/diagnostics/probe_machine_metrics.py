"""Darwin machine counters for the installed OpenDJ diagnostics probe.

This is the install-copy sampler: stdlib-only, no ``apps`` imports, launched
under ``/usr/bin/python3`` (3.9 on current macOS). The in-repo engine pressure
sampler stays :mod:`apps.shared.machine_metrics`. Two copies are the accepted
cost of the install boundary plus ``apps-never-imports-scripts``.

``sysctl`` is pinned at ``/usr/sbin/sysctl``. A PATH lookup takes the tool away
from a launchd job whose PATH has no ``/usr/sbin``.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from .probe_types import parse_byte_count, round_mb, run_text

SYSCTL = "/usr/sbin/sysctl"


def try_sysctl_n(oid: str, *, timeout: float = 2.0) -> str | None:
    """Return one ``sysctl -n`` value, or ``None`` when the call fails or times out."""
    try:
        completed = subprocess.run(
            [SYSCTL, "-n", oid],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip()


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


def _read_vm_stat() -> tuple[str, int] | None:
    """Run ``vm_stat`` and parse page size, or None when unreadable."""

    try:
        output = run_text(["vm_stat"], timeout=2.0)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    header = re.search(r"page size of (\d+) bytes", output)
    if header is None:
        return None
    return output, int(header.group(1))


def vm_stat_churn_snapshot() -> dict[str, int] | None:
    """Swap/compressor counters from ``vm_stat``, or None when unreadable."""

    read = _read_vm_stat()
    if read is None:
        return None
    output, page_size = read
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
    swap_delta = (current["swapins"] - prior["swapins"]) + (current["swapouts"] - prior["swapouts"])
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
    if isinstance(prior_pages, int) and isinstance(current_pages, int) and isinstance(page_size, int):
        result["compressed_mb"] = round_mb(current_pages * page_size)
    return result


def _vm_stat_free_mb() -> dict[str, float]:
    """Free physical memory, from ``vm_stat``'s page counters.

    FREE, not available: only ``Pages free`` is counted, deliberately. The
    wider "free + inactive + speculative + purgeable" figure that
    ``(private path)`` computes answers a different question (how much
    the kernel could reclaim under pressure) and is far larger, so mixing the
    two under one name would make two samples incomparable. The register's own
    "roughly 71 MB free RAM" reading is this narrow one.

    An unparseable output yields NO key rather than a zero. A machine sample
    that reports 0 MB free would be indistinguishable from a machine about to
    die, which is exactly the reading this must never fabricate.
    """

    read = _read_vm_stat()
    if read is None:
        return {}
    output, page_size = read
    free = re.search(r"^Pages free:\s+(\d+)", output, re.MULTILINE)
    if free is None:
        return {}
    return {"free_memory_mb": round_mb(int(free.group(1)) * page_size)}


def machine_metrics() -> dict[str, Any]:
    result: dict[str, Any] = {}
    memsize = try_sysctl_n("hw.memsize")
    pressure = try_sysctl_n("kern.memorystatus_vm_pressure_level")
    swap = try_sysctl_n("vm.swapusage")
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
