"""Run a command and, if it is still running at set times, record the native state of
its matching descendant processes (issue #4281).

#4281 is the webkit half of the deck-load smoke hanging to Playwright's 120s timeout
on the `agentbox` host, 1 to 3s after the deck's audio bytes arrive. The traces show
the page's main thread gone silent while the compositor still paints, and nothing
says WHERE it is stuck: a native WebKit/GStreamer/PulseAudio wait, or a busy loop
only WebKit reaches. The issue names the one measurement that settles it, a native
backtrace of the WebProcess taken DURING the hang. Playwright closes the browser when
the test times out, so a backtrace taken from a failure step afterwards finds nothing;
it has to be taken while the step is still running. This wrapper does that.

    python3 -m scripts.ci_hang_capture --out DIR [--at 60,95] [--match WebKitWebProces] \
        -- <command...>

It starts the command, and at each `--at` offset (seconds since start) that the
command is still running, writes `DIR/capture-<offset>s.txt` covering every
process in its tree (itself and every descendant) whose `comm` contains `--match`:

- per thread: name, scheduler state, kernel wait channel, and CPU ticks used over a
  `--sample` window, so a spin reads BUSY and a wait reads BLOCKED with the wchan;
- a `gdb -batch -ex 'thread apply all bt'` of the process. Yama's ptrace_scope=1
  refuses an attach from a non-ancestor, so on "Operation not permitted" it retries
  once under `sudo -n`.

Two offsets, not one: identical stacks in both say stuck; different stacks say slow.

It never signals, stops or kills anything other than what gdb's attach does (a brief
pause of the target), and it exits with the command's own exit code, so it can never
turn a red step green or a green one red. A capture that cannot measure says so:
no matching process, an unreadable /proc entry, a missing gdb or a refused attach
each print UNKNOWN with the reason, never an empty or clean-looking record.

Requirements:
- ✔︎ The wrapped command's exit code passes through unchanged.
- ✔︎ A command that finishes before the first offset leaves no capture file.
- ✔︎ A spinning descendant reads BUSY; a sleeping one reads BLOCKED.
- ✔︎ No matching process is recorded as UNKNOWN, naming the pattern.
- ✔︎ A gdb that is missing or refused is recorded as UNKNOWN, with its output.
- ✔︎ Off Linux (no /proc) the command still runs; the capture says it is unavailable.

Acceptance tests (tests/scripts/test_ci_hang_capture.py):
- [if] the command exits 3 [then] the wrapper exits 3 [⛔️ if a capture step could
  launder a failing playwright run].
- [if] a matched child spins [then] its thread reads BUSY [⛔️ if a busy loop would be
  reported as a native wait, sending #4281 to the wrong layer].
- [if] the pattern matches nothing [then] the capture says UNKNOWN [⛔️ if an empty
  capture could be read as "the WebProcess was fine"].

Standard library only: the e2e gate runs it with the runner's bare `python3`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")
DEFAULT_MATCH = "WebKitWebProces"  # comm is truncated to 15 bytes by the kernel
DEFAULT_AT = (60.0, 95.0)
BUSY_FRACTION = 0.5
GDB_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ThreadSample:
    tid: int
    name: str
    state: str
    wchan: str
    ticks: int


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _stat_fields(text: str) -> tuple[str, list[str]]:
    """Split /proc/<pid>/stat into (comm, fields after comm); comm may hold spaces."""
    open_paren = text.index("(")
    close_paren = text.rindex(")")
    return text[open_paren + 1 : close_paren], text[close_paren + 2 :].split()


def _ppid_map() -> dict[int, int]:
    parents: dict[int, int] = {}
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        text = _read(entry / "stat")
        if text is None:
            continue
        _, fields = _stat_fields(text)
        parents[int(entry.name)] = int(fields[1])
    return parents


def descendants(root: int) -> list[int]:
    parents = _ppid_map()
    children: dict[int, list[int]] = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return sorted(found)


def process_comm(pid: int) -> str | None:
    text = _read(PROC / str(pid) / "comm")
    return None if text is None else text.strip()


def _thread_samples(pid: int) -> dict[int, ThreadSample]:
    samples: dict[int, ThreadSample] = {}
    task_dir = PROC / str(pid) / "task"
    try:
        tids = sorted(int(t.name) for t in task_dir.iterdir() if t.name.isdigit())
    except OSError:
        return samples
    for tid in tids:
        text = _read(task_dir / str(tid) / "stat")
        if text is None:
            continue
        name, fields = _stat_fields(text)
        # fields[0] is state; utime and stime are stat fields 14 and 15, i.e.
        # fields[11] and fields[12] once pid and comm are removed.
        samples[tid] = ThreadSample(
            tid=tid,
            name=name,
            state=fields[0],
            wchan=(_read(task_dir / str(tid) / "wchan") or "?").strip() or "0",
            ticks=int(fields[11]) + int(fields[12]),
        )
    return samples


def thread_report(pid: int, window_s: float) -> list[str]:
    """One line per thread: BUSY or BLOCKED over the window, with state and wchan."""
    before = _thread_samples(pid)
    if not before:
        return [f"  threads: UNKNOWN (/proc/{pid}/task unreadable or process gone)"]
    time.sleep(window_s)
    after = _thread_samples(pid)
    hz = os.sysconf("SC_CLK_TCK")
    lines = []
    for tid, end in sorted(after.items()):
        start = before.get(tid)
        if start is None:
            lines.append(f"  tid {tid} {end.name!r}: NEW during the window, state {end.state}")
            continue
        cpu = (end.ticks - start.ticks) / hz / window_s
        verdict = "BUSY" if cpu >= BUSY_FRACTION else "BLOCKED"
        lines.append(
            f"  tid {tid} {end.name!r}: {verdict} cpu={cpu:.2f} of one core, state {end.state}, wchan {end.wchan}"
        )
    gone = sorted(set(before) - set(after))
    if gone:
        lines.append(f"  threads exited during the window: {gone}")
    return lines


def gdb_backtrace(pid: int, gdb: str) -> list[str]:
    resolved = shutil.which(gdb)
    if resolved is None:
        return [f"  gdb: UNKNOWN ({gdb!r} is not installed or not on PATH)"]
    base = [resolved, "-p", str(pid), "-batch", "-nx", "-ex", "thread apply all bt"]
    attempts = [base]
    sudo = shutil.which("sudo")
    if sudo is not None:
        attempts.append([sudo, "-n", *base])
    out = ""
    for cmd in attempts:
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=GDB_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            return [f"  gdb: UNKNOWN (timed out after {GDB_TIMEOUT_S:.0f}s: {' '.join(cmd)})"]
        out = run.stdout + run.stderr
        if "#0 " in run.stdout:
            return [f"  gdb ({' '.join(cmd[:2])}...):", *("    " + ln for ln in out.splitlines())]
    return [
        "  gdb: UNKNOWN (no backtrace; last attempt printed:)",
        *("    " + ln for ln in out.splitlines()[-30:]),
    ]


def capture(root: int, match: str, window_s: float, gdb: str) -> list[str]:
    lines = [f"hang capture at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC"]
    if not PROC.is_dir():
        return [*lines, f"UNKNOWN: no {PROC} on {sys.platform}; capture unavailable here"]
    tree = [root, *descendants(root)]
    matched = [p for p in tree if match in (process_comm(p) or "")]
    if not matched:
        seen = ", ".join(f"{p}:{process_comm(p)}" for p in tree)
        return [
            *lines,
            f"UNKNOWN: no process in the tree of pid {root} has {match!r} in its comm.",
            f"processes seen: {seen}",
        ]
    for pid in matched:
        lines.append(f"pid {pid} ({process_comm(pid)}):")
        lines.extend(thread_report(pid, window_s))
        lines.extend(gdb_backtrace(pid, gdb))
    return lines


def _parse_at(raw: str) -> tuple[float, ...]:
    offsets = tuple(sorted(float(x) for x in raw.split(",") if x.strip()))
    if not offsets or offsets[0] <= 0:
        raise argparse.ArgumentTypeError("--at needs one or more positive seconds")
    return offsets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--at", type=_parse_at, default=DEFAULT_AT)
    parser.add_argument("--match", default=DEFAULT_MATCH)
    parser.add_argument("--sample", type=float, default=2.0)
    parser.add_argument("--gdb", default="gdb")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")

    start = time.monotonic()
    child = subprocess.Popen(command)
    for offset in args.at:
        try:
            child.wait(timeout=max(0.0, start + offset - time.monotonic()))
            break
        except subprocess.TimeoutExpired:
            pass
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"capture-{offset:g}s.txt"
        lines = capture(child.pid, args.match, args.sample, args.gdb)
        path.write_text("\n".join(lines) + "\n")
        print(f"ci_hang_capture: still running at {offset:g}s; wrote {path}", file=sys.stderr)
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())
