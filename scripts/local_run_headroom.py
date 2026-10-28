"""Refuse a scoped local test run when this Mac has no CPU or RAM headroom.

the maintainer's rule (Tue 27 Oct 2026): scoped local runs are allowed on Silver only while
CPU and RAM headroom exists; full suites never run locally. This helper takes the
two readings, prints them, and exits non-zero when headroom is missing. A reading
it cannot take is UNKNOWN and also refuses (no silent fallback).

Usage:
    python -m scripts.local_run_headroom check
        prints the readings and the verdict
    python -m scripts.local_run_headroom run -- .venv/bin/pytest tests/scripts/test_x.py
        checks, then runs the command only when headroom is OK, and returns the
        command's own exit code

Flags (put them before ``--``): ``--max-load-per-core`` and ``--min-free-memory-pct``.

Exit codes: 0 OK, 1 REFUSED, 2 UNKNOWN, 127 when `run`'s command is not on PATH (checked
before measuring, so a typo is never reported as a headroom refusal). `run` launches the
command by its resolved path (AGENTS.md "Portable by default").
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Literal

# ---- config -----------------------------------------------------------------
MAX_LOAD_PER_CORE: float = 0.70  # 1-minute load average / logical CPU count
MIN_FREE_MEMORY_PCT: float = 20.0  # system free-memory percentage
SYSCTL_TIMEOUT_SECONDS: float = 5.0
SYSCTL_FALLBACK_PATH: str = "/usr/sbin/sysctl"
MEMINFO_PATH: str = "/proc/meminfo"
EXIT_OK: int = 0
EXIT_REFUSED: int = 1
EXIT_UNKNOWN: int = 2
EXIT_COMMAND_NOT_FOUND: int = 127  # the shell's code for a command it cannot find
PREFIX: str = "[headroom]"

Status = Literal["OK", "REFUSED", "UNKNOWN"]


class HeadroomUnknown(Exception):
    """A reading could not be taken; the reason is the exception message."""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    status: Status
    reasons: list[str] = field(default_factory=list)


# ---- measurements -----------------------------------------------------------
def _load_per_core() -> float:
    if not hasattr(os, "getloadavg"):
        raise HeadroomUnknown("load average is not available on Windows")
    cores = os.cpu_count()
    if not cores:
        raise HeadroomUnknown("logical CPU count is not available")
    try:
        load = os.getloadavg()[0]
    except OSError as exc:
        raise HeadroomUnknown(f"load average is not obtainable: {exc}") from exc
    return load / cores


def parse_meminfo_free_pct(text: str) -> float:
    """MemAvailable / MemTotal * 100 from the text of /proc/meminfo."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[name.strip()] = int(parts[0])
    for key in ("MemAvailable", "MemTotal"):
        if key not in fields:
            raise HeadroomUnknown(f"{key} missing from meminfo")
    if fields["MemTotal"] <= 0:
        raise HeadroomUnknown("MemTotal is not positive in meminfo")
    return fields["MemAvailable"] / fields["MemTotal"] * 100.0


def _free_memory_pct_macos() -> float:
    sysctl = shutil.which("sysctl") or SYSCTL_FALLBACK_PATH
    try:
        result = subprocess.run(
            [sysctl, "-n", "kern.memorystatus_level"],
            capture_output=True,
            text=True,
            timeout=SYSCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HeadroomUnknown(f"sysctl kern.memorystatus_level failed: {exc}") from exc
    if result.returncode != 0:
        raise HeadroomUnknown(f"sysctl exited {result.returncode}: {result.stderr.strip()}")
    try:
        level = float(result.stdout.strip())
    except ValueError as exc:
        raise HeadroomUnknown(f"sysctl printed a non-number: {result.stdout.strip()!r}") from exc
    if not 0.0 <= level <= 100.0:
        raise HeadroomUnknown(f"kern.memorystatus_level out of range 0-100: {level}")
    return level


def _free_memory_pct_linux() -> float:
    try:
        with open(MEMINFO_PATH, encoding="ascii") as handle:
            return parse_meminfo_free_pct(handle.read())
    except OSError as exc:
        raise HeadroomUnknown(f"cannot read {MEMINFO_PATH}: {exc}") from exc


def _free_memory_pct() -> float:
    system = platform.system()
    if system == "Darwin":
        return _free_memory_pct_macos()
    if system == "Linux":
        return _free_memory_pct_linux()
    raise HeadroomUnknown(f"free memory is not measurable on platform {system!r}")


# ---- verdict ----------------------------------------------------------------
def verdict(
    load_per_core: float | None,
    free_memory_pct: float | None,
    max_load_per_core: float,
    min_free_memory_pct: float,
) -> Verdict:
    unknown: list[str] = []
    refused: list[str] = []
    if load_per_core is None:
        unknown.append("load per core was not measured")
    elif not load_per_core <= max_load_per_core:  # written so a NaN on either side refuses
        refused.append(f"load per core {load_per_core:.2f} is above the limit {max_load_per_core:.2f}")
    if free_memory_pct is None:
        unknown.append("free memory was not measured")
    elif not free_memory_pct >= min_free_memory_pct:
        refused.append(f"free memory {free_memory_pct:.1f}% is below the minimum {min_free_memory_pct:.1f}%")
    if unknown:
        return Verdict(ok=False, status="UNKNOWN", reasons=unknown + refused)
    if refused:
        return Verdict(ok=False, status="REFUSED", reasons=refused)
    return Verdict(ok=True, status="OK", reasons=[])


# ---- cli --------------------------------------------------------------------
def _measure(name: str, reader) -> tuple[float | None, str | None]:
    try:
        return reader(), None
    except HeadroomUnknown as exc:
        return None, f"{name} is UNKNOWN: {exc}"


def _evaluate(max_load: float, min_free: float) -> Verdict:
    load, load_err = _measure("load per core", _load_per_core)
    free, free_err = _measure("free memory", _free_memory_pct)
    result = verdict(load, free, max_load, min_free)
    load_text = "UNKNOWN" if load is None else f"{load:.2f}"
    free_text = "UNKNOWN" if free is None else f"{free:.1f}%"
    print(f"{PREFIX} load per core: {load_text} (limit {max_load:.2f}, 1-minute load average / logical CPUs)")
    print(f"{PREFIX} free memory: {free_text} (minimum {min_free:.1f}%)")
    for err in (load_err, free_err):
        if err:
            print(f"{PREFIX} {err}")
    for reason in result.reasons:
        print(f"{PREFIX} {reason}")
    print(f"{PREFIX} verdict: {result.status}")
    return result


def _exit_code(result: Verdict) -> int:
    if result.status == "OK":
        return EXIT_OK
    if result.status == "REFUSED":
        return EXIT_REFUSED
    if result.status == "UNKNOWN":
        return EXIT_UNKNOWN
    raise ValueError(f"unhandled status {result.status!r}")


def _max_load_per_core(text: str) -> float:
    """A finite load-per-core limit above 0; `nan` or `inf` would let any load pass."""
    value = float(text)
    if not (math.isfinite(value) and value > 0.0):
        raise argparse.ArgumentTypeError(f"--max-load-per-core {text} must be a finite number above 0")
    return value


def _min_free_memory_pct(text: str) -> float:
    """A finite free-memory minimum from 0 to 100; `nan` would let any memory pass."""
    value = float(text)
    if not (math.isfinite(value) and 0.0 <= value <= 100.0):
        raise argparse.ArgumentTypeError(f"--min-free-memory-pct {text} must be a finite number from 0 to 100")
    return value


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--max-load-per-core", type=_max_load_per_core, default=MAX_LOAD_PER_CORE)
    common.add_argument("--min-free-memory-pct", type=_min_free_memory_pct, default=MIN_FREE_MEMORY_PCT)
    parser = argparse.ArgumentParser(prog="python -m scripts.local_run_headroom", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("check", parents=[common], help="print the readings and the verdict")
    run = sub.add_parser("run", parents=[common], help="check, then run a command only when headroom is OK")
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command: list[str] = []
    if args.mode == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run requires a command after --")
        executable = shutil.which(command[0])
        if executable is None:
            print(f"{PREFIX} command not found on PATH: {command[0]!r}; nothing was measured or run")
            return EXIT_COMMAND_NOT_FOUND
        command = [executable, *command[1:]]
    result = _evaluate(args.max_load_per_core, args.min_free_memory_pct)
    if not result.ok:
        print(f"{PREFIX} not running anything: scoped local runs need headroom (full suites never run locally)")
        return _exit_code(result)
    if args.mode == "run":
        return subprocess.run(command, check=False).returncode
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
