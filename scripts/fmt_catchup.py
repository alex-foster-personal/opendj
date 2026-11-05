"""Bring a branch or PR past the formatter parts of issue #4456 without a hand-resolved conflict.

Usage: python scripts/fmt_catchup.py [--main <ref>] <branch|PR-number> [<format-sha> ...]
       just fmt-catchup <branch|PR-number> [<format-sha> ...]

--main names the trunk the branch forked from (origin/main when omitted, fetched first). The
branch's own changes are the diff from its merge-base with that ref to its head. Run it from a
clean worktree whose current branch is the target. With no SHA it takes every commit listed in
the trunk's .git-blame-ignore-revs that the branch does not contain yet, in file order. For each
format commit F it runs the recipe in (private path) ("Blast radius"):

  1. merge F^ into the branch (a real conflict here is not a format conflict: stop);
  2. run the pinned formatter on the files the branch itself changes (not files main changed
     under it) that F also formats, following renames on either side, and commit that as
     "style(format): catch-up <F>";
  3. merge F taking the branch's side of each conflicting hunk (-X ours). The formatter is
     deterministic, so both sides already agree on those files;
  4. verify with the formatter's --check on the files of step 2.

Merge commits only: it never rebases, force-pushes or pushes. Push it yourself afterwards (a
queued PR goes through dequeue-for-push.sh first).

Python files get the ruff every part ran: ruff from ops/quality/requirements.txt (ruff==0.16.3)
through an isolated uv env on Python 3.11, in path mode with --force-exclude so the
[tool.ruff.format] exclusions in pyproject.toml hold. Frontend sources (ts, tsx, mts, cts, js,
mjs, cjs, svelte under apps/webui/frontend) get the frontend's pinned prettier, run from that
directory so its .prettierrc and .prettierignore apply; node_modules is installed with
`pnpm install --frozen-lockfile` when the installed prettier is not the version package.json pins.
Shell scripts (*.sh, *.bash, and extensionless files with a sh or bash shebang) get the pinned shfmt
with the ratified flags, `shfmt -w -i 4 -ci`; a missing or other-version shfmt is a refusal that names
`python -m scripts.format_proof_shell install`. SHFMT_VERSION and SHFMT_FLAGS here must equal
scripts/format_proof_shell.py's CFG (a test pins that), since this file runs as a plain script.

Standard library only, so it runs from any checkout on macOS, Linux and Windows.
Pinned by tests/scripts/test_fmt_catchup.py.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

FRONTEND = "apps/webui/frontend"
FRONTEND_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".svelte")
RUFF = ["uv", "run", "--isolated", "--no-project", "--python", "3.11"]
RUFF += ["--with-requirements", "ops/quality/requirements.txt", "ruff", "format", "--force-exclude"]
SHFMT_VERSION = "3.14.1"
SHFMT_FLAGS = ("-i", "4", "-ci")
SHELL_SUFFIXES = (".sh", ".bash")
SHELL_SHEBANG = re.compile(rb"#!\s*(/usr/bin/env\s+(-S\s+)?)?(/usr/local/bin/|/usr/bin/|/bin/)?(ba|da|k|mk)?sh\b")
SHA_LINE = re.compile(r"^[0-9a-f]{40}$")
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class CatchupError(RuntimeError):
    """A refusal: printed as [ERROR] and exit 1."""


# -----------------------------------------------------------------------------
# git and tool helpers


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise CatchupError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def _git_out(*args: str) -> str:
    return _git(*args).stdout.strip()


def _run_tool(command: list[str], cwd: str | None = None) -> int:
    executable = shutil.which(command[0]) or command[0]
    return subprocess.run([executable, *command[1:]], cwd=cwd, check=False).returncode


def _name_status(base: str, head: str) -> list[tuple[str, str, str]]:
    """(status letter, old path, new path) for every change from base to head, renames followed."""
    rows: list[tuple[str, str, str]] = []
    for line in _git_out("diff", "--name-status", "-M", base, head).splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status == "R":
            rows.append((status, parts[1], parts[2]))
        elif status in "MADT":
            rows.append((status, parts[1], parts[1]))
        else:
            rows.append((status, parts[-1], parts[-1]))
    return rows


def _is_frontend_source(path: str) -> bool:
    return path.startswith(f"{FRONTEND}/") and path.endswith(FRONTEND_SUFFIXES)


def _is_shell_script(path: str) -> bool:
    if path.endswith(SHELL_SUFFIXES):
        return True
    if "." in Path(path).name:
        return False
    with open(path, "rb") as handle:
        return bool(SHELL_SHEBANG.match(handle.readline(200)))


# -----------------------------------------------------------------------------
# formatters


def _ruff(mode: list[str], files: list[str]) -> int:
    return _run_tool([*RUFF, *mode, "--", *files])


def _ensure_pinned_prettier() -> None:
    package = json.loads(Path(FRONTEND, "package.json").read_text(encoding="utf-8"))
    pin = package.get("devDependencies", {}).get("prettier", "")
    if not EXACT_VERSION.match(pin):
        raise CatchupError(f"{FRONTEND}/package.json does not pin prettier to an exact version: {pin!r}")
    installed_manifest = Path(FRONTEND, "node_modules", "prettier", "package.json")
    installed = (
        json.loads(installed_manifest.read_text(encoding="utf-8"))["version"]
        if installed_manifest.is_file()
        else "none"
    )
    if installed != pin:
        print(
            f"[INFO] fmt-catchup: prettier {installed} installed, {pin} pinned; pnpm install --frozen-lockfile in {FRONTEND}"
        )
        if _run_tool(["pnpm", "install", "--frozen-lockfile"], cwd=FRONTEND) != 0:
            raise CatchupError(f"pnpm install --frozen-lockfile failed in {FRONTEND}")


def _prettier(mode: str, files: list[str]) -> int:
    _ensure_pinned_prettier()
    binary = Path(FRONTEND, "node_modules", ".bin", "prettier.cmd" if sys.platform == "win32" else "prettier")
    relative = [path.removeprefix(f"{FRONTEND}/") for path in files]
    return _run_tool([str(binary.resolve()), mode, "--log-level", "warn", "--", *relative], cwd=FRONTEND)


def _pinned_shfmt() -> str:
    binary = shutil.which("shfmt")
    version = (
        subprocess.run([binary, "--version"], capture_output=True, text=True, check=False).stdout.strip()
        if binary
        else "none"
    )
    if binary is None or version != f"v{SHFMT_VERSION}":
        raise CatchupError(
            f"shfmt {version} found, v{SHFMT_VERSION} pinned: run python -m scripts.format_proof_shell install"
        )
    return binary


def _shfmt(mode: str, files: list[str]) -> int:
    return _run_tool([_pinned_shfmt(), mode, *SHFMT_FLAGS, "--", *files])


def _shfmt_unformatted(files: list[str]) -> list[str]:
    """The files `shfmt -l` lists. Read from stdout, not the exit code alone, so a listing is never missed."""
    listed = subprocess.run(
        [_pinned_shfmt(), "-l", *SHFMT_FLAGS, "--", *files], capture_output=True, text=True, check=False
    )
    unformatted = listed.stdout.split()
    if listed.returncode != 0 and not unformatted:
        raise CatchupError(f"shfmt -l exited {listed.returncode}: {listed.stderr.strip() or '(no stderr)'}")
    return unformatted


# -----------------------------------------------------------------------------
# the catch-up


@dataclass
class FilesToFormat:
    """The branch's files a format commit formats, by formatter."""

    python: list[str] = field(default_factory=list)
    frontend: list[str] = field(default_factory=list)
    shell: list[str] = field(default_factory=list)

    def count(self) -> int:
        return len(self.python) + len(self.frontend) + len(self.shell)


@dataclass
class BranchChanges:
    """The branch's own changes from its fork point: fork path -> head path, and deleted fork paths."""

    fork: str
    own: dict[str, str] = field(default_factory=dict)
    deleted: set[str] = field(default_factory=set)

    @classmethod
    def measure(cls, main_ref: str) -> BranchChanges:
        fork = _git_out("merge-base", "HEAD", main_ref)
        changes = cls(fork=fork)
        for status, old, new in _name_status(fork, "HEAD"):
            if status in "MR":
                changes.own[old] = new
            elif status == "D":
                changes.deleted.add(old)
        return changes

    def files_to_format(self, parent: str, fmt: str, short: str) -> FilesToFormat:
        """Paths in the merged tree of the branch's own edits to files that fmt formats."""
        fork_path_at_parent = {old: new for status, old, new in _name_status(self.fork, parent) if status == "R"}
        parent_path_to_fork = {new: old for old, new in fork_path_at_parent.items()}
        files = FilesToFormat()
        for formatted in _git_out("diff-tree", "--no-commit-id", "--name-only", "-r", parent, fmt).splitlines():
            fork_path = parent_path_to_fork.get(formatted, formatted)
            if fork_path in self.own:
                branch_path = self.own[fork_path]
                target = branch_path if branch_path != fork_path else formatted
                if not Path(target).is_file():
                    continue
                if target.endswith(".py"):
                    files.python.append(target)
                elif _is_frontend_source(target):
                    files.frontend.append(target)
                elif _is_shell_script(target):
                    files.shell.append(target)
            elif fork_path in self.deleted:
                print(
                    f"[WARN] the branch deletes {fork_path}, which {short} formats. If it moved past git's "
                    "rename detection, run the pinned formatter on its new path by hand",
                    file=sys.stderr,
                )
        return files


def _merge_parent(parent: str, short: str) -> None:
    message = f"chore(format): merge {short}^ ahead of the format catch-up"
    if _git("merge", "--no-ff", "--no-edit", "-m", message, parent, check=False).returncode != 0:
        _git("merge", "--abort")
        raise CatchupError(
            f"merging {short}^ conflicts: that is a content conflict, not a format one. Resolve it with main first"
        )


def _format_and_commit(files: FilesToFormat, short: str) -> None:
    if files.python:
        if _ruff([], files.python) != 0:
            raise CatchupError(f"ruff format failed on the branch files before merging {short}")
        _git("add", "--", *files.python)
    if files.frontend:
        if _prettier("--write", files.frontend) != 0:
            raise CatchupError(f"prettier --write failed on the branch files before merging {short}")
        _git("add", "--", *files.frontend)
    if files.shell:
        if _shfmt("-w", files.shell) != 0:
            raise CatchupError(f"shfmt -w failed on the branch files before merging {short}")
        _git("add", "--", *files.shell)
    if _git("diff", "--cached", "--quiet", check=False).returncode != 0:
        body = (
            "The pinned formatter (ruff from ops/quality/requirements.txt, the frontend's pinned prettier, or "
            f"shfmt {SHFMT_VERSION} {' '.join(SHFMT_FLAGS)}) "
            f"on this branch's own changes to files {short} formats, before merging it. Command: scripts/fmt_catchup.py."
        )
        _git("commit", "-q", "-m", f"style(format): catch-up {short}", "-m", body)


def _merge_format_commit_keeping_branch_side(fmt: str, short: str) -> None:
    message = f"chore(format): merge format commit {short}, branch side kept"
    if _git("merge", "--no-ff", "--no-edit", "-X", "ours", "-m", message, fmt, check=False).returncode == 0:
        return
    for unmerged in _git_out("diff", "--name-only", "--diff-filter=U").splitlines():
        if _git("cat-file", "-e", f"HEAD:{unmerged}", check=False).returncode == 0:
            _git("merge", "--abort")
            raise CatchupError(f"unresolvable conflict on {unmerged} merging {short}")
        _git("rm", "-q", "--", unmerged)
    _git("commit", "-q", "--no-edit")


def _verify_formatted(files: FilesToFormat, short: str) -> None:
    if files.python and _ruff(["--check"], files.python) != 0:
        raise CatchupError(f"ruff format --check fails after merging {short}")
    if files.frontend and _prettier("--check", files.frontend) != 0:
        raise CatchupError(f"prettier --check fails after merging {short}")
    unformatted = _shfmt_unformatted(files.shell) if files.shell else []
    if unformatted:
        raise CatchupError(f"shfmt -l lists unformatted files after merging {short}: {' '.join(unformatted)}")


def _catch_up_one(fmt: str, branch: str, changes: BranchChanges) -> None:
    if _git("cat-file", "-e", f"{fmt}^{{commit}}", check=False).returncode != 0:
        raise CatchupError(f"{fmt} is not a commit here (fetch origin first)")
    short = _git_out("rev-parse", "--short=10", fmt)
    if _git("merge-base", "--is-ancestor", fmt, "HEAD", check=False).returncode == 0:
        print(f"[OK] {short} already in {branch}")
        return
    if len(_git_out("rev-list", "--parents", "-n1", fmt).split()) != 2:
        raise CatchupError(f"{short} is not a single-parent commit")
    parent = _git_out("rev-parse", f"{fmt}^")
    _merge_parent(parent, short)
    files = changes.files_to_format(parent, fmt, short)
    _format_and_commit(files, short)
    _merge_format_commit_keeping_branch_side(fmt, short)
    _verify_formatted(files, short)
    print(f"[OK] {short} merged into {branch}, {files.count()} branch file(s) caught up")


def _resolve_branch(target: str) -> str:
    if not target.isdigit():
        return target
    result = subprocess.run(
        [shutil.which("gh") or "gh", "pr", "view", target, "--json", "headRefName", "--jq", ".headRefName"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CatchupError(f"gh could not read PR #{target}: {result.stderr.strip()}")
    return result.stdout.strip()


def catch_up(target: str, format_shas: list[str], main_ref: str) -> None:
    branch = _resolve_branch(target)
    current = _git_out("branch", "--show-current")
    if current != branch:
        raise CatchupError(f"check out '{branch}' in this worktree first (current branch: '{current or 'detached'}')")
    if _git_out("status", "--porcelain", "--untracked-files=no"):
        raise CatchupError("worktree has uncommitted changes; commit them first")
    if main_ref == "origin/main":
        _git("fetch", "-q", "origin", "main")
    if not format_shas:
        listed = _git_out("show", f"{main_ref}:.git-blame-ignore-revs").splitlines()
        format_shas = [line for line in listed if SHA_LINE.match(line)]
    if not format_shas:
        raise CatchupError("no format commits to catch up on")
    changes = BranchChanges.measure(main_ref)
    for fmt in format_shas:
        _catch_up_one(fmt, branch, changes)
    print(f"[OK] fmt-catchup: {branch} is past {len(format_shas)} format commit(s); nothing pushed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--main", dest="main_ref", default="origin/main", help="trunk ref the branch forked from")
    parser.add_argument("target", help="branch name, or a PR number resolved with gh")
    parser.add_argument("format_shas", nargs="*", help="format commits; default: the trunk's .git-blame-ignore-revs")
    args = parser.parse_args(argv)
    try:
        catch_up(args.target, args.format_shas, args.main_ref)
    except CatchupError as error:
        print(f"[ERROR] fmt-catchup: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
