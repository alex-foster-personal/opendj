# /// script
# requires-python = ">=3.11"
# dependencies = ["psutil>=5.9"]
# ///
"""perf-lab process-family sampler: OS-level ground truth for CPU and memory.

Every attribution the analyzer makes (trace slices, JS samples, allocator
dumps, heap snapshots) is a FRACTION of something. This sampler records that
something: per-process and per-thread CPU seconds and resident memory, read
from the OS rather than from any profiler, at a fixed cadence. The coverage
score divides attributed amounts by these totals.

Roles are resolved from the command line, never by process name alone (a
machine can run several Chromes; see (private path) "Traps"):

  chrome-renderer   --type=renderer (not extensions)
  chrome-gpu        --type=gpu-process
  chrome-audio      --type=utility --utility-sub-type=audio.mojom.AudioService
  chrome-utility    other --type=utility
  chrome-browser    the browser process (no --type)
  engine-python     python -m apps.engine_core serve
  odj-audio         the Rust engine
  vite              the dev server
  chrome-other      a descendant no rule names, under a Chromium process
  other             any other descendant no rule names (vite's esbuild)

Memory: RSS everywhere; USS and PSS on Linux (psutil memory_full_info), which
is what a per-process "who holds this" answer needs because shared pages are
otherwise counted once per process.

Scope: only processes descended from (or equal to) a root are sampled, so
another Chrome, vite or engine on the machine never adds to the totals. Roots
are --root-pid (repeatable; the engine and vite pids) plus, with --driver-dir,
the driver's own pid from DIR/driver.pid once driver.mjs writes it (Chromium
runs as a child of the driver). --unscoped samples every matching process and
records scope "unscoped"; without roots and without it the sampler refuses.

driver.pid is trusted only when it names THIS run's driver: a live process
whose command line runs driver.mjs and that was created no earlier than the
sampler was launched (DRIVER_SLACK_S of slack). The launch time is the
creation time of the oldest consecutive ancestor whose command line names
sampler.py, so under `uv run --script` it is uv's start, not the later start of
the Python it spawns: the documented flow backgrounds the sampler and starts
the driver at once, so the driver can write driver.pid before this process
even runs, and that pid is accepted. A driver.pid naming a dead process, a
process that is not driver.mjs, or one older than the launch is a failed
earlier run's, whose surviving browser family is not this capture: refused
with exit 2 at startup, and never taken as a root during the loop (a
driver.pid that never validates exits 2 once the samples are written).

Usage:
  uv run --locked --script scripts/perf/lab/sampler.py --out DIR --root-pid ENGINE --root-pid VITE --driver-dir RUN
Stops on SIGTERM/SIGINT (after one last tick) or after --duration, and flushes.
The harness identity (lab code digest and libraries) is taken before the first
tick and again before sampler.json is written; if they differ the record
carries no harness, names why in harness_fault, and the sampler exits 3.
sampler.json records first_tick_t and last_tick_t (epoch seconds) and host:
ledger.py refuses a capture whose driver.json started_at..finished_at is not
inside that interval, so stop the sampler AFTER the driver exits and never give
it a --duration that can end first. Output: DIR/samples.jsonl (one JSON object
per tick), DIR/sampler.json (role map and totals).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import signal
import sys
import time
from pathlib import Path

import psutil


def _harness() -> tuple[str, dict[str, str]]:
    """manifest.harness_digest of this lab directory and the realized psutil
    and Python versions: the measurement code and libraries that sampled."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from manifest import harness_digest, measurement_deps

    return harness_digest(), measurement_deps(("psutil",))


def _harness_moved(before: tuple[str, dict[str, str]], after: tuple[str, dict[str, str]]) -> str | None:
    """Why the harness identity taken after sampling is not the one taken
    before it, or None when they agree."""
    if before[0] != after[0]:
        return f"the lab code changed while the sampler ran ({before[0][:12]} -> {after[0][:12]})"
    moved = sorted(k for k in before[1].keys() | after[1].keys() if before[1].get(k) != after[1].get(k))
    if moved:
        return f"the sampler's libraries changed while it ran: {', '.join(moved)}"
    return None


ROLE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("chrome-renderer", re.compile(r"--type=renderer(?!.*--extension-process)")),
    ("chrome-gpu", re.compile(r"--type=gpu-process")),
    ("chrome-audio", re.compile(r"--utility-sub-type=audio\.mojom\.AudioService")),
    ("chrome-utility", re.compile(r"--type=(utility|zygote|broker)")),
    ("engine-python", re.compile(r"apps\.engine_core serve")),
    # the native child: a POSIX path, or a Windows one ending in odj-audio.exe
    ("odj-audio", re.compile(r"(^|[/\\])odj-audio(\.exe)?(\s|$)", re.IGNORECASE)),
    ("vite", re.compile(r"node.*vite|vite.*dev")),
]
CHROME_BROWSER = re.compile(r"(chrome|chromium|Chromium|Google Chrome|Electron)(\.exe)?$")


def role_of(proc: psutil.Process) -> str | None:
    try:
        argv = proc.cmdline()
    except (psutil.Error, OSError):
        return None
    return role_of_argv(argv)


def role_of_argv(argv: list[str]) -> str | None:
    cmd = " ".join(argv)
    if not cmd:
        return None
    for role, pattern in ROLE_RULES:
        if pattern.search(cmd):
            if role == "engine-python" and "python" not in cmd and "opendj-engine" not in cmd:
                continue
            return role
    # argv[0], never the joined line cut at a space: macOS's Chrome lives at
    # ".../Google Chrome.app/Contents/MacOS/Google Chrome"
    exe = argv[0]
    if CHROME_BROWSER.search(os.path.basename(exe)) and "--type=" not in cmd:
        return "chrome-browser"
    return None


def snapshot(procs: dict[int, tuple[str, psutil.Process]]) -> dict:
    out: dict = {"t": time.time(), "procs": {}}
    for pid, (role, proc) in list(procs.items()):
        try:
            with proc.oneshot():
                cpu = proc.cpu_times()
                try:
                    full = proc.memory_full_info()
                    mem = {"rss": full.rss, "uss": getattr(full, "uss", None), "pss": getattr(full, "pss", None)}
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    mem = {"rss": proc.memory_info().rss, "uss": None, "pss": None}
                threads = {}
                try:
                    for th in proc.threads():
                        threads[str(th.id)] = th.user_time + th.system_time
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    threads = {}
                out["procs"][str(pid)] = {
                    "role": role,
                    "cpu_s": cpu.user + cpu.system,
                    # CPU of children this process has reaped: it grows when a
                    # descendant exits, even one born and gone between two ticks
                    "child_cpu_s": (cpu.children_user + cpu.children_system if hasattr(cpu, "children_user") else None),
                    **mem,
                    "threads": threads,
                }
        except psutil.NoSuchProcess:
            procs.pop(pid, None)
    return out


def thread_names(pid: int) -> dict[str, str]:
    """Linux only: thread id -> comm. Elsewhere the analyzer keys threads by id."""
    names: dict[str, str] = {}
    task = Path(f"/proc/{pid}/task")
    if not task.is_dir():
        return names
    for tdir in task.iterdir():
        try:
            names[tdir.name] = (tdir / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return names


def _in_scope(proc: psutil.Process, roots: set[int] | None) -> bool:
    if roots is None:
        return True
    if proc.pid in roots:
        return True
    try:
        return any(parent.pid in roots for parent in proc.parents())
    except (psutil.Error, OSError):
        return False


# a driver this much older than the sampler's launch is still this run's:
# creation times are read at 10 ms resolution on Linux, and an operator may
# type the two commands in the other order within a moment
DRIVER_SLACK_S = 0.5


def launch_time(proc: psutil.Process | None = None) -> float:
    """When this sampler was launched: the creation time of the oldest
    consecutive ancestor (or itself) whose command line names sampler.py, so a
    launcher such as `uv run --script sampler.py` counts from its own start."""
    proc = proc or psutil.Process()
    earliest = proc.create_time()
    with contextlib.suppress(psutil.Error, OSError):
        for parent in proc.parents():
            try:
                argv = parent.cmdline()
            except (psutil.Error, OSError):
                break
            if not any(os.path.basename(a) == "sampler.py" for a in argv):
                break
            earliest = min(earliest, parent.create_time())
    return earliest


def driver_pid(driver_dir: Path, launched: float) -> tuple[int | None, str | None]:
    """(pid, None) when DIR/driver.pid names this run's live driver; (None,
    why) when it names anything else; (None, None) when there is no readable
    pid yet (no file, or the driver is mid-write)."""
    try:
        text = (driver_dir / "driver.pid").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    try:
        pid = int(text)
    except ValueError:
        return None, None
    try:
        proc = psutil.Process(pid)
        argv = proc.cmdline()
        created = proc.create_time()
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None, f"driver.pid names pid {pid}, which has exited"
    except psutil.NoSuchProcess:
        return None, f"driver.pid names pid {pid}, which is not running"
    except (psutil.Error, OSError) as e:
        return None, f"driver.pid names pid {pid}, which cannot be read ({type(e).__name__})"
    if not any(os.path.basename(a) == "driver.mjs" for a in argv):
        return None, f"driver.pid names pid {pid}, which is not driver.mjs ({' '.join(argv)[:120]!r})"
    if created < launched - DRIVER_SLACK_S:
        return None, (
            f"driver.pid names pid {pid}, a driver.mjs started {launched - created:.1f} s before this sampler was launched"
        )
    return pid, None


class DriverRoot:
    """The driver root of one run: the first driver.pid that validates is this
    run's driver, and stays a root on every tick it still validates (a pid
    reused after it exits fails the creation-time check)."""

    def __init__(self, driver_dir: Path | None, launched: float) -> None:
        self.driver_dir = driver_dir
        self.launched = launched
        self.accepted: int | None = None
        self.refused: str | None = None

    def at_start(self) -> str | None:
        """Why a driver.pid already in the dir is refused, or None. The driver
        may already have written it (it is started right after the
        backgrounded sampler); only an earlier run's is refused."""
        if self.driver_dir is None or not (self.driver_dir / "driver.pid").exists():
            return None
        for _ in range(20):
            self.tick()
            if self.accepted is not None or self.refused is not None:
                break
            time.sleep(0.05)  # mid-write: the driver has created the file, not filled it
        return None if self.accepted is not None else (self.refused or "it never held a pid")

    def tick(self) -> int | None:
        """This run's driver pid while it validates, else None."""
        if self.driver_dir is None:
            return None
        pid, why = driver_pid(self.driver_dir, self.launched)
        if self.accepted is None:
            if pid is not None:
                self.accepted = pid
            elif why is not None:
                self.refused = why
        return pid if pid is not None and pid == self.accepted else None

    def record(self) -> dict | None:
        if self.driver_dir is None:
            return None
        return {"pid": self.accepted, "refused": None if self.accepted is not None else self.refused}


def _roots(root_pids: list[int], driver: int | None) -> set[int]:
    roots = set(root_pids)
    if driver is not None:
        roots.add(driver)
    return roots


def discover(procs: dict[int, tuple[str, psutil.Process]], roots: set[int] | None) -> None:
    for proc in psutil.process_iter():
        if proc.pid in procs:
            continue
        role = role_of(proc)
        if roots is None:
            # unscoped: only processes a rule names, or the whole machine would count
            if role:
                procs[proc.pid] = (role, proc)
            continue
        if not _in_scope(proc, roots):
            continue
        if role is None:
            # A descendant no rule names (vite's esbuild service, a new Chromium
            # helper) still spends the family's CPU and memory; dropping it would
            # let work moved into it read as a saving. Only a harness root (the
            # driver itself) is left out.
            if proc.pid in roots:
                continue
            role = _fallback_role(proc, procs)
        procs[proc.pid] = (role, proc)


def _fallback_role(proc: psutil.Process, procs: dict[int, tuple[str, psutil.Process]]) -> str:
    """The role of a descendant no rule names: chrome-other under a Chromium
    process, so a helper the rules do not know stays in the browser's CPU and
    memory (analyze.py sums every chrome* role), else other."""
    try:
        parents = proc.parents()
    except (psutil.Error, OSError):
        return "other"
    for parent in parents:
        known = procs.get(parent.pid)
        role = known[0] if known else role_of(parent)
        if role is None or role == "other":
            continue
        return "chrome-other" if role.startswith("chrome") else "other"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0.0, help="0 = until signalled")
    ap.add_argument("--root-pid", type=int, action="append", default=[], help="sample this process family")
    ap.add_argument("--driver-dir", type=Path, help="add the pid driver.mjs writes to DIR/driver.pid as a root")
    ap.add_argument("--unscoped", action="store_true", help="sample every matching process on the machine")
    args = ap.parse_args()
    if not (args.root_pid or args.driver_dir or args.unscoped):
        print(
            "[FAIL] no scope: pass --root-pid/--driver-dir, or --unscoped to sample the whole machine", file=sys.stderr
        )
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    existing = [n for n in ("samples.jsonl", "sampler.json") if (args.out / n).exists()]
    if existing:
        # Opening samples.jsonl would truncate an earlier capture's evidence.
        print(f"[FAIL] {args.out} already holds {', '.join(existing)}; use a fresh --out", file=sys.stderr)
        return 2
    driver = DriverRoot(args.driver_dir, launch_time())
    refusal = driver.at_start()
    if refusal is not None:
        print(
            f"[FAIL] {args.driver_dir} holds a driver.pid that is not this run's driver: {refusal}; use a fresh --out",
            file=sys.stderr,
        )
        return 2

    # The harness identity of the code and libraries this process loaded,
    # taken before the first tick: an end-of-run stamp alone would fingerprint
    # whatever is on disk by then, so a sampler.py or psutil replaced mid-run
    # would stamp the new identity over samples the old code took.
    started_as = _harness()

    stop = False

    def on_signal(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    procs: dict[int, tuple[str, psutil.Process]] = {}
    roles_seen: dict[str, dict] = {}
    names: dict[str, dict[str, str]] = {}
    start = time.time()
    ticks = 0
    first_tick_t: float | None = None
    last_tick_t: float | None = None
    with (args.out / "samples.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        # A tick is taken before the stop check, so a signal that lands during
        # the sleep still gets one last tick after it: the sampled interval
        # then reaches the moment the sampler was told to stop, which is what
        # ledger.py sampler_span holds against the driver's finished_at.
        while True:
            discover(procs, None if args.unscoped else _roots(args.root_pid, driver.tick()))
            snap = snapshot(procs)
            for pid, row in snap["procs"].items():
                roles_seen.setdefault(pid, {"role": row["role"]})
                if ticks % 10 == 0:
                    names[pid] = thread_names(int(pid)) or names.get(pid, {})
            fh.write(json.dumps(snap) + "\n")
            fh.flush()
            ticks += 1
            first_tick_t = snap["t"] if first_tick_t is None else first_tick_t
            last_tick_t = snap["t"]
            if stop or (args.duration and time.time() - start >= args.duration):
                break
            time.sleep(args.interval)
    # verified again before the result is written: a harness that changed
    # while it sampled has no single identity, so the record carries none
    # (ledger.py refuses a sampler.json with no harness) and names why
    harness_fault = _harness_moved(started_as, _harness())
    harness, deps = (None, None) if harness_fault else started_as
    (args.out / "sampler.json").write_text(
        json.dumps(
            {
                "interval_s": args.interval,
                "harness": harness,
                "measurement_deps": deps,
                "harness_fault": harness_fault,
                "ticks": ticks,
                # the sampled interval, as time.time() epoch seconds (UTC, no
                # zone to misread) of the first and last tick, and the host
                # whose clock took them: ledger.py sampler_span refuses a
                # capture whose driver ran outside it
                "first_tick_t": first_tick_t,
                "last_tick_t": last_tick_t,
                "host": platform.node(),
                "scope": "unscoped" if args.unscoped else "process-family",
                "roots": sorted(_roots(args.root_pid, driver.accepted)),
                "launched_at": driver.launched,
                "sampler_created_at": psutil.Process().create_time(),
                "driver": driver.record(),
                "roles": roles_seen,
                "thread_names": names,
            },
            indent=1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    if driver.accepted is None and driver.refused is not None:
        # a driver.pid appeared during the run and never named this run's driver
        print(f"[FAIL] the driver was never sampled: {driver.refused}", file=sys.stderr)
        return 2
    if harness_fault:
        print(f"[FAIL] UNKNOWN harness: {harness_fault}; {args.out}/sampler.json records none", file=sys.stderr)
        return 3
    print(f"[OK] sampler wrote {ticks} ticks for {len(roles_seen)} processes to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
