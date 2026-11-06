"""The machine's pressure, sampled once and shared, for stamping onto timings.

WHY THIS EXISTS. Deck-load timing rows in the browser's perf ring record what
happened and never the conditions it happened under, and
``(private path)`` is a list of numbers that cost the
program because of it: a waveform decode measured 0.73 s and 7.53 s in one
evening on nothing but the machine's load average, a beatgrid lane whose whole
runtime budget is documented as untested because it was taken at load average
554 with roughly 71 MB free. The browser cannot see any of that. This is the
one read-only door it gets.

WHAT IT REUSES. ``apps.shared.machine_metrics.machine_metrics``, which is
already the project's machine sampler: the launchd probe writes it into its
JSONL every 15 seconds, and it is where load average and free page count now
live too. Writing a second sampler here would guarantee the two drift and
would make the endpoint's numbers incomparable with the probe log's.

WHY IT IS CACHED. ``machine_metrics`` shells out to ``sysctl`` three times and
``vm_stat`` once. That is cheap next to walking the whole process table, which
this deliberately does NOT do on the pressure path, but it is not free, and the
caller is a browser polling on a timer while audio is playing. One sample is
shared for a self-throttled TTL and every response states how old the sample
it is handing back actually is.

WHY IT CAN REPORT NOTHING. The sampler lives under ``apps.shared``, which the
desktop payload already stages (see APP_SOURCE_ROOTS in
scripts/build_engine_payload.py). ``available: false`` is for ``OSError`` or
unreadable native tools, not for a missing checkout. ``ImportError`` remains
the defensive path if the sampler module cannot load. It never reports zeros.
An unmeasured condition that renders as a real reading is precisely the
defect (private path) exists to forbid.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared import process_family_rule
from apps.shared.machine_pressure_signal import (
    pressure_is_elevated,
)
from apps.shared.process_family_rule import (
    ELECTRON_GPU_ROLE,
    ELECTRON_RENDERER_ROLE,
    ELECTRON_UTILITY_ROLE,
    SHELL_MARKER,
    WEBKIT_ASSOCIATION_WINDOW_SECONDS,
    WEBKIT_ROLES,
    associate_family,
    opendj_process_name,
    process_role,
    shell_candidates,
)

NAMED_CHILD_ROLES = process_family_rule.NAMED_CHILD_ROLES
ANALYSIS_LANE_ROLE = process_family_rule.ANALYSIS_LANE_ROLE
FFMPEG_ROLE = process_family_rule.FFMPEG_ROLE
named_child_role = process_family_rule.named_child_role

_WEBKIT_FAMILY_ROLES = frozenset((*WEBKIT_ROLES.values(), "webkit-other"))

# Locked signals ((private path) Pressure cluster, PERFMODE-05).
PRESSURE_CHURN_WEIGHT_SWAP = 10
PRESSURE_CHURN_WINDOW_S = 1
PRESSURE_SAMPLE_IDLE_MS = 1000
PRESSURE_SAMPLE_ELEVATED_MS = 5000
PRESSURE_SAMPLE_P95_WALL_MS = 5

# Back-compat alias for tests that pin the old constant name.
CACHE_TTL_SECONDS = PRESSURE_SAMPLE_IDLE_MS / 1000.0

# The three numbers a deck-load row stamps, mapped from the sampler's keys.
_EXPOSED_FIELDS = (
    ("load_avg_1m", "load_average_1m"),
    ("mem_free_mb", "free_memory_mb"),
    ("swap_used_mb", "swap_used_mb"),
)


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


def _read_vm_stat_churn_snapshot() -> dict[str, int] | None:
    try:
        from apps.shared.machine_metrics import _vm_stat_page_count
    except ImportError:
        return None
    try:
        proc = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2.0, check=True)
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
    swap_delta = (current["swapins"] - prior["swapins"]) + (current["swapouts"] - prior["swapouts"])
    decomp_delta = current["decompressions"] - prior["decompressions"]
    swap_rate = swap_delta / elapsed_s
    decomp_rate = decomp_delta / elapsed_s
    overlay: dict[str, float] = {
        "swap_rate": round(swap_rate, 3),
        "decomp_rate": round(decomp_rate, 3),
        "churn_score": round(swap_rate * PRESSURE_CHURN_WEIGHT_SWAP + decomp_rate, 3),
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
    from apps.shared.machine_metrics import machine_metrics

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
        overlay.update(_churn_overlay(prior_vm_stat, prior_vm_stat_at, now, current_vm_stat))
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
        exposed: float(raw[source]) for exposed, source in _EXPOSED_FIELDS if isinstance(raw.get(source), (int, float))
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
    start_time: float | None = None


@dataclass(frozen=True)
class LiveProcessFamily:
    """One live walk: members (with pid), family pids, and WebKit association status."""

    members: list[dict[str, Any]]
    pids: set[int]
    webkit_association: dict[str, str]


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
    # ``name`` is prefetched with the rest: a live ``proc.name()`` call here
    # raised NoSuchProcess for a process that exited mid-walk, and that one
    # error emptied the whole walk (the family read as unavailable).
    for proc in psutil.process_iter(
        ["pid", "ppid", "cmdline", "name", "memory_info", "create_time"]
    ):
        try:
            info = proc.info
        except (psutil.Error, OSError):
            continue
        pid = info.get("pid")
        ppid = info.get("ppid")
        if not isinstance(pid, int) or not isinstance(ppid, int):
            continue
        cmdline = info.get("cmdline") or []
        command = " ".join(str(part) for part in cmdline) if cmdline else str(info.get("name") or "")
        memory_info = info.get("memory_info")
        rss_bytes = memory_info.rss if memory_info is not None else None
        ct = info.get("create_time")
        start_time = float(ct) if isinstance(ct, (int, float)) else None
        rows.append(
            ProcessInfo(pid=pid, ppid=ppid, command=command, rss_bytes=rss_bytes, start_time=start_time)
        )
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


# What a process RUNS AS, not what its command line merely mentions: the
# packaged desktop shell is the bundle's main executable. A shell or ``uv``
# command that only passes an ``opendj-*`` path as an argument (a ``--data-dir``
# made by ``mkdtemp(prefix="opendj-...")``, a log path) is NOT a launcher.
# The desktop shell's install path can hold spaces ("Open DJ.app",
# "Program Files"), so its executable is matched up to the first option.
_OWN_DESKTOP_SHELL_RE = re.compile(r'^"?(?:[A-Za-z]:)?[/\\](?:(?! -)[^"])*?[/\\]opendj-desktop(?:\.exe)?(?=[\s"]|$)')


# The launch identity every platform shows: an interpreter run with
# ``-m apps.webui.server`` or ``-m apps.engine_core``. On Windows it is the
# ONLY identity a Python Open DJ process has, because setproctitle there does
# not change the command line other processes read, so a ``--reload``
# supervisor reads as ``python.exe -m apps.webui.server --reload``. On POSIX the
# title above replaces it once the process has started. Interpreter options
# may precede ``-m`` (``-u``, ``-X utf8``, ``-W ignore``), but nothing else: a
# script path or ``-c`` before it makes the module name only text.
# The backend supervisor the app starts (setproctitle title ``opendj-backend``),
# whose --reload worker is the engine.
_OWN_BACKEND_SUPERVISOR_RE = re.compile(r'^"?(?:[^\s"]*[/\\])?opendj-backend(?=[\s"]|$)')
_OPENDJ_MODULE_LAUNCH_RE = re.compile(
    r'^"?(?:[^"]*[/\\])?python[0-9.]*(?:w)?(?:\.exe)?"?'
    r"(?:\s+(?:-[WX]\s*[^\s-]\S*|-(?![mcWX])[A-Za-z]+))*"
    r"\s+-m\s+apps\.(?:webui\.server|engine_core)(?=\s|$)",
    re.IGNORECASE,
)


def _runs_as_app_launcher(command: str) -> bool:
    """True when a parent process is one of the app's own launchers.

    The family climbs only through the packaged desktop shell and the ``--reload``
    server launch. Any other ``opendj-*`` program (the native performance probe,
    a hub, a lease tool) is a neighbor that happens to share the name, not the
    engine's launcher, so climbing through it would pull in everything it
    spawned.
    """

    text = command.lstrip()
    return bool(
        _OWN_DESKTOP_SHELL_RE.match(text)
        or _OWN_BACKEND_SUPERVISOR_RE.match(text)
        or _OPENDJ_MODULE_LAUNCH_RE.match(text)
    )


# The engine's launch identity from its desktop shell: every shell that
# spawns the engine (the Electron main process in ``apps/desktop/electron/
# src/engine.ts`` ``spawnEngine``, the Tauri shell in ``engine.rs``) puts its
# OWN pid in the engine's environment under this name, and
# ``apps/engine_core/parent_watch.py`` reads the same variable to exit when
# that shell dies. It is the trustworthy way to find the Electron shell, whose
# executable is ``Open DJ`` when packaged and ``electron`` when not, so no
# name pattern can say it is Open DJ's.
LAUNCHING_SHELL_PID_ENV = "OPENDJ_PARENT_PID"


def launching_shell_pid(environ: Mapping[str, str] | None = None) -> int | None:
    """The pid this engine's shell stamped into its environment, or None.

    Unset, empty, non-numeric or non-positive means no shell launched this
    engine (a CLI engine, pytest, a dev server), the same reading as
    ``parent_watch``.
    """

    raw = (os.environ if environ is None else environ).get(LAUNCHING_SHELL_PID_ENV)
    if raw is None:
        return None
    try:
        pid = int(raw.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


# The shell's REGISTRATION with an engine it did not necessarily launch. Every
# desktop shell writes its own pid to this owner-only file in the engine's data
# dir on every path by which it comes to share an engine: before it spawns one
# (``apps/desktop/electron/src/boot.ts`` ``spawnFreshEngine``, Tauri
# ``main.rs`` ``spawn_fresh_engine``), when it ADOPTS a healthy engine that
# already holds the lock (``boot.ts`` ``adopt``, ``main.rs`` ``start_engine``),
# and when it restarts one (``supervisor.ts``, ``supervisor.rs``
# ``restart_engine``). ``apps/engine_core/parent_watch.py`` already trusts the
# same file to decide when this engine must exit. An adopted engine was not
# launched by this shell, so the shell is neither its ancestor nor stamped in
# its environment; this file is then the only thing that names it. Keep the
# name in sync with ``parent_watch.PARENT_FILE_NAME`` (a test pins it).
SHELL_REGISTRATION_FILE_NAME = ".engine.parent"
# Allowance before a registration reads as future-dated: a slewing clock, not a reason to trust it.
_REGISTRATION_CLOCK_SKEW_S = 5.0


@dataclass(frozen=True)
class ShellRegistration:
    """What the engine's data dir says about the shell sharing it.

    ``status`` is ``absent`` (no file: no shell registered), ``accepted`` (the
    named pid is this user's running process that existed when the file was
    written) or ``rejected`` with a ``reason``. A rejected registration is
    never guessed at: its pid is not used, and the route reports why.
    """

    status: str
    pid: int | None = None
    reason: str | None = None

    def report(self) -> dict[str, Any]:
        """The privacy-reduced form the route returns: no pid."""
        out: dict[str, Any] = {"status": self.status}
        if self.reason is not None:
            out["reason"] = self.reason
        return out


def _rejected(reason: str) -> ShellRegistration:
    return ShellRegistration(status="rejected", reason=reason)


def _registration_file_trusted(stat: os.stat_result) -> str | None:
    """Why the file cannot be trusted as this user's own, or None.

    POSIX only (the platform seam): Windows has no uid on ``os.stat`` and the
    data dir's own ACL is what protects it there.
    """

    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    if stat.st_uid != getuid():
        return "the registration file is not owned by this user"
    if stat.st_mode & 0o022:
        return "the registration file is writable by other users"
    return None


def _registered_pid(path: str) -> tuple[int, float] | ShellRegistration:
    """The pid the file holds and the time it was written, or why not."""

    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return ShellRegistration(status="absent")
    except OSError as err:
        return _rejected(f"the registration file is unreadable: {err.strerror}")
    untrusted = _registration_file_trusted(stat)
    if untrusted is not None:
        return _rejected(untrusted)
    # A write dated ahead of now means the clock moved back since it was made. Then the
    # create-time check below would accept any later process given the same pid (Codex on #6785).
    if stat.st_mtime > time.time() + _REGISTRATION_CLOCK_SKEW_S:
        return _rejected("the registration is dated in the future (the clock moved back after it was written)")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read(64)
    except OSError as err:
        return _rejected(f"the registration file is unreadable: {err.strerror}")
    try:
        pid = int(raw.strip())
    except ValueError:
        return _rejected("the registration file does not hold a pid")
    if pid <= 0:
        return _rejected("the registration file does not hold a positive pid")
    return pid, stat.st_mtime


def _live_registration(pid: int, written_at: float) -> ShellRegistration:
    """Accept ``pid`` only if it is running and existed when it was registered."""

    import psutil

    try:
        process = psutil.Process(pid)
        created = process.create_time()
        status = process.status()
    except psutil.NoSuchProcess:
        return _rejected("the registered shell is not running")
    except psutil.Error as err:
        return _rejected(f"the registered shell could not be read: {type(err).__name__}")
    if status == psutil.STATUS_ZOMBIE:
        return _rejected("the registered shell has exited (zombie)")
    if created > written_at:
        return _rejected("the registered pid started after the registration was written (a reused pid)")
    return ShellRegistration(status="accepted", pid=pid)


def read_shell_registration(data_dir: os.PathLike[str] | str | None) -> ShellRegistration:
    """The shell registered in ``data_dir``, validated against the live process.

    Accepted only when the file is this user's and not writable by others,
    is not dated in the future, names a positive pid, that pid is running (not
    a zombie), and the process STARTED NO LATER than the file was written. The last check is the
    create-time proof against a reused pid: a shell that died leaves its file
    behind, and a later process given the same pid started after the file's
    write, so it is rejected rather than counted as the shell.
    """

    if data_dir is None:
        return ShellRegistration(status="absent")
    found = _registered_pid(os.path.join(os.fspath(data_dir), SHELL_REGISTRATION_FILE_NAME))
    if isinstance(found, ShellRegistration):
        return found
    return _live_registration(*found)


def _ancestor_chain(by_pid: dict[int, ProcessInfo], pid: int) -> list[int]:
    """``pid`` and its running ancestors, nearest first."""

    chain = [pid]
    seen = {pid}
    while True:
        row = by_pid.get(chain[-1])
        if row is None or row.ppid in seen or row.ppid not in by_pid:
            return chain
        seen.add(row.ppid)
        chain.append(row.ppid)


def _family_root_pid(by_pid: dict[int, ProcessInfo], engine_pid: int, shell_pid: int | None = None) -> int:
    """The topmost Open DJ ancestor of ``engine_pid``, or the engine itself.

    Climbs while the parent runs as an Open DJ program: the packaged desktop
    shell above the engine, or the ``--reload`` supervisor above a dev
    backend worker. It stops at the first parent that is not one (``uv``, a
    shell, launchd, a test runner), so a second engine, a vite dev server or
    any other ``opendj-*`` process elsewhere on the machine is never reached:
    the family is this engine's own tree, not every process that shares a
    name with it.

    ``shell_pid`` is the pid the launching shell stamped into the engine's
    environment (``launching_shell_pid``). It roots the family at that shell
    when, and only when, it is a running ANCESTOR of the engine: a stale or
    recycled pid naming any other process is ignored, so it can never pull a
    foreign tree in.
    """

    chain = _ancestor_chain(by_pid, engine_pid)
    depth = 0
    while depth + 1 < len(chain) and _runs_as_app_launcher(by_pid[chain[depth + 1]].command):
        depth += 1
    if shell_pid is not None and shell_pid in chain[1:]:
        depth = max(depth, chain.index(shell_pid))
    return chain[depth]


# The Electron Chromium helpers under the desktop shell; each is tagged by
# ``--type=``. Those arguments alone would match any Chromium-based browser on
# the machine; they are trusted only inside a family rooted at the shell that
# stamped its pid into this engine's environment (``launching_shell_pid``) or
# at a registered adopting shell. Only ``electron-renderer`` is the page's own
# process (``PAGE_PROCESS_ROLES`` in the frontend is unchanged).
_CHROMIUM_TYPE_ROLES = (
    (re.compile(r"(?:^|\s)--type=renderer(?=\s|$)"), ELECTRON_RENDERER_ROLE),
    (re.compile(r"(?:^|\s)--type=gpu-process(?=\s|$)"), ELECTRON_GPU_ROLE),
    (re.compile(r"(?:^|\s)--type=(?:utility|ppapi|plugin)(?=\s|$)"), ELECTRON_UTILITY_ROLE),
)


def _chromium_helper_role(command: str) -> str | None:
    for pattern, role in _CHROMIUM_TYPE_ROLES:
        if pattern.search(command):
            return role
    return None


def _pick_association_shell_row(
    by_pid: dict[int, ProcessInfo],
    family_pids: set[int],
    root_pid: int,
    registered_shell_pid: int | None,
) -> ProcessInfo | None:
    for candidate in (root_pid, registered_shell_pid):
        if candidate is None:
            continue
        row = by_pid.get(candidate)
        if row is None or candidate not in family_pids:
            continue
        if SHELL_MARKER not in row.command:
            continue
        if row.start_time is None:
            continue
        return row
    return None


def _desktop_shell_for_roles(
    by_pid: dict[int, ProcessInfo],
    family_pids: set[int],
    root_pid: int,
    registered_shell_pid: int | None,
) -> int | None:
    """A desktop shell pid in the family for ``process_role``, if any."""

    for candidate in (root_pid, registered_shell_pid):
        if candidate is None:
            continue
        row = by_pid.get(candidate)
        if row is None or candidate not in family_pids:
            continue
        if SHELL_MARKER not in row.command:
            continue
        return candidate
    return None


def _webkit_association_body(
    *,
    shell_row: ProcessInfo | None,
    global_shell_count: int,
    assoc: dict[str, Any] | None,
) -> dict[str, str]:
    if shell_row is None:
        return {
            "status": "not_applicable",
            "reason": "no Open DJ desktop shell with a known start time is in this family",
        }
    if global_shell_count > 1:
        return {
            "status": "refused",
            "reason": (
                f"{global_shell_count} Open DJ desktop shells are running, "
                "so no WebKit helper can be tied to this one"
            ),
        }
    assert assoc is not None
    if assoc.get("webkit_cluster_ambiguous"):
        refusal = assoc.get("webkit_refusal")
        if refusal == "contested":
            reason = "another running Open DJ shell could equally claim the WebKit helpers"
        elif refusal == "second_cluster":
            reason = "a second WebContent cluster in the window may belong to another app"
        else:
            reason = "WebKit helper association is ambiguous under the timing rule"
        return {"status": "refused", "reason": reason}
    if assoc.get("webkit_cluster_found"):
        return {"status": "attributed", "reason": str(assoc["webkit_rule"])}
    return {
        "status": "none_found",
        "reason": (
            f"no WebContent launched within {int(WEBKIT_ASSOCIATION_WINDOW_SECONDS)} s after the shell"
        ),
    }


def _merge_webkit_into_family(
    rows: list[ProcessInfo],
    by_pid: dict[int, ProcessInfo],
    family_pids: set[int],
    assoc_shell: ProcessInfo | None,
    global_shell_count: int,
) -> tuple[dict[str, Any] | None, dict[int, str]]:
    """Run ``associate_family`` when allowed and return association plus roles by pid."""

    if assoc_shell is None or global_shell_count > 1:
        return None, {}

    def start_seconds(pid: int) -> float | None:
        row = by_pid.get(pid)
        if row is None:
            return None
        return row.start_time

    _selected, assoc_dict = associate_family(rows, assoc_shell, start_seconds)
    assoc_role_by_pid = {row.pid: role for row, role in _selected}
    for wk_pid, wk_role in assoc_role_by_pid.items():
        if wk_pid in family_pids:
            continue
        if wk_role in _WEBKIT_FAMILY_ROLES:
            family_pids.add(wk_pid)
    return assoc_dict, assoc_role_by_pid


def _live_family_members(
    *,
    by_pid: dict[int, ProcessInfo],
    family_pids: set[int],
    shell_tree: set[int],
    assoc_shell: ProcessInfo | None,
    assoc_role_by_pid: dict[int, str],
    role_shell_pid: int | None,
) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for pid in sorted(family_pids):
        row = by_pid.get(pid)
        if row is None:
            continue
        member: dict[str, Any] = {"pid": pid, "name": opendj_process_name(row.command)}
        role: str | None = None
        if pid in shell_tree:
            chromium_role = _chromium_helper_role(row.command)
            if chromium_role is not None:
                role = chromium_role
        if role is None and pid in assoc_role_by_pid:
            role = assoc_role_by_pid[pid]
        if role is None and role_shell_pid is not None and assoc_shell is not None:
            # Without a shell start time we cannot run WebKit association; keep the
            # historic rule: Chromium --type= only under shell_tree, else no role
            # (tests hand-build rows with no start_time).
            role = process_role(row, role_shell_pid, family_pids - {role_shell_pid})
        if role is not None:
            member["role"] = role
        if isinstance(row.rss_bytes, int) and row.rss_bytes >= 0:
            member["rss_mb"] = round(row.rss_bytes / 1_048_576, 1)
        members.append(member)
    return members


def _collect_live_process_family(
    rows: list[ProcessInfo],
    *,
    engine_pid: int,
    shell_pid: int | None = None,
    registered_shell_pid: int | None = None,
) -> LiveProcessFamily:
    """This engine's family: Open DJ root, descendants, optional WebKit cluster."""

    by_pid = {row.pid: row for row in rows}
    root_pid = _family_root_pid(by_pid, engine_pid, shell_pid)
    family_pids: set[int] = {root_pid, engine_pid}
    family_pids.update(_descendant_pids(rows, root_pid))
    shell_tree: set[int] = set()
    if shell_pid is not None and root_pid == shell_pid:
        shell_tree |= family_pids
    if registered_shell_pid is not None and registered_shell_pid in by_pid:
        shell_tree |= {registered_shell_pid} | _descendant_pids(rows, registered_shell_pid)
        family_pids |= shell_tree
    family_pids &= by_pid.keys()

    assoc_shell = _pick_association_shell_row(by_pid, family_pids, root_pid, registered_shell_pid)
    global_shells = shell_candidates(rows)
    assoc_dict, assoc_role_by_pid = _merge_webkit_into_family(
        rows, by_pid, family_pids, assoc_shell, len(global_shells)
    )
    webkit_assoc = _webkit_association_body(
        shell_row=assoc_shell,
        global_shell_count=len(global_shells),
        assoc=assoc_dict,
    )
    role_shell_pid = _desktop_shell_for_roles(by_pid, family_pids, root_pid, registered_shell_pid)
    members = _live_family_members(
        by_pid=by_pid,
        family_pids=family_pids,
        shell_tree=shell_tree,
        assoc_shell=assoc_shell,
        assoc_role_by_pid=assoc_role_by_pid,
        role_shell_pid=role_shell_pid,
    )
    return LiveProcessFamily(members=members, pids=family_pids, webkit_association=webkit_assoc)


def _walk_identity(engine_pid: int | None, shell_pid: int | None, registered_shell_pid: int | None) -> dict[str, Any]:
    """Who the walk is for: this engine and the shell its environment names by
    default, or exactly the pids a caller passes (a test rooting the walk at a
    real process of its own, whose environment is not this one). The
    registered shell is always the caller's: only it knows the data dir."""

    if engine_pid is None:
        return {
            "engine_pid": os.getpid(),
            "shell_pid": launching_shell_pid(),
            "registered_shell_pid": registered_shell_pid,
        }
    return {
        "engine_pid": engine_pid,
        "shell_pid": shell_pid,
        "registered_shell_pid": registered_shell_pid,
    }


def live_process_family(
    *,
    engine_pid: int | None = None,
    shell_pid: int | None = None,
    registered_shell_pid: int | None = None,
    process_iter: Callable[[], list[ProcessInfo]] | None = None,
) -> LiveProcessFamily:
    """Walk this engine's live family via psutil; include WebKit helpers when attributable."""

    rows = _read_process_rows(process_iter)
    if not rows:
        return LiveProcessFamily(
            [],
            set(),
            {"status": "not_applicable", "reason": "the process table could not be read"},
        )
    return _collect_live_process_family(rows, **_walk_identity(engine_pid, shell_pid, registered_shell_pid))


def live_process_family_members(
    *,
    engine_pid: int | None = None,
    shell_pid: int | None = None,
    registered_shell_pid: int | None = None,
    process_iter: Callable[[], list[ProcessInfo]] | None = None,
) -> list[dict[str, Any]]:
    """Walk this engine's live process family via psutil. Members omit command and pid."""

    family = live_process_family(
        engine_pid=engine_pid,
        shell_pid=shell_pid,
        registered_shell_pid=registered_shell_pid,
        process_iter=process_iter,
    )
    return [{key: value for key, value in member.items() if key != "pid"} for member in family.members]


def live_process_family_state(
    *,
    engine_pid: int | None = None,
    shell_pid: int | None = None,
    registered_shell_pid: int | None = None,
    process_iter: Callable[[], list[ProcessInfo]] | None = None,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Like ``live_process_family_members``, plus family PIDs, for the route's merge.

    Here each member also carries its ``pid`` so the route can attach the
    native probe's row for the same process; the route strips it before any
    member leaves the engine.
    """

    family = live_process_family(
        engine_pid=engine_pid,
        shell_pid=shell_pid,
        registered_shell_pid=registered_shell_pid,
        process_iter=process_iter,
    )
    return family.members, family.pids


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
