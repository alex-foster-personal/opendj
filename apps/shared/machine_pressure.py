"""The machine's pressure, sampled once and shared, for stamping onto timings.

WHY THIS EXISTS. Deck-load timing rows in the browser's perf ring record what
happened and never the conditions it happened under, and
``docs/perf/performance-register.md`` is a list of numbers that cost the
program because of it: a waveform decode measured 0.73 s and 7.53 s in one
evening on nothing but the machine's load average, a beatgrid lane whose whole
runtime budget is documented as untested because it was taken at load average
554 with roughly 71 MB free. The browser cannot see any of that. This is the
one read-only door it gets.

WHAT IT REUSES. ``scripts/diagnostics/probe_native_metrics.machine_metrics``,
which is already the project's machine sampler: the launchd probe writes it
into its JSONL every 15 seconds, and it is where load average and free page
count now live too. Writing a second sampler here would guarantee the two
drift and would make the endpoint's numbers incomparable with the probe log's.

WHY IT IS CACHED. ``machine_metrics`` shells out to ``sysctl`` three times and
``vm_stat`` once. That is cheap next to walking the whole process table, which
this deliberately does NOT do on the pressure path, but it is not free, and the
caller is a browser polling on a timer while audio is playing. One sample is
shared for a self-throttled TTL and every response states how old the sample
it is handing back actually is.

WHY IT CAN REPORT NOTHING. The diagnostics package is stdlib-only and
standalone by design so the launchd probe can run on a machine with no
checkout, and the desktop payload stages ``apps`` only (see APP_SOURCE_ROOTS
in scripts/build_engine_payload.py), so ``scripts`` is absent inside a
packaged Open DJ.app. There, this reports ``available: false`` with the reason.
It never reports zeros. An unmeasured condition that renders as a real reading
is precisely the defect .claude/rules/verification.md exists to forbid.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared.machine_pressure_signal import (
    pressure_is_elevated,
)

# Locked signals (specs/perf-latency-program.md Pressure cluster, PERFMODE-05).
PRESSURE_CHURN_WEIGHT_SWAP = 10
PRESSURE_CHURN_WINDOW_S = 1
PRESSURE_SAMPLE_IDLE_MS = 1000
PRESSURE_SAMPLE_ELEVATED_MS = 5000
PRESSURE_SAMPLE_P95_WALL_MS = 5

# Back-compat alias for tests that pin the old constant name.
CACHE_TTL_SECONDS = PRESSURE_SAMPLE_IDLE_MS / 1000.0

_OPENDJ_NAME_RE = re.compile(r"opendj-[a-z0-9.-]+")

# The three numbers a deck-load row stamps, mapped from the sampler's keys.
_EXPOSED_FIELDS = (
    ("load_avg_1m", "load_average_1m"),
    ("mem_free_mb", "free_memory_mb"),
    ("swap_used_mb", "swap_used_mb"),
)

def opendj_process_name(command: str) -> str:
    """Keep in sync with ``probe_process_family.opendj_process_name``."""

    if "/MacOS/opendj-desktop" in command:
        return "opendj-desktop"
    match = _OPENDJ_NAME_RE.search(command)
    if match is not None:
        return match.group(0)
    return "unnamed"


def valid_kernel_pressure_level(level: object) -> int | None:
    return level if isinstance(level, int) and level in (1, 2, 4) else None


def sample_wall_p95_ms(walls: Sequence[float]) -> float | None:
    """Nearest-rank p95 of recorded sample walls. Empty is absent, never 0."""

    if not walls:
        return None
    ordered = sorted(walls)
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return round(ordered[rank], 3)


def pressure_band_for_level(level: int) -> str:
    return {1: "fine", 2: "warning", 4: "critical"}[level]


def _next_ttl_seconds(values: dict[str, Any]) -> float:
    if pressure_is_elevated(values):
        return PRESSURE_SAMPLE_ELEVATED_MS / 1000.0
    return PRESSURE_SAMPLE_IDLE_MS / 1000.0


def _vm_stat_page_count(output: str, label: str) -> int | None:
    match = re.search(rf"^{label}:\s+(\d+)\.", output, re.MULTILINE)
    if match is None:
        return None
    return int(match.group(1))


def _read_vm_stat_churn_snapshot() -> dict[str, int] | None:
    try:
        proc = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=2.0, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = proc.stdout
    header = re.search(r"page size of (\d+) bytes", output)
    if header is None:
        return None
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
        "page_size": int(header.group(1)),
    }
    if compressor_pages is not None:
        snapshot["compressor_pages"] = compressor_pages
    return snapshot


def _churn_overlay(
    prior: dict[str, int] | None,
    prior_at: float | None,
    now: float,
    current: dict[str, int],
) -> dict[str, float]:
    if prior is None or prior_at is None:
        return {}
    elapsed_s = now - prior_at
    if elapsed_s <= 0:
        return {}
    swap_delta = (current["swapins"] - prior["swapins"]) + (
        current["swapouts"] - prior["swapouts"]
    )
    decomp_delta = current["decompressions"] - prior["decompressions"]
    swap_rate = swap_delta / elapsed_s
    decomp_rate = decomp_delta / elapsed_s
    overlay: dict[str, float] = {
        "swap_rate": round(swap_rate, 3),
        "decomp_rate": round(decomp_rate, 3),
        "churn_score": round(
            swap_rate * PRESSURE_CHURN_WEIGHT_SWAP + decomp_rate, 3
        ),
    }
    current_pages = current.get("compressor_pages")
    page_size = current.get("page_size")
    if isinstance(current_pages, int) and isinstance(page_size, int):
        overlay["compressed_mb"] = round(current_pages * page_size / 1_048_576, 1)
    return overlay


@dataclass(frozen=True)
class PressureSample:
    """One reading and the monotonic instant it was taken at."""

    taken_at: float
    values: dict[str, float | int | str]
    unavailable_reason: str | None
    next_ttl_seconds: float


def _sample_machine_metrics() -> dict[str, Any]:
    from scripts.diagnostics.probe_native_metrics import machine_metrics

    return machine_metrics()


def _overlay_from_raw(
    raw: dict[str, Any],
    *,
    prior_vm_stat: dict[str, int] | None,
    prior_vm_stat_at: float | None,
    now: float,
    current_vm_stat: dict[str, int] | None,
) -> dict[str, float | int | str]:
    overlay: dict[str, float | int | str] = {}
    kernel = valid_kernel_pressure_level(raw.get("kernel_memory_pressure_level"))
    if kernel is not None:
        overlay["kernel_memory_pressure_level"] = kernel
        overlay["band"] = pressure_band_for_level(kernel)
    if current_vm_stat is not None:
        overlay.update(
            _churn_overlay(prior_vm_stat, prior_vm_stat_at, now, current_vm_stat)
        )
    return overlay


def _build_sample(
    now: float,
    sampler: Callable[[], dict[str, Any]] = _sample_machine_metrics,
    *,
    prior_vm_stat: dict[str, int] | None = None,
    prior_vm_stat_at: float | None = None,
    vm_stat_reader: Callable[[], dict[str, int] | None] = _read_vm_stat_churn_snapshot,
) -> tuple[PressureSample, dict[str, int] | None]:
    started = time.monotonic()
    try:
        raw = sampler()
    except ImportError as exc:
        reason = f"native machine sampler is not importable: {exc}"
        return PressureSample(now, {}, reason, CACHE_TTL_SECONDS), prior_vm_stat
    except OSError as exc:
        reason = f"native machine sampler failed: {exc}"
        return PressureSample(now, {}, reason, CACHE_TTL_SECONDS), prior_vm_stat
    current_vm_stat = vm_stat_reader()
    values: dict[str, float | int | str] = {
        exposed: float(raw[source])
        for exposed, source in _EXPOSED_FIELDS
        if isinstance(raw.get(source), (int, float))
    }
    values.update(
        _overlay_from_raw(
            raw,
            prior_vm_stat=prior_vm_stat,
            prior_vm_stat_at=prior_vm_stat_at,
            now=now,
            current_vm_stat=current_vm_stat,
        )
    )
    values["sample_wall_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    next_ttl = _next_ttl_seconds(values)
    values["sample_interval_ms"] = int(next_ttl * 1000)
    if not values.keys() & {key for key, _ in _EXPOSED_FIELDS}:
        return (
            PressureSample(
                now,
                {},
                "native machine sampler returned no readable field",
                CACHE_TTL_SECONDS,
            ),
            current_vm_stat if current_vm_stat is not None else prior_vm_stat,
        )
    return PressureSample(now, values, None, next_ttl), current_vm_stat


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    command: str
    rss_bytes: int | None


def _read_process_rows(
    process_iter: Callable[[], list[ProcessInfo]] | None,
) -> list[ProcessInfo]:
    import psutil

    try:
        return (process_iter or _default_process_iter)()
    except (ImportError, OSError, psutil.Error):
        return []


def _default_process_iter() -> list[ProcessInfo]:
    import psutil

    rows: list[ProcessInfo] = []
    for proc in psutil.process_iter(["pid", "ppid", "cmdline", "memory_info"]):
        try:
            info = proc.info
        except (psutil.Error, OSError):
            continue
        pid = info.get("pid")
        ppid = info.get("ppid")
        if not isinstance(pid, int) or not isinstance(ppid, int):
            continue
        cmdline = info.get("cmdline") or []
        command = " ".join(str(part) for part in cmdline) if cmdline else proc.name()
        memory_info = info.get("memory_info")
        rss_bytes = memory_info.rss if memory_info is not None else None
        rows.append(ProcessInfo(pid=pid, ppid=ppid, command=command, rss_bytes=rss_bytes))
    return rows


def _descendant_pids(rows: Iterable[ProcessInfo], root_pid: int) -> set[int]:
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


def _collect_live_process_family(
    rows: list[ProcessInfo],
    *,
    engine_pid: int,
) -> tuple[list[dict[str, Any]], set[int]]:
    by_pid = {row.pid: row for row in rows}
    seeds: set[int] = {engine_pid}
    for row in rows:
        if opendj_process_name(row.command) != "unnamed":
            seeds.add(row.pid)
    family_pids: set[int] = set(seeds)
    for seed in sorted(seeds):
        family_pids.update(_descendant_pids(rows, seed))
    members: list[dict[str, Any]] = []
    for pid in sorted(family_pids):
        row = by_pid.get(pid)
        if row is None:
            continue
        member: dict[str, Any] = {"name": opendj_process_name(row.command)}
        if isinstance(row.rss_bytes, int) and row.rss_bytes >= 0:
            member["rss_mb"] = round(row.rss_bytes / 1_048_576, 1)
        members.append(member)
    return members, family_pids


def live_process_family_members(
    *,
    engine_pid: int | None = None,
    process_iter: Callable[[], list[ProcessInfo]] | None = None,
) -> list[dict[str, Any]]:
    """Walk the live opendj-* family via psutil. Members omit command and pid."""

    rows = _read_process_rows(process_iter)
    if not rows:
        return []
    root_pid = engine_pid if engine_pid is not None else os.getpid()
    members, _family_pids = _collect_live_process_family(rows, engine_pid=root_pid)
    return members


def live_process_family_state(
    *,
    engine_pid: int | None = None,
    process_iter: Callable[[], list[ProcessInfo]] | None = None,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Like ``live_process_family_members`` but also returns family PIDs for merge."""

    rows = _read_process_rows(process_iter)
    if not rows:
        return [], set()
    root_pid = engine_pid if engine_pid is not None else os.getpid()
    return _collect_live_process_family(rows, engine_pid=root_pid)


class MachinePressureCache:
    """A single shared sample behind a self-throttled TTL and a lock."""

    def __init__(self, ttl_seconds: float = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._sample: PressureSample | None = None
        self._prior_vm_stat: dict[str, int] | None = None
        self._prior_vm_stat_at: float | None = None
        self._wall_ms: deque[float] = deque(maxlen=20)

    def read(
        self,
        *,
        now: float | None = None,
        sampler: Callable[[], dict[str, Any]] = _sample_machine_metrics,
        vm_stat_reader: Callable[[], dict[str, int] | None] = _read_vm_stat_churn_snapshot,
    ) -> dict[str, Any]:
        instant = time.monotonic() if now is None else now
        with self._lock:
            cached = self._sample
            ttl = cached.next_ttl_seconds if cached is not None else self._ttl
            if cached is None or instant - cached.taken_at >= ttl:
                cached, vm_snapshot = _build_sample(
                    instant,
                    sampler,
                    prior_vm_stat=self._prior_vm_stat,
                    prior_vm_stat_at=self._prior_vm_stat_at,
                    vm_stat_reader=vm_stat_reader,
                )
                self._sample = cached
                wall = cached.values.get("sample_wall_ms")
                if isinstance(wall, (int, float)):
                    self._wall_ms.append(float(wall))
                if vm_snapshot is not None:
                    self._prior_vm_stat = vm_snapshot
                    self._prior_vm_stat_at = instant
        age_ms = round(max(0.0, instant - cached.taken_at) * 1000.0, 1)
        if cached.unavailable_reason is not None:
            return {
                "available": False,
                "reason": cached.unavailable_reason,
                "cache_age_ms": age_ms,
            }
        body: dict[str, Any] = {"available": True, "cache_age_ms": age_ms, **cached.values}
        p95 = sample_wall_p95_ms(self._wall_ms)
        if p95 is not None:
            body["sample_wall_p95_ms"] = p95
        return body


_CACHE = MachinePressureCache()


def read_machine_pressure() -> dict[str, Any]:
    """The process-wide cached reading. THE entry point for the route."""

    return _CACHE.read()
