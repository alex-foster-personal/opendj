#!/usr/bin/env python3
"""Census, classify and reap test-spawned processes that outlived their owner.

Stdlib only and Python 3.9+, so the SAME file runs on every fleet host without
a checkout or a venv: ``ssh <host> python3 - census < scripts/orphan_reaper.py``.

WHY. the maintainer, Mon 28 Sep 2026: test runners do not clean up after themselves;
opendj servers, browsers and pytest workers keep being found orphaned. A process
started by a test is only killed by its harness if the harness itself exits
normally. When the harness is SIGKILLed (an agent's Bash timeout, a cancelled
CI step, an OOM kill) the servers it spawned in their own process group are
reparented to init and keep running with their ports, forever.

WHAT COUNTS AS AN ORPHAN. A process is IN SCOPE when its command looks
test-spawned (an opendj engine/webui/hub server, vite, a Playwright browser, a
pytest worker, ffmpeg) or its environment names a test harness or runner. Its
TREE ROOT is the ancestor whose parent is init (pid 1) or a Linux subreaper.
The tree is ORPHANED when that root is neither a supervised job (a launchd job
or app whose pid launchctl lists, a systemd unit's MainPID) nor a known
long-lived session host (tmux, screen, sshd, a runner's Listener). Every
orphan is then ATTRIBUTED by the environment it inherited, which survives
reparenting where argv and ancestry do not:

    runner   GITHUB_RUN_ID or RUNNER_NAME present (a CI job leftover)
    agent    CLAUDECODE / CODEX_* present, or cwd inside a worktree
    unknown  neither

``AF_SERVICE_ID`` containing ``.test.`` marks a process the test harness itself
spawned (see ``tests/support/spawned_servers.py``); any other AF_SERVICE_ID is
a legitimately long-lived service and is NEVER reaped.

    orphan_reaper.py census [--json]        # measure, never kills
    orphan_reaper.py reap [--dry-run] [--min-age-s N] [--report PATH]

``reap`` kills only ORPHANED, ATTRIBUTED (runner or agent) or ``.test.``
processes older than ``--min-age-s``, re-checking each pid's identity (start
time) before every signal so a recycled pid is never hit. Every kill is logged
with its evidence as one JSON line. It never prints environment VALUES other
than the allowlisted non-secret keys below.

Requirements (mini-PRD):
  ✔︎ census lists in-scope processes with pid, ppid chain root, age, cwd, command, allowlisted env
    - [if] a test server reparented to init with CLAUDECODE=1 is not reapable/agent [then ⛔️]
    - [if] a launchd/systemd job's main process carries AF_SERVICE_ID [then ⛔️ unless class=service]
    - [if] a token-bearing env key (GITHUB_TOKEN) appears in census output [then ⛔️]
  ✔︎ reap kills orphaned attributed trees and never a service
    - [if] an orphaned `.test.` server survives `reap` [then ⛔️]
    - [if] a process whose root is a supervised service is signalled [then ⛔️]
    - [if] a recycled pid (start time changed) is signalled [then ⛔️]
  ✔︎ reap reports counts so orphans are visible (JSON line to --report and stdout)
    - [if] reap kills N processes and the report says killed != N [then ⛔️]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- config


class CFG:
    # A test-spawned process, by its command line. Deliberately specific: a
    # bare "python" or "node" is far too broad to be evidence of anything.
    TEST_COMMAND_RE = re.compile(
        r"apps[./](engine_core|webui|hub|cloudsync)"
        r"|opendj-(engine|backend|frontend|hub|webui|spoke)"
        r"|\buvicorn\b"
        r"|\bvite\b( dev| preview|/bin/vite)"
        r"|\bpnpm\b.*\b(dev|preview)\b"
        r"|ms-playwright|chrome-headless-shell|headless_shell|--headless"
        r"|pw_run\.sh|Playwright\.app|MiniBrowser|-juggler"
        r"|@playwright/test|playwright test|playwright/cli"
        r"|\bpytest\b|xdist|node --test|\bvitest\b|esbuild --service"
        r"|\bffmpeg\b|\bffprobe\b"
        r"|Open ?DJ\.app|open-dj",
        re.IGNORECASE,
    )
    # System frameworks (Safari's WebKit XPC helpers match "webkit" words but
    # are the OS's, not ours).
    EXCLUDED_COMMAND_RE = re.compile(r"^/System/|^/usr/libexec/|orphan_reaper\.py")
    # Env keys whose VALUES are safe to print. Anything else is reported by
    # key NAME only when it is a marker, never by value.
    PRINTABLE_ENV_KEYS = (
        "AF_SERVICE_ID",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
        "GITHUB_WORKFLOW",
        "GITHUB_REPOSITORY",
        "RUNNER_NAME",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "XPC_SERVICE_NAME",
        "TEST_WORKER_INDEX",
        "PYTEST_XDIST_WORKER",
        "MDT_DATA_DIR",
    )
    MARKER_ENV_PREFIXES = ("CODEX_",)
    RUNNER_ENV_KEYS = ("GITHUB_RUN_ID", "RUNNER_NAME")
    AGENT_ENV_KEYS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
    # A cwd in one of these is an agent's (or a human's) checkout of the repo.
    WORKTREE_CWD_RE = re.compile(
        r"/music-dj-tools(-[\w.-]+)?(/|$)|/\.claude/worktrees/|/lanes/|/_idd/"
    )
    RUNNER_WORK_RE = re.compile(r"/_work(/|$)")
    # Roots that legitimately outlive their parent and host other people's
    # live sessions. A tree hanging under one of these is attached, not
    # orphaned, even though the root itself has ppid 1.
    SESSION_HOST_RE = re.compile(
        r"(^|/)(tmux|screen|sshd|Runner\.Listener|runsvc\.sh|systemd|launchd|login|zellij|herdr)(\s|:|$)"
    )
    # An agent session's own id (Claude profiles set one); inherited by all
    # its children, so it is AGENT provenance, never a service to protect.
    AGENT_SERVICE_ID_RE = re.compile(r"claude|codex|cursor|grok", re.IGNORECASE)
    TEST_SERVICE_MARKER = ".test."
    DEFAULT_MIN_AGE_S = 120
    TERM_GRACE_S = 3.0


# ---------------------------------------------------------------- model


@dataclass
class Proc:
    pid: int
    ppid: int
    pgid: int
    age_s: int
    state: str
    user: str
    command: str
    start: str = ""  # identity: start time, re-checked before every signal
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)  # allowlisted values only
    env_markers: list[str] = field(default_factory=list)  # key names only
    supervisor: str = (
        ""  # "job" (launchd job / systemd unit MainPID) | "app" (a GUI app launchd tracks) | ""
    )


@dataclass
class Row:
    pid: int
    ppid: int
    pgid: int
    root_pid: int
    root_command: str
    age_s: int
    state: str
    cwd: str
    command: str
    env: dict[str, str]
    env_markers: list[str]
    tree: str  # orphaned | supervised-job | supervised-app | session | attached
    attribution: str  # runner | agent | test-harness | service | unknown
    verdict: str  # reapable | service | active | zombie | unattributed-orphan


# ---------------------------------------------------------------- snapshot (imperative shell)


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout


def _parse_etime(etime: str) -> int:
    days, _, rest = etime.strip().rpartition("-")
    parts = [int(p) for p in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _filter_env(pairs: list[tuple[str, str]]) -> tuple[dict[str, str], list[str]]:
    env = {k: v for k, v in pairs if k in CFG.PRINTABLE_ENV_KEYS}
    markers = sorted({k for k, _ in pairs if k.startswith(CFG.MARKER_ENV_PREFIXES)})
    return env, markers


def _snapshot_darwin() -> dict[int, Proc]:
    procs: dict[int, Proc] = {}
    # LC_ALL=C pins lstart to exactly five tokens ("Mon Sep 28 09:17:09 2026"),
    # so the command is reliably everything after the eleventh field.
    out = _run(
        [
            "env",
            "LC_ALL=C",
            "ps",
            "-axww",
            "-o",
            "pid=,ppid=,pgid=,etime=,stat=,user=,lstart=,command=",
        ]
    )
    for line in out.splitlines():
        f = line.split(None, 11)
        if len(f) < 11:
            continue
        pid, ppid, pgid, etime, stat, user = f[:6]
        procs[int(pid)] = Proc(
            int(pid),
            int(ppid),
            int(pgid),
            _parse_etime(etime),
            stat,
            user,
            f[11] if len(f) == 12 else "",
            " ".join(f[6:11]),
        )
    for line in _run(["launchctl", "list"]).splitlines()[1:]:
        pid_s, _, rest = line.partition("\t")
        label = rest.partition("\t")[2]
        if pid_s.isdigit() and int(pid_s) in procs:
            procs[int(pid_s)].supervisor = "app" if label.startswith("application.") else "job"
    return procs


def _enrich_darwin(procs: dict[int, Proc], pids: list[int]) -> None:
    if not pids:
        return
    joined = ",".join(str(p) for p in pids)
    # -E appends the environment after the command. NEVER combine with -a/-e,
    # which mean "all processes" and silently override -p.
    for line in _run(["ps", "-Eww", "-o", "pid=,command=", "-p", joined]).splitlines():
        pid_s, _, text = line.strip().partition(" ")
        if not pid_s.isdigit() or int(pid_s) not in procs:
            continue
        pairs = re.findall(r"(?:^|\s)([A-Z][A-Z0-9_]*)=(\S*)", text)
        procs[int(pid_s)].env, procs[int(pid_s)].env_markers = _filter_env(pairs)
    pid = None
    for line in _run(["lsof", "-a", "-d", "cwd", "-p", joined, "-Fpn"]).splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid in procs:
            procs[pid].cwd = line[1:]


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _snapshot_linux() -> dict[int, Proc]:
    procs: dict[int, Proc] = {}
    ticks = os.sysconf("SC_CLK_TCK")
    uptime = float((_read("/proc/uptime") or b"0").split()[0])
    uid_names = {}
    for line in (_read("/etc/passwd") or b"").decode(errors="replace").splitlines():
        parts = line.split(":")
        if len(parts) > 2:
            uid_names[parts[2]] = parts[0]
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        stat = _read(f"/proc/{entry}/stat")
        if stat is None:
            continue
        text = stat.decode(errors="replace")
        fields = text[text.rfind(")") + 2 :].split()
        state, ppid, pgid, start_ticks = fields[0], int(fields[1]), int(fields[2]), int(fields[19])
        cmd = (
            (_read(f"/proc/{entry}/cmdline") or b"")
            .replace(b"\0", b" ")
            .decode(errors="replace")
            .strip()
        )
        status = (_read(f"/proc/{entry}/status") or b"").decode(errors="replace")
        uid = re.search(r"^Uid:\s+(\d+)", status, re.M)
        procs[int(entry)] = Proc(
            int(entry),
            ppid,
            pgid,
            int(uptime - start_ticks / ticks),
            state,
            uid_names.get(uid.group(1), uid.group(1)) if uid else "?",
            cmd,
            str(start_ticks),
        )
    for line in _run(["systemctl", "show", "--property=MainPID", "*"]).splitlines():
        _mark_supervised(procs, line)
    for line in _run(["systemctl", "--user", "show", "--property=MainPID", "*"]).splitlines():
        _mark_supervised(procs, line)
    return procs


def _mark_supervised(procs: dict[int, Proc], line: str) -> None:
    value = line.partition("=")[2]
    if value.isdigit() and int(value) in procs:
        procs[int(value)].supervisor = "job"


def _enrich_linux(procs: dict[int, Proc], pids: list[int]) -> None:
    for pid in pids:
        raw = _read(f"/proc/{pid}/environ") or b""
        pairs = [
            tuple(kv.split("=", 1)) for kv in raw.decode(errors="replace").split("\0") if "=" in kv
        ]
        procs[pid].env, procs[pid].env_markers = _filter_env(pairs)  # type: ignore[arg-type]
        try:
            procs[pid].cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            procs[pid].cwd = "?"


def snapshot() -> dict[int, Proc]:
    """Every process on the host, with env and cwd for the ones that matter."""
    if sys.platform == "darwin":
        procs, enrich = _snapshot_darwin(), _enrich_darwin
    elif sys.platform.startswith("linux"):
        procs, enrich = _snapshot_linux(), _enrich_linux
    else:
        raise SystemExit(f"[ERROR] unsupported platform {sys.platform}")
    if not procs:
        raise SystemExit(
            "[ERROR] process snapshot is empty: the instrument failed, not a clean host"
        )
    # Env is needed for candidates AND for every ancestor up to the tree
    # root, because attribution reads the root's environment too.
    wanted = set()
    for pid, proc in procs.items():
        if _looks_test_spawned(proc):
            wanted.update(_ancestry(procs, pid))
    enrich(procs, sorted(p for p in wanted if p in procs))
    return procs


# ---------------------------------------------------------------- classification (functional core)


def _looks_test_spawned(proc: Proc) -> bool:
    return bool(CFG.TEST_COMMAND_RE.search(proc.command)) and not CFG.EXCLUDED_COMMAND_RE.search(
        proc.command
    )


def _ancestry(procs: dict[int, Proc], pid: int) -> list[int]:
    """pid, its parent, ... up to (not including) init. Cycle-safe."""
    chain: list[int] = []
    while pid in procs and pid not in chain and pid > 1:
        chain.append(pid)
        pid = procs[pid].ppid
    return chain


def _subreapers(procs: dict[int, Proc]) -> set:
    """Linux `systemd --user` instances adopt orphans in place of pid 1."""
    return {p.pid for p in procs.values() if re.search(r"(^|/)systemd --user", p.command)}


def classify(procs: dict[int, Proc]) -> list[Row]:
    reapers = _subreapers(procs) | {1}
    rows: list[Row] = []
    for pid, proc in sorted(procs.items()):
        in_scope = _looks_test_spawned(proc) or CFG.TEST_SERVICE_MARKER in proc.env.get(
            "AF_SERVICE_ID", ""
        )
        if not in_scope:
            continue
        chain = _ancestry(procs, pid)
        root_pid = next((p for p in chain if procs[p].ppid in reapers), chain[-1] if chain else pid)
        root = procs.get(root_pid, proc)
        tree = _tree_kind(procs, chain, root)
        attribution = _attribution(procs, chain)
        rows.append(
            Row(
                pid=pid,
                ppid=proc.ppid,
                pgid=proc.pgid,
                root_pid=root_pid,
                root_command=root.command[:120],
                age_s=proc.age_s,
                state=proc.state,
                cwd=proc.cwd,
                command=proc.command[:240],
                env=proc.env,
                env_markers=proc.env_markers,
                tree=tree,
                attribution=attribution,
                verdict=_verdict(proc, tree, attribution),
            )
        )
    return rows


def _tree_kind(procs: dict[int, Proc], chain: list[int], root: Proc) -> str:
    supervisors = [procs[p].supervisor for p in chain if procs[p].supervisor]
    if supervisors:
        return "supervised-" + supervisors[-1]  # the outermost supervisor owns the tree
    if any(CFG.SESSION_HOST_RE.search(procs[p].command) for p in chain):
        return "session"
    if root.ppid in (_subreapers(procs) | {1}):
        return "orphaned"
    return "attached"


def _attribution(procs: dict[int, Proc], chain: list[int]) -> str:
    """Who started this tree, read from the env it inherited (survives reparenting).

    AF_SERVICE_ID is inherited by every descendant, so on its own it names the
    OUTERMOST service, not this process: a pytest run inside a Claude session
    carries the Claude profile's id. Precedence therefore runs from the most
    specific marker to the least.
    """
    envs = [procs[p].env for p in chain]
    service_ids = [e.get("AF_SERVICE_ID", "") for e in envs if e.get("AF_SERVICE_ID")]
    if any(CFG.TEST_SERVICE_MARKER in s for s in service_ids):
        return "test-harness"
    if any(k in e for e in envs for k in CFG.RUNNER_ENV_KEYS) or any(
        CFG.RUNNER_WORK_RE.search(procs[p].cwd) for p in chain
    ):
        return "runner"
    if (
        any(k in e for e in envs for k in CFG.AGENT_ENV_KEYS)
        or any(procs[p].env_markers for p in chain)
        or any(CFG.AGENT_SERVICE_ID_RE.search(s) for s in service_ids)
        or any(CFG.WORKTREE_CWD_RE.search(procs[p].cwd) for p in chain)
    ):
        return "agent"
    if service_ids:
        return "service"
    return "unknown"


def _verdict(proc: Proc, tree: str, attribution: str) -> str:
    """reapable only for an ORPHANED tree with positive test/runner/agent provenance."""
    if proc.state.startswith("Z"):
        return "zombie"
    if tree == "orphaned" and attribution in ("runner", "agent", "test-harness"):
        return "reapable"
    if tree == "orphaned" and attribution == "service":
        return "service"  # a non-test AF_SERVICE_ID that daemonized on purpose
    if tree == "orphaned":
        return "unattributed-orphan"
    if tree == "supervised-job" and attribution != "runner":
        return "service"  # a launchd job or systemd unit and its children: long-lived by design
    return "active"


# ---------------------------------------------------------------- reap


def _utc_now() -> str:
    # gmtime is UTC by construction, so the Z is true; datetime.UTC would
    # need Python 3.11 and this file must run on any fleet host's python3.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _same_process(pid: int, start: str) -> bool:
    fresh = snapshot_one(pid)
    return fresh is not None and fresh.start == start and not fresh.state.startswith("Z")


def snapshot_one(pid: int) -> Proc | None:
    if sys.platform.startswith("linux"):
        stat = _read(f"/proc/{pid}/stat")
        if stat is None:
            return None
        text = stat.decode(errors="replace")
        fields = text[text.rfind(")") + 2 :].split()
        return Proc(pid, int(fields[1]), int(fields[2]), 0, fields[0], "", "", str(int(fields[19])))
    out = _run(["ps", "-o", "stat=,lstart=", "-p", str(pid)]).strip()
    if not out:
        return None
    state, _, start = out.partition(" ")
    return Proc(pid, 0, 0, 0, state, "", "", start.strip())


def reap(
    procs: dict[int, Proc],
    rows: list[Row],
    *,
    min_age_s: int,
    dry_run: bool,
    only_pids: list[int] | None = None,
) -> dict:
    targets = [
        r
        for r in rows
        if r.verdict == "reapable"
        and r.age_s >= min_age_s
        and (not only_pids or r.pid in only_pids)
    ]
    identities = {r.pid: procs[r.pid].start for r in targets}
    killed: list[dict] = []
    survivors: list[int] = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = [r for r in targets if _same_process(r.pid, identities[r.pid])]
        if sig == signal.SIGKILL and not alive:
            break
        for row in alive:
            if dry_run:
                continue
            try:
                os.kill(row.pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError:
                survivors.append(row.pid)
        if dry_run:
            break
        time.sleep(CFG.TERM_GRACE_S if sig == signal.SIGTERM else 0.5)
    for row in targets:
        still = (not dry_run) and _same_process(row.pid, identities[row.pid])
        if still:
            survivors.append(row.pid)
        killed.append(
            {
                "pid": row.pid,
                "attribution": row.attribution,
                "root_pid": row.root_pid,
                "age_s": row.age_s,
                "cwd": row.cwd,
                "command": row.command[:160],
                "env": row.env,
                "outcome": "would-kill" if dry_run else ("SURVIVED" if still else "killed"),
            }
        )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.verdict] = counts.get(row.verdict, 0) + 1
    return {
        "at": _utc_now(),
        "host": os.uname().nodename,
        "dry_run": dry_run,
        "in_scope": len(rows),
        "verdicts": counts,
        "reapable": len(targets),
        "killed": sum(1 for k in killed if k["outcome"] == "killed"),
        "survived": sorted(set(survivors)),
        "kills": killed,
    }


# ---------------------------------------------------------------- cli


def zombies_by_parent(procs: dict[int, Proc]) -> dict[str, int]:
    """Zombies cannot be killed; only their parent can reap them, so name it."""
    counts: dict[str, int] = {}
    for proc in procs.values():
        if proc.state.startswith("Z"):
            parent = procs.get(proc.ppid)
            key = f"{proc.ppid} {parent.command[:60] if parent else '?'}"
            counts[key] = counts.get(key, 0) + 1
    return counts


def _render_table(rows: list[Row]) -> str:
    head = (
        f"{'pid':>7} {'ppid':>7} {'root':>7} {'age':>8} {'verdict':<20} {'attrib':<12} "
        f"{'tree':<14} {'AF_SERVICE_ID':<34} command | cwd"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        age = f"{r.age_s // 3600}h{(r.age_s % 3600) // 60:02d}m"
        lines.append(
            f"{r.pid:>7} {r.ppid:>7} {r.root_pid:>7} {age:>8} {r.verdict:<20} "
            f"{r.attribution:<12} {r.tree:<14} "
            f"{r.env.get('AF_SERVICE_ID', '-'):<34} {r.command[:90]} | {r.cwd}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    census = sub.add_parser("census", help="measure; never kills")
    census.add_argument("--json", action="store_true")
    reaper = sub.add_parser("reap", help="kill orphaned, attributed test leftovers")
    reaper.add_argument("--dry-run", action="store_true")
    reaper.add_argument("--min-age-s", type=int, default=CFG.DEFAULT_MIN_AGE_S)
    reaper.add_argument("--report", type=Path, help="append the JSON report line here")
    reaper.add_argument(
        "--only-pid",
        type=int,
        action="append",
        default=[],
        help="restrict kills to these pids (repeatable)",
    )
    args = parser.parse_args(argv)

    procs = snapshot()
    rows = classify(procs)
    if args.cmd == "census":
        if args.json:
            print(
                json.dumps(
                    {
                        "at": _utc_now(),
                        "host": os.uname().nodename,
                        "total_procs": len(procs),
                        "zombies_by_parent": zombies_by_parent(procs),
                        "rows": [asdict(r) for r in rows],
                    }
                )
            )
        else:
            zombies = zombies_by_parent(procs)
            print(
                f"[census] host={os.uname().nodename} at={_utc_now()} total_procs={len(procs)} "
                f"in_scope={len(rows)} zombies={sum(zombies.values())} {zombies}"
            )
            print(_render_table(rows))
        return 0
    report = reap(
        procs, rows, min_age_s=args.min_age_s, dry_run=args.dry_run, only_pids=args.only_pid
    )
    line = json.dumps(report)
    print(line)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("a") as fh:
            fh.write(line + "\n")
    return 1 if report["survived"] else 0


if __name__ == "__main__":
    sys.exit(main())
