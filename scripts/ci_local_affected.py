"""Run, before pushing, the same pytest selection a pull request head will run in CI.

Agents pushed and then waited on the five pytest shards to learn what a local run could have
told them in a minute. This asks the PR-head selector (`scripts.ci_test_selection_rules`,
DEVOPS-20) the same question CI asks, against this checkout's diff from the merge base with
`--base` (default `origin/main`), uncommitted and untracked files included, and with the shard
lane's own `--ignore` list read from `.github/workflows/ci.yml` so the two cannot drift.

    python -m scripts.ci_local_affected            # run the selection with pytest
    python -m scripts.ci_local_affected --list     # print it, run nothing
    python -m scripts.ci_local_affected -- -x -k foo   # extra pytest arguments after --

Exit codes: 0 when the selected tests passed, the selection is explicitly empty, or pytest
collected nothing (exit 5) and its JUnit report proves every selected module skipped itself at
module level, the one exit 5 CI's shard lane also passes; 1 when pytest failed for any
reason (its own code is printed, so a pytest 2 or 3 never reads as ours); 2 when the change
cannot be narrowed (FullSuite: the reason is printed, and CI will run everything too, so run
`just fast-tier` locally rather than the whole suite); 3 when the selector could not measure,
or pytest-timeout is missing, so a hung test can never run unbounded.
Never an empty run for a diff it could not read: an unreadable diff is exit 3.

This only runs tests EARLIER. CI's own selection, the merge queue draft and main still run
what they run today.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil

from scripts.affected_tests import REPO
from scripts.ci_test_selection import junit_all_skipped
from scripts.ci_test_selection_rules import FullSuite, select_tests

WORKFLOW = Path(".github/workflows/ci.yml")
RUN_DEADLINE_S = 3600
_BLOCK = re.compile(r"^\s*LANE_IGNORES=\(\n(?P<body>(?:\s*--ignore=\S+\n)+)\s*\)", re.MULTILINE)


class Unmeasured(Exception):
    """The selection could not be computed; never read as an empty one."""


def lane_ignores(workflow_text: str) -> list[str]:
    """The pytest shard lane's `--ignore=` arguments, from the LANE_IGNORES array in ci.yml."""
    match = _BLOCK.search(workflow_text)
    if match is None:
        raise Unmeasured(f"no LANE_IGNORES=( ... ) block of --ignore= lines in {WORKFLOW}")
    return [line.strip() for line in match.group("body").splitlines() if line.strip()]


@functools.cache
def _git_exe() -> str:
    """git by resolved path, once (AGENTS.md: launch programs by resolved path)."""
    git = shutil.which("git")
    if git is None:
        raise Unmeasured("git is not on PATH: the diff cannot be read")
    return git


def _git(root: Path, *args: str, nul: bool = False) -> list[str]:
    """Run git; with `nul` the output is -z NUL-delimited, so a path is never C-quoted or split on a newline."""
    done = subprocess.run([_git_exe(), *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)
    if done.returncode != 0:
        raise Unmeasured(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return [entry for entry in (done.stdout.split("\0") if nul else done.stdout.splitlines()) if entry]


def changed_paths(root: Path, base: str) -> list[str]:
    """Committed, uncommitted and untracked paths since the merge base with `base`."""
    merge_base = _git(root, "merge-base", base, "HEAD")[0]
    if merge_base != _git(root, "rev-parse", f"{base}^{{commit}}")[0]:
        # CI selects on the merge with the base; an unmerged tree would miss base-side tests of changed code.
        raise Unmeasured(f"{base} has commits HEAD lacks: merge it first so the selection matches CI")
    paths = set(_git(root, "diff", "--name-only", "-z", "--no-renames", merge_base, nul=True))
    paths.update(_git(root, "ls-files", "-z", "--others", "--exclude-standard", nul=True))
    if not paths:
        raise Unmeasured(f"no change since {base} ({merge_base[:9]}): nothing to select from")
    return sorted(paths)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1 :]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--list", action="store_true", help="print the selection, run nothing")
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    parser.add_argument("--deadline-s", type=float, default=RUN_DEADLINE_S, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root: Path = args.root
    try:
        ignores = lane_ignores((root / WORKFLOW).read_text(encoding="utf-8"))
        changed = changed_paths(root, args.base)
        selection = select_tests(changed, [i.removeprefix("--ignore=") for i in ignores], root)
    except FullSuite as full:
        print(f"[FULL] this change cannot be narrowed: {full}", file=sys.stderr)
        print("[FULL] CI will run every shard too; locally run `just fast-tier`.", file=sys.stderr)
        return 2
    except Unmeasured as err:
        print(f"[UNKNOWN] {err}", file=sys.stderr)
        return 3
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as err:
        print(f"[UNKNOWN] the selector could not read the tree: {type(err).__name__}: {err}", file=sys.stderr)
        return 3
    print(f"[selection] {len(changed)} changed paths -> {len(selection.modules)} test modules", file=sys.stderr)
    if args.list:
        for module in selection.modules:
            print(f"{module}\t{'; '.join(selection.reasons[module])}")
        return 0
    if not selection.modules:
        print("[selection] explicitly empty: no test module is reached by this change", file=sys.stderr)
        return 0
    parallel = ["-n", "auto"] if importlib.util.find_spec("xdist") else []
    if importlib.util.find_spec("pytest_timeout") is None:
        print("[UNKNOWN] pytest-timeout is not installed; run `uv sync --extra dev`", file=sys.stderr)
        return 3
    with tempfile.TemporaryDirectory() as scratch:
        report = Path(scratch) / "junit.xml"
        command = [sys.executable, "-m", "pytest", "-q", *parallel, "--timeout=900", f"--junitxml={report}"]
        code = _run_within([*command, *selection.modules, *extra], cwd=root, deadline_s=args.deadline_s)
        if code is None:
            # --timeout bounds each test, not import or collection; this bounds the whole run.
            print(
                f"[FAIL] pytest did not finish within {args.deadline_s:g} s; killed it and its workers", file=sys.stderr
            )
            return 1
        return pytest_verdict(code, report, selection.modules)


def _run_within(command: list[str], cwd: Path, deadline_s: float) -> int | None:
    """Run `command`; at the deadline kill it AND every descendant (xdist workers), return None.

    Killing only the controller would orphan a worker hung in collection. psutil walks the tree
    the same way on macOS, Linux and Windows, so no POSIX process-group seam is needed."""
    child = subprocess.Popen(command, cwd=cwd)
    try:
        return child.wait(timeout=deadline_s)
    except subprocess.TimeoutExpired:
        tree = _freeze_tree(child.pid)
        for proc in tree:
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.kill()
        # Bounded: a controller that ignores the kill must not hold this command past the deadline.
        _, alive = psutil.wait_procs(tree, timeout=10)
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=1)
        if alive:
            pids = ", ".join(str(p.pid) for p in alive)
            print(f"[ERROR] pytest processes survived the kill: {pids}", file=sys.stderr)
        return None


def _freeze_tree(root_pid: int, rounds: int = 20) -> list[psutil.Process]:
    """Suspend `root_pid` and every descendant until a rescan finds no new process, then return them.

    A suspended process cannot spawn, so once a rescan is stable no worker can appear after the
    snapshot and escape the kill. suspend() is SIGSTOP on POSIX and NtSuspendProcess on Windows."""
    try:
        frozen = {root_pid: psutil.Process(root_pid)}
    except psutil.NoSuchProcess:
        return []
    for _ in range(rounds):
        found: dict[int, psutil.Process] = {}
        for proc in list(frozen.values()):
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.suspend()
                found.update({kid.pid: kid for kid in proc.children(recursive=True)})
        new = {pid: proc for pid, proc in found.items() if pid not in frozen}
        if not new:
            return list(frozen.values())
        frozen.update(new)
    print(f"[ERROR] pytest kept spawning after {rounds} suspend rounds; killing what was found", file=sys.stderr)
    return list(frozen.values())


def pytest_verdict(code: int, report: Path, modules: list[str]) -> int:
    """Map pytest's exit code onto this runner's: 0 pass, 1 fail. 2 and 3 stay ours.

    Exit 5 passes only on the JUnit evidence CI's shard lane requires: every selected module
    skipped itself at module level. A module with no tests, or a -k that matched nothing, fails."""
    if code == 0:
        return 0
    if code == 5:
        if report.is_file() and junit_all_skipped(report, modules):
            print("[selection] exit 5, every selected module skipped at module level: success", file=sys.stderr)
            return 0
        print("[FAIL] pytest collected nothing (exit 5) and not every module skipped itself", file=sys.stderr)
        return 1
    print(f"[FAIL] pytest exited {code}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
