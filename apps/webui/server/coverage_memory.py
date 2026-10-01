"""Memory pressure gate for the drain's analysis step.

One analysis job costs about 2 GB (HEALTH-06). On a laptop that is already
swapping, starting one makes everything else slower, playback included, so
the drain holds analysis back while memory is tight. Vocals and lyrics are a
few hundred megabytes and seconds long, and are not gated.

The signal, in the order it is read:

1. macOS: ``kern.memorystatus_vm_pressure_level``, the kernel's own verdict
   (1 normal, 2 warn, 4 critical). Warn or worse is high pressure. This is
   the signal Activity Monitor's memory graph colors from, and it already
   accounts for compression and swap. Measured on the preview laptop (16 GB,
   7.1 GB of 8.2 GB swap used) on Thu 1 Oct 2026: level 2.
2. Every platform: available memory (``psutil.virtual_memory().available``)
   under :data:`MIN_AVAILABLE_BYTES`, twice one analysis job. The same
   laptop read 3.2 GB available at that moment.

Swap used is NOT the signal: macOS grows and shrinks its swap file, so a
"percent used" has no fixed denominator, and swap stays allocated long after
the pressure that caused it has gone.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 HEALTH-10 analysis waits under memory pressure
    [if] the kernel level is warn or critical [then] a reason is returned
    [if] available memory is under 4 GiB [then] a reason is returned
    [if] the level is normal and 8 GiB is available [then] None
    [if] the kernel level cannot be read [then] available memory decides
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

import psutil

GIB: int = 1024**3
#: Two analysis jobs' worth (about 2 GB each): one to run, one of headroom.
MIN_AVAILABLE_BYTES: int = 4 * GIB
MACOS_PRESSURE_SYSCTL: str = "kern.memorystatus_vm_pressure_level"
MACOS_PRESSURE_WARN: int = 2
MACOS_PRESSURE_NAMES: dict[int, str] = {1: "normal", 2: "warn", 4: "critical"}
SYSCTL_TIMEOUT_S: float = 5.0


def macos_pressure_level() -> int | None:
    """The kernel's memory pressure level, or None off macOS or unreadable."""
    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            ["/usr/sbin/sysctl", "-n", MACOS_PRESSURE_SYSCTL],
            capture_output=True, text=True, timeout=SYSCTL_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip().isdigit():
        return None
    return int(completed.stdout.strip())


def available_bytes() -> int:
    return int(psutil.virtual_memory().available)


def pressure_reason(
    level_fn: Callable[[], int | None] = macos_pressure_level,
    available_fn: Callable[[], int] = available_bytes,
) -> str | None:
    """Why analysis should wait for memory, or None when there is room."""
    level = level_fn()
    if level is not None and level >= MACOS_PRESSURE_WARN:
        name = MACOS_PRESSURE_NAMES.get(level, str(level))
        return f"the kernel reports memory pressure {name} (level {level})"
    available = available_fn()
    if available < MIN_AVAILABLE_BYTES:
        return (
            f"{available / GIB:.1f} GiB of memory is available, under the "
            f"{MIN_AVAILABLE_BYTES / GIB:.0f} GiB analysis needs"
        )
    return None


__all__ = [
    "MACOS_PRESSURE_WARN",
    "MIN_AVAILABLE_BYTES",
    "available_bytes",
    "macos_pressure_level",
    "pressure_reason",
]
