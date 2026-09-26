"""Fast, scoped pre-PR gate: the commands a build factory runs before it opens a PR.

The repository owns these commands; the factory only calls them (OPS-44, and
docs/decisions/ADR-NEW-repo-owns-pre-pr-gate-commands.md). IDD's SSSF quality phase
reads `[tool.sssf.quality]` in pyproject.toml, runs one block per call and reads the
exit code. GitHub CI stays the authoritative gate. This is the cheap catch
that runs first, on the files the change touched, in well under five minutes.

Blocks, each scoped to the change (committed, staged, unstaged and untracked files since the
merge base with `--base`, minus the factory scaffolding stamped into the worktree):

    lint       pinned ruff (ops/quality/requirements.txt) on changed Python files; FAIL only
               on a rule whose count rose against the merge base, the same delta the
               quality ratchet scores, so pre-existing debt in a touched file is not the
               builder's to fix.
    typecheck  svelte-check (`pnpm run check`) when a frontend file changed.
    build      the frontend production build (`pnpm run build`) when a frontend file changed.
    test       affected pytest modules (scripts/affected_tests.py, the static import graph)
               plus any changed frontend unit test file. Never the full suite.

Exit codes: 0 PASS, 1 FAIL, 3 UNMEASURED. UNMEASURED covers a block with nothing to check in
this change, a selection of zero tests, a selection over the time budget, and a tool that ran
but could not produce a verdict. It is never a pass: a gate that selected nothing says so.

Usage:
    uv run --extra dev python -m scripts.pre_pr_gate test [--base origin/main]

Requirements (statuses: ? todo, done, done+ran, done+ran+tests):
  - done+ran+tests: changed-file scope includes uncommitted and untracked work, excludes adws/.
      [if] the builder's edit is uncommitted [then] it is in scope [then ⛔️]
      [if] an untracked new file exists [then] it is in scope [then ⛔️]
      [if] only factory scaffolding under adws/ is untracked [then] nothing is in scope [then ⛔️]
  - done+ran+tests: zero selection never passes.
      [if] no test module reaches the change [then] exit 3 and say 0 tests selected [then ⛔️]
      [if] every selected test is skipped [then] exit 3, not 0 [then ⛔️]
      [if] a block has nothing to check [then] exit 3 naming why [then ⛔️]
  - done+ran+tests: lint scores the delta, not the debt.
      [if] a touched file already had violations and gained none [then] PASS [then ⛔️]
      [if] a rule count rises in a changed file [then] FAIL naming the new finding [then ⛔️]
      [if] ruff output cannot be parsed [then] exit 3 [then ⛔️]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts.affected_tests import UnresolvedPaths, affected_tests

REPO = Path(__file__).resolve().parents[1]

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNMEASURED = 3

Status = Literal["PASS", "FAIL", "UNMEASURED"]
EXIT_OF: dict[Status, int] = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "UNMEASURED": EXIT_UNMEASURED}


class CFG:
    BASE = "origin/main"
    #: Stamped into every IDD worktree by the SSSF installer, untracked, not repository source.
    FACTORY_SCAFFOLD_PREFIXES = ("adws/",)
    FRONTEND = "apps/webui/frontend"
    FRONTEND_UNIT_TEST = re.compile(r"^apps/webui/frontend/tests/unit/(slow/)?[^/]+\.test\.mjs$")
    RUFF_REQUIREMENTS = "ops/quality/requirements.txt"
    DURATIONS_LEDGER = ".test_durations"
    #: Ledger seconds (CI-runner speed) past which the affected selection is not run pre-PR.
    #: Measured Sat 26 Sep 2026 on nucbox: see the ADR for the ratio behind this number.
    PYTEST_BUDGET_LEDGER_SECONDS = 600.0
    PYTEST_WORKERS = "4"
    PYTEST_TIMEOUT_SECONDS = "120"
    #: The pytest environment CI builds (ci.yml "Python deps" step), so a pre-PR red is a red
    #: CI would also see. `uv sync --extra dev` is NOT that environment: measured Sat 26 Sep
    #: 2026 on nucbox, 6 affected tests failed on a clean main under it and passed under this.
    CI_PYTHON = "3.11"
    CI_REQUIREMENTS = "requirements.txt"
    CI_EXTRA_PACKAGES = ("modal", "sentry-sdk>=2.0,<3")


@dataclass(frozen=True)
class Verdict:
    status: Status
    reason: str


# ----- change scope ------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def changed_paths(merge_base: str) -> list[str]:
    """Every path the change touches since the merge base, including uncommitted work.

    An IDD builder's edits are uncommitted while this runs, so `merge-base..HEAD` alone would
    see nothing. `git diff <merge-base>` compares the WORKING TREE, and `ls-files --others`
    adds new files git has not been told about. `--no-renames` lists a rename's old path too.
    """
    tracked = _git("diff", "--name-only", "--no-renames", merge_base).splitlines()
    untracked = _git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(
        {p for p in tracked + untracked if p and not p.startswith(CFG.FACTORY_SCAFFOLD_PREFIXES)}
    )


def _existing(paths: list[str]) -> list[str]:
    return [p for p in paths if (REPO / p).is_file()]


def _python(paths: list[str]) -> list[str]:
    return [p for p in paths if p.endswith(".py")]


def _frontend(paths: list[str]) -> list[str]:
    return [p for p in paths if p.startswith(CFG.FRONTEND + "/")]


def _run(
    argv: list[str], cwd: Path | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a tool with its output visible to the caller; the verdict reads it, never a guess."""
    return subprocess.run(
        argv, cwd=cwd or REPO, input=stdin, capture_output=True, text=True, check=False
    )


def _echo(completed: subprocess.CompletedProcess[str]) -> None:
    sys.stdout.write(completed.stdout)
    sys.stdout.write(completed.stderr)


# ----- lint ----------------------------------------------------------------------


class Lint:
    @staticmethod
    def _ruff(paths: list[str], stdin: str | None = None) -> list[dict] | None:
        """Pinned ruff findings as JSON, or None when ruff could not produce any."""
        argv = [
            "uv",
            "run",
            "--no-project",
            "--quiet",
            "--with-requirements",
            CFG.RUFF_REQUIREMENTS,
            "ruff",
            "check",
            "--output-format",
            "json",
            "--no-cache",
            "--exit-zero",
        ]
        argv += ["--stdin-filename", paths[0], "-"] if stdin is not None else paths
        completed = _run(argv, stdin=stdin)
        try:
            findings = json.loads(completed.stdout) if completed.returncode == 0 else None
        except json.JSONDecodeError:
            findings = None
        if findings is None:
            _echo(completed)
        return findings

    @staticmethod
    def _key(finding: dict) -> tuple[str, str]:
        """(repo-relative path, rule code): ruff reports absolute paths, even for stdin."""
        path = Path(finding["filename"]).resolve()
        return str(path.relative_to(REPO.resolve())), finding.get("code") or "UNKNOWN"

    @staticmethod
    def run(changed: list[str], merge_base: str) -> Verdict:
        files = _existing(_python(changed))
        if not files:
            return Verdict("UNMEASURED", "N/A: no changed Python file to lint")
        head = Lint._ruff(files)
        if head is None:
            return Verdict("UNMEASURED", "ruff produced no parseable findings at HEAD")
        base: collections.Counter[tuple[str, str]] = collections.Counter()
        for path in files:
            shown = _run(["git", "show", f"{merge_base}:{path}"])
            if shown.returncode != 0:
                continue  # new file: nothing at the base, so its base count is zero
            findings = Lint._ruff([path], stdin=shown.stdout)
            if findings is None:
                return Verdict(
                    "UNMEASURED", f"ruff produced no parseable findings for {path} at base"
                )
            base.update(Lint._key(f) for f in findings)
        now = collections.Counter(Lint._key(f) for f in head)
        risen = sorted(key for key, n in now.items() if n > base[key])
        for finding in head:
            if Lint._key(finding) in risen:
                where = finding["location"]
                print(
                    f"{Lint._key(finding)[0]}:{where['row']}:{where['column']}: "
                    f"{finding.get('code')} {finding['message']}"
                )
        if risen:
            summary = ", ".join(f"{p} {c} {base[(p, c)]}->{now[(p, c)]}" for p, c in risen)
            return Verdict("FAIL", f"new ruff violations in changed files: {summary}")
        return Verdict(
            "PASS",
            f"ruff on {len(files)} changed Python file(s): no rule count rose "
            f"(head {sum(now.values())}, base {sum(base.values())})",
        )


# ----- frontend ------------------------------------------------------------------


class Front:
    @staticmethod
    def root() -> Path:
        return REPO / CFG.FRONTEND

    @staticmethod
    def install() -> Verdict | None:
        """node_modules for this worktree, from the local store; None when ready."""
        completed = _run(
            ["pnpm", "install", "--frozen-lockfile", "--prefer-offline", "--reporter=silent"],
            cwd=Front.root(),
        )
        if completed.returncode != 0:
            _echo(completed)
            return Verdict(
                "UNMEASURED", f"pnpm install exited {completed.returncode}; frontend not measurable"
            )
        return None

    @staticmethod
    def typecheck(changed: list[str]) -> Verdict:
        if not _frontend(changed):
            return Verdict("UNMEASURED", "N/A: no changed frontend file to typecheck")
        if (blocked := Front.install()) is not None:
            return blocked
        completed = _run(["pnpm", "run", "check"], cwd=Front.root())
        _echo(completed)
        return Front.read_svelte_check(completed.returncode, completed.stdout + completed.stderr)

    @staticmethod
    def read_svelte_check(returncode: int, output: str) -> Verdict:
        """PASS needs the "found 0 errors" summary AND exit 0; a missing summary is no verdict."""
        found = re.search(r"svelte-check found (\d+) errors?", output)
        if found is None:
            return Verdict("UNMEASURED", f"svelte-check printed no summary (exit {returncode})")
        errors = int(found.group(1))
        if errors:
            return Verdict("FAIL", f"svelte-check found {errors} error(s)")
        if returncode != 0:
            return Verdict("UNMEASURED", f"svelte-check reported 0 errors but exited {returncode}")
        return Verdict("PASS", "svelte-check found 0 errors")

    @staticmethod
    def build(changed: list[str]) -> Verdict:
        if not _frontend(changed):
            return Verdict("UNMEASURED", "N/A: no changed frontend file to build")
        if (blocked := Front.install()) is not None:
            return blocked
        completed = _run(["pnpm", "run", "build"], cwd=Front.root())
        _echo(completed)
        if completed.returncode != 0:
            return Verdict("FAIL", f"frontend build exited {completed.returncode}")
        if not (Front.root() / "build" / "index.html").is_file():
            return Verdict("UNMEASURED", "frontend build exited 0 but wrote no build/index.html")
        return Verdict("PASS", "frontend build wrote build/index.html")

    @staticmethod
    def unit_tests(files: list[str]) -> Verdict:
        if (blocked := Front.install()) is not None:
            return blocked
        sync = _run(["pnpm", "exec", "svelte-kit", "sync"], cwd=Front.root())
        if sync.returncode != 0:
            _echo(sync)
            return Verdict("UNMEASURED", f"svelte-kit sync exited {sync.returncode}")
        relative = [str(Path(f).relative_to(CFG.FRONTEND)) for f in files]
        completed = _run(
            [
                "node",
                "--test",
                "--test-reporter=tap",
                "--experimental-strip-types",
                "--test-concurrency=4",
                "--test-timeout=120000",
                *relative,
            ],
            cwd=Front.root(),
        )
        _echo(completed)
        return Front.read_node_tap(completed.returncode, completed.stdout, len(files))

    @staticmethod
    def read_node_tap(returncode: int, stdout: str, files: int) -> Verdict:
        """A verdict from the node test runner's TAP tally; no tally, or 0 passed, is none."""
        tally = {k: int(v) for k, v in re.findall(r"^# (pass|fail) (\d+)$", stdout, re.MULTILINE)}
        if "pass" not in tally or "fail" not in tally:
            return Verdict(
                "UNMEASURED",
                f"the node test runner printed no TAP pass/fail tally (exit {returncode})",
            )
        if tally["fail"] or returncode != 0:
            return Verdict("FAIL", f"frontend unit: {tally['fail']} failed of {files} file(s)")
        if not tally["pass"]:
            return Verdict("UNMEASURED", f"frontend unit: 0 passed in {files} file(s)")
        return Verdict("PASS", f"frontend unit: {tally['pass']} passed in {files} changed file(s)")


# ----- test ----------------------------------------------------------------------


class Tests:
    @staticmethod
    def venv_python() -> Path:
        return REPO / ".venv" / "bin" / "python"

    @staticmethod
    def ledger_seconds(selection: list[str]) -> float:
        """CI-recorded seconds for the selected modules; a module the ledger lacks adds 0."""
        ledger = json.loads((REPO / CFG.DURATIONS_LEDGER).read_text(encoding="utf-8"))
        chosen = set(selection)
        return sum(s for node, s in ledger.items() if node.split("::", 1)[0] in chosen)

    @staticmethod
    def provision() -> Verdict | None:
        """Build CI's pytest environment in this worktree's .venv; None when ready."""
        steps = (
            ["scripts/ci_venv.sh", CFG.CI_PYTHON],
            [
                "uv",
                "pip",
                "install",
                "--exact",
                "--upgrade",
                "--quiet",
                "--python",
                str(Tests.venv_python()),
                "-r",
                CFG.CI_REQUIREMENTS,
                *CFG.CI_EXTRA_PACKAGES,
            ],
        )
        for argv in steps:
            completed = _run(argv)
            if completed.returncode != 0:
                _echo(completed)
                return Verdict(
                    "UNMEASURED",
                    f"`{' '.join(argv[:3])}` exited {completed.returncode}; no pytest environment",
                )
        return None

    @staticmethod
    def pytest(selection: list[str]) -> Verdict:
        estimate = Tests.ledger_seconds(selection)
        if estimate > CFG.PYTEST_BUDGET_LEDGER_SECONDS:
            return Verdict(
                "UNMEASURED",
                f"pytest: {len(selection)} affected module(s) record {estimate:.0f}s in "
                f"{CFG.DURATIONS_LEDGER}, over the {CFG.PYTEST_BUDGET_LEDGER_SECONDS:.0f}s "
                "pre-PR budget; CI runs them",
            )
        if (blocked := Tests.provision()) is not None:
            return blocked
        argv = [
            str(Tests.venv_python()),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--timeout",
            CFG.PYTEST_TIMEOUT_SECONDS,
        ]
        if len(selection) > 1:
            argv += ["-n", CFG.PYTEST_WORKERS, "--dist", "loadfile"]
        completed = _run([*argv, *selection])
        _echo(completed)
        return Tests.read_pytest(completed.returncode, completed.stdout, len(selection))

    @staticmethod
    def read_pytest(returncode: int, stdout: str, modules: int) -> Verdict:
        """A verdict from pytest's own exit code and summary line, never from either alone."""
        summary = (stdout.strip().splitlines() or [""])[-1].strip("= ")
        counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|errors?)\b", summary)}
        if returncode == 1 or counts.get("failed") or counts.get("error") or counts.get("errors"):
            return Verdict("FAIL", f"pytest: {summary}")
        if returncode == 5:
            return Verdict("UNMEASURED", "pytest collected 0 tests from the affected selection")
        if returncode != 0:
            return Verdict("UNMEASURED", f"pytest exited {returncode} without a verdict")
        if not counts.get("passed"):
            return Verdict("UNMEASURED", f"pytest: 0 passed ({summary})")
        return Verdict("PASS", f"pytest: {summary} across {modules} affected module(s)")

    @staticmethod
    def run(changed: list[str]) -> Verdict:
        python_changed = _python(changed)
        try:
            selection = affected_tests(python_changed, root=REPO) if python_changed else []
        except UnresolvedPaths as unresolved:
            return Verdict(
                "UNMEASURED",
                f"affected tests unknown: {len(unresolved.paths)} changed Python path(s) are not "
                f"modules in the import graph: {', '.join(unresolved.paths)}",
            )
        frontend_tests = _existing([p for p in changed if CFG.FRONTEND_UNIT_TEST.match(p)])
        if not selection and not frontend_tests:
            return Verdict(
                "UNMEASURED",
                f"0 tests selected: {len(python_changed)} changed Python file(s) reach no test "
                "module and no frontend unit test file changed",
            )
        print(f"# selected: {len(selection)} pytest module(s), {len(frontend_tests)} frontend unit")
        verdicts = []
        if selection:
            verdicts.append(Tests.pytest(selection))
        if frontend_tests:
            verdicts.append(Front.unit_tests(frontend_tests))
        return combine(verdicts)


def combine(verdicts: list[Verdict]) -> Verdict:
    """FAIL beats UNMEASURED beats PASS: a part that did not run is not carried by one that did."""
    reason = "; ".join(v.reason for v in verdicts)
    for status in ("FAIL", "UNMEASURED"):
        if any(v.status == status for v in verdicts):
            return Verdict(status, reason)
    return Verdict("PASS", reason)


# ----- entry point ---------------------------------------------------------------

BLOCKS: dict[str, Callable[[list[str], str], Verdict]] = {
    "lint": Lint.run,
    "typecheck": lambda changed, _merge_base: Front.typecheck(changed),
    "build": lambda changed, _merge_base: Front.build(changed),
    "test": lambda changed, _merge_base: Tests.run(changed),
}


def run_block(block: str, base: str) -> Verdict:
    try:
        merge_base = _git("merge-base", "HEAD", base).strip()
        changed = changed_paths(merge_base)
    except subprocess.CalledProcessError as error:
        return Verdict(
            "UNMEASURED", f"cannot scope the change against {base}: {error.stderr.strip()}"
        )
    print(f"# pre-pr-gate {block}: {len(changed)} changed path(s) since {merge_base[:12]} ({base})")
    return BLOCKS[block](changed, merge_base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("block", choices=BLOCKS)
    parser.add_argument(
        "--base", default=CFG.BASE, help=f"ref the change is scoped against (default {CFG.BASE})"
    )
    arguments = parser.parse_args(argv)
    verdict = run_block(arguments.block, arguments.base)
    print(f"pre-pr-gate {arguments.block}: {verdict.status}: {verdict.reason}")
    return EXIT_OF[verdict.status]


if __name__ == "__main__":
    sys.exit(main())
