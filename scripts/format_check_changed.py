"""Check the files a branch changes against the ratified formatters, sparing the unformatted backlog (issue #4456).

    python -m scripts.format_check_changed [--base origin/main] [--write]
    just fmt-changed [--base REV] [--write]

The CI job "format check (changed files)" runs this on every pull request (advisory, see
(private path) part 9). It reads the diff from the merge-base of HEAD and --base to HEAD
and checks only the files the branch adds, modifies, renames or copies:

  - Python (.py, .pyi) with ruff from ops/quality/requirements.txt, `ruff format --check --force-exclude`,
    so the [tool.ruff.format] exclusions in pyproject.toml hold;
  - frontend sources under apps/webui/frontend (ts, tsx, mts, cts, js, mjs, cjs, svelte) with that app's
    pinned prettier, run from there so its .prettierrc and .prettierignore apply;
  - shell scripts (*.sh, *.bash, extensionless sh/bash shebang scripts) with the pinned shfmt, `-i 4 -ci`.

Requirements (mini-PRD):
  check  ✔︎
    Every changed file of those types must be its formatter's output, except one that was already
    unformatted at the merge-base: the part 6 backlog, which a PR must never be failed for.
      [if] a changed file is not its formatter's output and is new, or was formatted at base [then ⛔️] exit 1,
        naming it and printing the fix command (`just fmt-changed --write`, and the formatter line)
      [if] a changed file is unformatted at base too (renames followed) [then ⛔️] a SKIP notice naming it, exit 0
      [if] a file is deleted, a symlink, a submodule, or of a type no formatter owns [then ⛔️] not checked
      [if] git, ruff, prettier or shfmt cannot run, or a tool is not its pinned version [then ⛔️] exit 2, never a pass
  write  ✔︎
    --write formats exactly the files that FAIL (never the backlog ones, which part 6 formats in its own
    commits), then re-checks them.
      [if] a file still fails after --write [then ⛔️] exit 1

What could satisfy this without satisfying its intent: comparing against the PR's own base file alone
would let a formatted-at-base file regress, so a SKIP needs the base side to be unformatted itself;
checking the whole tree would fail every PR on the backlog, so only changed paths are read.
Standard library plus subprocess. Pinned by (private path).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from scripts.format_proof_shell import CFG as SHELL_CFG
from scripts.format_proof_shell import Verdict, ensure_pinned_shfmt, is_shell_path, is_shell_source

REPO = Path(__file__).resolve().parent.parent


class CFG:
    FRONTEND: str = "apps/webui/frontend"
    FRONTEND_SUFFIXES: tuple[str, ...] = (".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".svelte")
    PYTHON_SUFFIXES: tuple[str, ...] = (".py", ".pyi")
    RUFF_REQUIREMENTS: str = "ops/quality/requirements.txt"
    RUFF: tuple[str, ...] = ("uv", "run", "--isolated", "--no-project", "--quiet", "--python", "3.11")
    REGULAR_MODES: frozenset[str] = frozenset({"100644", "100755"})
    # `ruff format --check --output-format concise`: one line per file, "<path>:<row>:<col>: <kind>: <message>".
    RUFF_DIAGNOSTIC: re.Pattern[str] = re.compile(r"^(?P<path>.+?):\d+:\d+: (unformatted|invalid-syntax): ")
    RUFF_SUMMARY: re.Pattern[str] = re.compile(r"^\d+ files? (would be reformatted|already formatted)")
    PRETTIER_SYNTAX_ERROR: re.Pattern[str] = re.compile(r"^\[error\] (?P<path>[^ ][^:]*): SyntaxError: ")
    FIX_RECIPE: str = "just fmt-changed --write"
    TIMEOUT_S: int = 300
    COLOR_FORCING: frozenset[str] = frozenset({"FORCE_COLOR", "CLICOLOR_FORCE"})


class ToolError(RuntimeError):
    """A formatter or git could not run: exit 2."""


@dataclass(frozen=True)
class Change:
    path: str  # at HEAD
    base_path: str | None  # at the merge-base; None when the branch added it


@dataclass
class Report:
    failed: dict[str, list[Change]] = field(default_factory=dict)  # kind -> changes
    backlog: list[Change] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)


# ----- git ------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ToolError(f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def _git_bytes(repo: Path, rev: str, path: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, check=False)
    if proc.returncode != 0:
        raise ToolError(f"git show {rev}:{path} exited {proc.returncode}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout


def changed_files(repo: Path, fork: str) -> list[Change]:
    """Regular files the branch adds, modifies, renames or copies since fork, as (head path, base path)."""
    raw = _git(repo, "diff", "--raw", "-z", "-M", "-C", "--diff-filter=AMRCT", "--no-abbrev", fork, "HEAD")
    fields = raw.split("\0")
    changes: list[Change] = []
    index = 0
    while index < len(fields) - 1:
        meta = fields[index].lstrip(":").split()
        status, new_mode = meta[4], meta[1]
        if status[0] in "RC":
            old, new = fields[index + 1], fields[index + 2]
            index += 3
        else:
            old = new = fields[index + 1]
            index += 2
        if new_mode not in CFG.REGULAR_MODES:
            continue
        changes.append(Change(new, None if status[0] == "A" or status[0] == "C" else old))
    return changes


# ----- classification -------------------------------------------------------


def kind_of(path: str, head_bytes: bytes) -> str | None:
    """ "python", "frontend", "shell", or None when no ratified formatter owns the path."""
    if path.startswith(f"{CFG.FRONTEND}/"):
        return "frontend" if path.endswith(CFG.FRONTEND_SUFFIXES) else None
    if path.endswith(CFG.PYTHON_SUFFIXES):
        return "python"
    if is_shell_path(path) and is_shell_source(path, head_bytes[:200]):
        return "shell"
    return None


# ----- formatters -----------------------------------------------------------


def _plain_env() -> dict[str, str]:
    """This environment with colour off: CI sets FORCE_COLOR, and coloured output breaks the line parsers."""
    env = {key: value for key, value in os.environ.items() if key not in CFG.COLOR_FORCING}
    env["NO_COLOR"] = "1"
    return env


def _run(command: list[str], cwd: Path, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command, cwd=cwd, input=data, capture_output=True, timeout=CFG.TIMEOUT_S, check=False, env=_plain_env()
        )
    except FileNotFoundError as exc:
        raise ToolError(f"{command[0]} is not on PATH: {exc}") from exc


def ruff_command(*args: str) -> list[str]:
    return [*CFG.RUFF, "--with-requirements", CFG.RUFF_REQUIREMENTS, "ruff", "format", *args]


def _frontend_rel(path: str) -> str:
    return path[len(CFG.FRONTEND) + 1 :]


def prettier_command(repo: Path) -> list[str]:
    """node plus the frontend prettier's JS entry, which must be the exact version package.json pins.

    Not `node_modules/.bin/prettier`: that shim is a shell script, which Windows cannot execute.
    """
    frontend = repo / CFG.FRONTEND
    pin = json.loads((frontend / "package.json").read_text(encoding="utf-8")).get("devDependencies", {}).get("prettier")
    if pin is None:
        raise ToolError(f"{CFG.FRONTEND}/package.json pins no prettier devDependency")
    manifest = frontend / "node_modules" / "prettier" / "package.json"
    if not manifest.is_file():
        raise ToolError(f"prettier is not installed: run `pnpm install --frozen-lockfile` in {CFG.FRONTEND}")
    installed = json.loads(manifest.read_text(encoding="utf-8"))
    if installed["version"] != pin:
        raise ToolError(f"prettier {installed['version']} is installed, {pin} pinned: `pnpm install --frozen-lockfile`")
    node = shutil.which("node")
    if node is None:
        raise ToolError("node is not on PATH, so the frontend prettier cannot run")
    entry = installed["bin"] if isinstance(installed["bin"], str) else installed["bin"]["prettier"]
    return [node, str((manifest.parent / entry).resolve())]


def shfmt_binary() -> str:
    found = ensure_pinned_shfmt(install_missing=True)
    if isinstance(found, Verdict):
        raise ToolError(" ".join(found.lines))
    return found


def _listed(proc: subprocess.CompletedProcess[bytes], tool: str, ok_codes: tuple[int, ...]) -> None:
    if proc.returncode not in ok_codes:
        raise ToolError(f"{tool} exited {proc.returncode}: {proc.stderr.decode(errors='replace').strip()[-500:]}")


def _ruff_flagged(proc: subprocess.CompletedProcess[bytes]) -> set[str]:
    """Paths `ruff format --check --output-format concise` would reformat or cannot parse; any other error raises."""
    flagged: set[str] = set()
    for line in proc.stdout.decode(errors="replace").splitlines():
        match = CFG.RUFF_DIAGNOSTIC.match(line)
        if match:
            flagged.add(match["path"])
        elif proc.returncode == 2 and line and not CFG.RUFF_SUMMARY.match(line):
            raise ToolError(f"ruff format --check: {line}")
    if proc.returncode == 2 and not flagged:
        raise ToolError(f"ruff format --check exited 2: {proc.stderr.decode(errors='replace').strip()[-500:]}")
    return flagged


def _prettier_flagged(proc: subprocess.CompletedProcess[bytes]) -> set[str]:
    """Frontend-relative paths prettier lists as different or cannot parse; exit 2 with no SyntaxError raises."""
    flagged = {line for line in proc.stdout.decode(errors="replace").splitlines() if line}
    stderr = proc.stderr.decode(errors="replace")
    syntax_errors = {match["path"] for match in map(CFG.PRETTIER_SYNTAX_ERROR.match, stderr.splitlines()) if match}
    if proc.returncode == 2 and not syntax_errors:
        raise ToolError(f"prettier --list-different exited 2: {stderr.strip()[-500:]}")
    return flagged | syntax_errors


def _shfmt_output(path: str, data: bytes, repo: Path) -> bytes | None:
    """shfmt -i 4 -ci of data, or None when shfmt cannot parse it."""
    proc = _run([shfmt_binary(), *SHELL_CFG.SHFMT_FLAGS, "--filename", path], repo, data)
    return proc.stdout if proc.returncode == 0 else None


def unformatted(repo: Path, kind: str, paths: list[str]) -> set[str]:
    """The paths, of one kind, whose working-tree content is not their formatter's output or does not parse."""
    if kind == "python":
        proc = _run(ruff_command("--check", "--force-exclude", "--output-format", "concise", "--", *paths), repo)
        _listed(proc, "ruff format --check", (0, 1, 2))
        return _ruff_flagged(proc)
    if kind == "frontend":
        proc = _run([*prettier_command(repo), "--list-different", *map(_frontend_rel, paths)], repo / CFG.FRONTEND)
        _listed(proc, "prettier --list-different", (0, 1, 2))
        return {f"{CFG.FRONTEND}/{rel}" for rel in _prettier_flagged(proc)}
    if kind == "shell":
        contents = {path: (repo / path).read_bytes() for path in paths}
        return {path for path, data in contents.items() if _shfmt_output(path, data, repo) != data}
    raise ValueError(f"unhandled kind {kind}")


def formatted_at_base(repo: Path, kind: str, path: str, data: bytes) -> bool:
    """Whether data, the base side of path, is already its formatter's output; a side that does not parse is not."""
    if kind == "python":
        proc = _run(ruff_command("--check", "--stdin-filename", path, "-"), repo, data)
        if proc.returncode == 2 and b"Failed to parse" in proc.stderr:
            return False
        _listed(proc, "ruff format --check (base side)", (0, 1))
        return proc.returncode == 0
    if kind == "frontend":
        command = [*prettier_command(repo), "--check", "--stdin-filepath", _frontend_rel(path)]
        proc = _run(command, repo / CFG.FRONTEND, data)
        if proc.returncode == 2 and b"SyntaxError" in proc.stderr:
            return False
        _listed(proc, "prettier --check (base side)", (0, 1))
        return proc.returncode == 0
    if kind == "shell":
        return _shfmt_output(path, data, repo) == data
    raise ValueError(f"unhandled kind {kind}")


def write(repo: Path, kind: str, paths: list[str]) -> None:
    if kind == "python":
        proc = _run(ruff_command("--force-exclude", "--", *paths), repo)
    elif kind == "frontend":
        proc = _run([*prettier_command(repo), "--write", "--", *map(_frontend_rel, paths)], repo / CFG.FRONTEND)
    elif kind == "shell":
        proc = _run([shfmt_binary(), "-w", *SHELL_CFG.SHFMT_FLAGS, "--", *paths], repo)
    else:
        raise ValueError(f"unhandled kind {kind}")
    _listed(proc, f"{kind} formatter --write", (0,))


def fix_lines(kind: str, paths: list[str]) -> list[str]:
    files = " ".join(paths)
    if kind == "python":
        return [" ".join(ruff_command("--force-exclude", "--", files))]
    if kind == "frontend":
        return [f"cd {CFG.FRONTEND} && pnpm exec prettier --write -- {' '.join(map(_frontend_rel, paths))}"]
    if kind == "shell":
        return [f"shfmt -w {' '.join(SHELL_CFG.SHFMT_FLAGS)} -- {files}"]
    raise ValueError(f"unhandled kind {kind}")


# ----- check ----------------------------------------------------------------


def check(repo: Path, base: str) -> tuple[str, Report]:
    """(merge-base SHA, report) for the files HEAD changes since its merge-base with base."""
    fork = _git(repo, "merge-base", base, "HEAD").strip()
    by_kind: dict[str, list[Change]] = {}
    for change in changed_files(repo, fork):
        # Classify the committed path even when its working copy has been deleted.
        kind = kind_of(change.path, _git_bytes(repo, "HEAD", change.path)[:200])
        if kind is not None:
            by_kind.setdefault(kind, []).append(change)
    report = Report(checked={kind: len(changes) for kind, changes in by_kind.items()})
    for kind, changes in sorted(by_kind.items()):
        flagged = unformatted(repo, kind, [change.path for change in changes])
        for change in changes:
            if change.path not in flagged:
                continue
            base_side = None if change.base_path is None else _git_bytes(repo, fork, change.base_path)
            # The base content is judged where it lives now: a file renamed into apps/webui/frontend
            # takes that directory's prettier config, and its old path may lie outside it.
            if base_side is not None and not formatted_at_base(repo, kind, change.path, base_side):
                report.backlog.append(change)
            else:
                report.failed.setdefault(kind, []).append(change)
    return fork, report


def _print_report(fork: str, report: Report, in_actions: bool) -> None:
    counts = (
        ", ".join(f"{count} {kind}" for kind, count in sorted(report.checked.items())) or "none of a formatted type"
    )
    print(f"[format-check] merge-base {fork}: changed files checked: {counts}")
    for change in report.backlog:
        reason = "unformatted at the merge-base too: formatter backlog (issue #4456 part 6), not this PR's to fix"
        print(f"[format-check] SKIP {change.path}: {reason}")
        if in_actions:
            print(f"::notice file={change.path}::format check skipped: {reason}")
    for kind, changes in sorted(report.failed.items()):
        for change in changes:
            print(f"[format-check] FAIL {change.path}: not {kind} formatter output")
            if in_actions:
                print(f"::error file={change.path}::not formatted: run `{CFG.FIX_RECIPE}`")
    if report.failed:
        print(f"[format-check] FIX: {CFG.FIX_RECIPE}   (formats only the FAIL files), or by hand:")
        for kind, changes in sorted(report.failed.items()):
            for line in fix_lines(kind, [change.path for change in changes]):
                print(f"    {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="the trunk; the diff starts at its merge-base with HEAD")
    parser.add_argument("--write", action="store_true", help="format the files that fail, then check again")
    args = parser.parse_args(argv)
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    try:
        fork, report = check(REPO, args.base)
        if args.write and report.failed:
            for kind, changes in sorted(report.failed.items()):
                write(REPO, kind, [change.path for change in changes])
                print(f"[format-check] wrote {len(changes)} {kind} file(s)")
            fork, report = check(REPO, args.base)
    except ToolError as exc:
        print(f"[format-check] UNKNOWN {exc}")
        return 2
    _print_report(fork, report, in_actions)
    if report.failed:
        return 1
    print(f"[format-check] OK: no changed file is unformatted ({len(report.backlog)} backlog file(s) skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
